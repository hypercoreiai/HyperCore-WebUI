"""serve/vision.py - the vision (mmproj) model lifecycle.

Vision runs as its own process beside the text engine, so it needs its own VRAM estimate, its own
unload handshake and its own image decoding path.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from http.client import HTTPException
from pathlib import Path

from serve.proc import network_path, popen
from serve.winjob import contain

IMAGE_URL_MAX = 32 << 20  # an image URL is read up to this (the web app attaches pictures of up to 20 MB)

VISION_ALLOC_MARKS = ("cudaMalloc", "alloc_tensor_range", "GGML_ASSERT", "out of memory")

ENGINE_LINES = ("hypercore serve:", "hypercore generate:", "hypercore_worker:", "hypercore mtp:",
                "hypercore verify:", "hypercore hc:", "FileExpertSource:")

VISION_VRAM_MIB = 1400          # the GPU encoder's share: 865 MiB of BF16 mmproj tensors + its CUDA context + work buffers

VISION_VRAM_HINT = (" - the image encoder needs its own share of VRAM (about 1.4 GB with --vision gpu): run setup "
                    "with --vision cpu, give it a card of its own (vision.cuda_device in the config), or free the "
                    "one it is on (docs/TROUBLESHOOTING.md)")

def vision_start_reason(log: str | None, offset: int) -> str:
    """What the image encoder said before it died: after `offset` in the log its stderr went to - the model's log is
    shared with the engine, so the engine's lines are skipped - the line naming an allocation (the most actionable
    mark first), else its last line.  "" with no log or nothing new in it."""
    if not log:
        return ""
    try:
        with open(log, "rb") as f:
            f.seek(offset)
            lines = [x.strip() for x in f.read().decode("utf-8", "replace").splitlines()
                     if x.strip() and not x.strip().startswith(ENGINE_LINES)]
    except OSError:
        return ""
    for mark in VISION_ALLOC_MARKS:
        for line in lines:
            if mark in line:
                return line
    return lines[-1] if lines else ""

class Vision:
    """The resident image encoder: `hypercore-vision` (llama.cpp mtmd + the mmproj file) reads `ENC <image> <out>`
    lines and writes each image's embeddings; results are cached by the image's hash, so a conversation that
    sends the same picture again (every turn, with most clients) encodes it once."""

    @staticmethod
    def work_dir() -> Path:
        """#480: the directory for the encoder's files.  The engine reads the embeddings file named in a `GENI` line
        and the encoder the output named in an `ENC <image> <out>` line, both split at spaces, so a space in the path
        (a %TEMP% under a user name with a space) broke every image.  The system temp directory is used while its path
        has no space, then the working directory, then the system drive's root; the plain temp directory is the last
        resort (the encoder's own files are then named relative to it, see `_start`)."""
        for parent in (None, Path.cwd(), Path(os.environ.get("SystemDrive", "C:") + os.sep)):
            try:
                d = Path(tempfile.mkdtemp(prefix="hypercore-vision-", dir=parent))
            except OSError:
                continue                                       # not writable, or no such drive
            if " " not in str(d):
                return d
            try:
                d.rmdir()                                      # just created, so still empty
            except OSError:
                pass
        return Path(tempfile.mkdtemp(prefix="hypercore-vision-"))

    def __init__(self, cfg: dict, log=None, env: dict | None = None):
        def absolute(p):   # the encoder runs in its own dir (#480)
            return os.path.abspath(p) if os.path.dirname(str(p)) else p
        args = [absolute(cfg["exe"]), "--mmproj", absolute(cfg["mmproj"]), "--model", absolute(cfg["model"])]
        if cfg.get("gpu"):
            args.append("--gpu")
        if cfg.get("threads"):
            args += ["--threads", str(cfg["threads"])]
        if cfg.get("max_tokens"):
            args += ["--max-tokens", str(cfg["max_tokens"])]
        if cfg.get("min_tokens"):                       # #767: mtmd's image_min_tokens (a hand-edited key)
            args += ["--min-tokens", str(cfg["min_tokens"])]
        self.dir = self.work_dir()
        self.spawn = (args, log, env)                   # to start it again after an unload
        self.cpu_only = False                           # set when the card had no room for the encoder
        self.stopped = False
        self._start()
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[Path, int]] = {}

    def _start(self, args: list | None = None):
        args = list(self.spawn[0]) if args is None else list(args)
        log, env = self.spawn[1], self.spawn[2]
        # where this start's lines begin in the log: an append-mode handle reports 0 until it is written to, and the
        # lines of an earlier start would then be read as the encoder's own
        log_path = getattr(log, "name", None)
        log_start = os.path.getsize(log_path) if log_path else 0
        # cwd is the encoder's directory: the ENC lines name the files relative to it (#480: no space in the line)
        self.proc = popen("the image encoder", args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=log or subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=env,
                          cwd=self.dir)
        contain(self.proc)
        for line in iter(self.proc.stdout.readline, ""):
            if line.startswith("READY"):
                self.stopped = False
                return
            if line.startswith("ERR"):                       # the encoder's own "cannot do this"
                raise RuntimeError("the vision encoder did not start: " + line.strip())
            # anything else on stdout before READY is noise (a crash reporter's stack trace, a loader warning):
            # the reason is on stderr, which went to the log
        code = self.proc.wait()
        why = vision_start_reason(log_path, log_start)
        if "--gpu" in args and not self.cpu_only and any(m in why for m in VISION_ALLOC_MARKS):
            # the card has no room for the encoder: a chat that answers is better than one that will not start
            print(f"[hypercore] the image encoder does not fit the VRAM left on the card ({why}). It runs on the "
                  "CPU instead - pictures are slow (about 3-13 s) but they work. To keep it on the GPU, run setup "
                  f"again with --vram-reserve-mib {VISION_VRAM_MIB} or --vision cpu (docs/TROUBLESHOOTING.md).",
                  flush=True)
            self.cpu_only = True
            self.spawn = ([a for a in args if a != "--gpu"], log, env)   # a restart stays on the CPU
            return self._start([a for a in args if a != "--gpu"])
        hint = VISION_VRAM_HINT if any(m in why for m in VISION_ALLOC_MARKS) else ""
        raise RuntimeError(f"the vision encoder did not start (it exited with {code})" +
                          (": " + why if why else "") + hint)

    def alive(self) -> bool:
        return not self.stopped and self.proc.poll() is None

    def unload(self):
        """Stop the encoder process (its VRAM or RAM goes back); the encoded images stay cached on disk."""
        self.close()
        self.stopped = True

    def restart(self):
        """Start the encoder again after an unload (or if it died); the cache of encoded images is kept."""
        try:
            self.proc.kill()
        except OSError:
            pass
        self._start()

    @staticmethod
    def load(source: str) -> bytes:
        if source.startswith("data:"):
            try:
                return base64.b64decode(source.split(",", 1)[1])
            except (IndexError, ValueError) as e:                # no comma, or not base64
                raise ValueError(f"the image's data: URL could not be read ({e})") from None
        if source.startswith(("http://", "https://")):
            return Vision.download(source)
        path = source.removeprefix("file://")
        # refused before anything looks at the path: file://host/share/x.png is \\host\share\x.png ("file://C:/x.png",
        # a drive, is a client's spelling of file:///C:/x.png)
        host = re.split(r"[/\\]", path, maxsplit=1)[0] if source.startswith("file://") else ""
        if network_path(path) or host and not re.fullmatch(r"[A-Za-z]:|localhost", host, re.IGNORECASE):
            raise ValueError("an image file must be on this computer: network paths (\\\\host\\share, //host/share, "
                             "file://host/...) are not read")
        if path and os.path.isfile(path):
            try:
                return Path(path).read_bytes()
            except OSError as e:
                raise ValueError(f"the image could not be read from {path} ({e})") from None
        raise ValueError("an image must be a data: URL, an http(s) URL or a local file path")

    @staticmethod
    def download(url: str) -> bytes:
        """An image URL's bytes, at most IMAGE_URL_MAX of them: the whole response was read, so a huge or endless one
        filled the memory.  One that cannot be read is a ValueError (a 400 that says so), not a dropped connection."""
        too_big = f"the image URL's file is over {IMAGE_URL_MAX >> 20} MiB"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hypercore"}), timeout=60) as r:
                size = r.headers.get("Content-Length") or ""
                if size.isdigit() and int(size) > IMAGE_URL_MAX:
                    raise ValueError(too_big)
                data = r.read(IMAGE_URL_MAX + 1)
        except (OSError, HTTPException) as e:
            raise ValueError(f"the image URL could not be read: {e}") from None
        if len(data) > IMAGE_URL_MAX:
            raise ValueError(too_big)
        return data

    @staticmethod
    def normalize(data: bytes) -> bytes:
        """The formats hypercore-vision's decoder (stb_image) reads pass through; anything else is converted to PNG."""
        if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"BM" or \
                data[:6] in (b"GIF87a", b"GIF89a"):
            return data
        try:
            
            from PIL import Image
        except ImportError:
            raise ValueError("this image format needs Pillow (python -m pip install pillow); JPEG, PNG, BMP and "
                             "GIF work without it") from None
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as e:
            raise ValueError(f"the image could not be read ({e})") from None
        if im.mode in ("RGBA", "LA", "P") and "transparency" in im.info or im.mode in ("RGBA", "LA"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))   # transparent areas become white, not black
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()

    def encode(self, source: str | bytes) -> tuple[Path, int]:
        """-> (embeddings file, number of image tokens).  `source`: what load() reads, or the image's bytes."""
        data = self.normalize(source if isinstance(source, bytes) else self.load(source))
        key = hashlib.sha256(data).hexdigest()[:32]
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            img, out = self.dir / f"{key}.img", self.dir / f"{key}.sve"
            img.write_bytes(data)
            try:
                self.proc.stdin.write(f"ENC {img.name} {out.name}\n")      # relative to the encoder's cwd (#480)
                self.proc.stdin.flush()
                line = self.proc.stdout.readline().strip()
            finally:                                                   # #352: also when the encoder's pipe is gone
                img.unlink(missing_ok=True)
            if not line.startswith("OK"):
                raise ValueError("the image could not be read: " + (line[4:] if line.startswith("ERR") else
                                                                     "the vision encoder stopped"))
            self.cache[key] = (out, int(line.split()[1]))
            if len(self.cache) > 64:                                   # oldest first
                old = next(iter(self.cache))
                self.cache.pop(old)[0].unlink(missing_ok=True)
            return self.cache[key]

    def close(self):
        try:
            self.proc.stdin.write("QUIT\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def shutdown(self):
        """#914: close() for good: the server ends, so the encoder's directory (one ~10 MB .sve per image, on a tmpfs
        /tmp that is RAM) goes with it instead of piling up with every start.  unload() keeps it for the restart."""
        self.close()
        shutil.rmtree(self.dir, ignore_errors=True)
