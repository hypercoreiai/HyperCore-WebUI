"""serve/build_api.py - the Build tab: check for a new Strata, get it, build it.

Two ways to update the engine, both offered:

  ready-made   download the release asset for this OS, verify its published
               SHA-256, unpack into engine/. Seconds.
  from source  shallow-clone the release into tmp/, inject engine-src/ so the
               HyperCore worker targets exist, configure with the options the
               Build tab's dropdowns came from, build strata + strata-vision +
               hypercore-worker, copy them into engine/, delete tmp/.

tmp/ holds exactly one clone at a time and is deleted when a build finishes.
Nothing at runtime reads it.

The version manifest is extracted from the clone itself (tools/extract_build_options.py),
never from a list we maintain, so a release that adds an option shows up in the
dropdowns the moment it is cloned.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from serve import quant_store
from serve.quant_store import normalise_quant

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
TMP_DIR = ROOT / "tmp"
MANIFEST = ROOT / "config" / "build_options.json"
REPO = "Niko1221/Strata"
UA = "HyperCore-WebUI/1.0 (+https://github.com/HyperCore-WebUI; build tab release check)"
IS_WIN = os.name == "nt"
EXE = "strata.exe" if IS_WIN else "strata"
VEXE = "strata-vision.exe" if IS_WIN else "strata-vision"
WORKER = "hypercore-worker.exe" if IS_WIN else "hypercore-worker"


def normalise_or_none(quant: Any) -> str | None:
    """A quant name, or None when the caller named none or named one badly.

    A bad quant must not stop a build: it is logged and the engine is stored as the
    default one instead.
    """
    try:
        return normalise_quant(quant) if quant else None
    except ValueError:
        return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- engine version
def installed_version() -> dict[str, Any]:
    """What engine is in engine/, from BUILD.json, or from the binary itself."""
    out = {"version": None, "source": None, "archs": [], "built_at": None, "worker": False}
    meta = ENGINE_DIR / "BUILD.json"
    if meta.is_file():
        try:
            out.update(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    exe = ENGINE_DIR / EXE
    if out["version"] is None and exe.is_file():
        try:
            import re
            m = re.search(rb"engine=(\d+\.\d+\.\d+(?:\.\d+)?)\n", exe.read_bytes())
            out["version"] = m.group(1).decode() if m else None
        except OSError:
            pass
    out["worker"] = (ENGINE_DIR / WORKER).is_file()
    return out


def latest_release() -> dict[str, Any]:
    """The newest published release of Niko1221/Strata, and the assets it carries."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases?per_page=20",
            headers={"User-Agent": "HyperCore-WebUI", "Accept": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except Exception as exc:  # noqa: BLE001 - offline is normal; the tab must not crash
        return {"error": str(exc), "releases": []}
    releases = [
        {
            "tag": r.get("tag_name"),
            "version": (r.get("name") or r.get("tag_name") or "").replace("v", "").replace("Strata", "").strip(),
            "published": r.get("published_at"),
            "assets": [
                {"name": a["name"], "url": a["browser_download_url"], "size": a.get("size")}
                for a in r.get("assets", [])
            ],
        }
        for r in data
    ]
    return {"repo": REPO, "releases": releases, "checked_at": _now()}


def build_status() -> dict[str, Any]:
    """Everything the Build tab needs in one call.

    `latest` is the whole listing ({repo, releases, checked_at}, or {error, releases: []}
    when GitHub cannot be reached) so the tab can fill its version dropdown; `newest` is
    the single release the update note points at. They were one and the same object once,
    which is why the dropdown never populated: the page reads `latest.releases` and got a
    single release back.
    """
    installed = installed_version()
    listing = latest_release()
    releases = listing.get("releases") or []
    newest = releases[0] if releases else None
    have = installed.get("version")
    options = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else None
    return {
        "installed": installed,
        "latest": listing,
        "newest": newest,
        "update_available": bool(newest and have and newest.get("version") != have),
        "options": options,
        "setup": setup_choices(options or {}),
        "job": JOB.status(),
    }


#: The two files that say what a release can be built and configured for. Reading
#: them from raw.githubusercontent is a fraction of a clone, so the Build tab can
#: show the choices before any build starts.
SOURCE_FILES = ("CMakeLists.txt", "setup.py")
_options_cache: dict[str, dict[str, Any]] = {}


def fetch_release_files(tag: str) -> tuple[Path | None, str]:
    """CMakeLists.txt and setup.py of a release, without cloning it.

    Written under tmp/, which is scratch and deleted straight after: nothing at
    runtime reads it. Returns (folder, "") or (None, the reason GitHub was not
    reachable).
    """
    dest = TMP_DIR / f"manifest-{tag}"
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        for name in SOURCE_FILES:
            req = urllib.request.Request(f"https://raw.githubusercontent.com/{REPO}/{tag}/{name}",
                                        headers={"User-Agent": UA})
            dest.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(urllib.request.urlopen(req, timeout=20).read())
        return dest, ""
    except Exception as exc:  # noqa: BLE001 - offline is normal; the tab falls back
        shutil.rmtree(dest, ignore_errors=True)
        return None, str(exc)


def options_for(tag: str) -> dict[str, Any]:
    """How a release can be built and configured, read from the release's own files.

    Nothing is hard-coded against a Strata version: the options come from the
    CMakeLists.txt and setup.py of the tag asked for. The local manifest (written by
    the last build) is used when it is for that same version, so a machine that is
    offline still gets the list.
    """
    tag = tag or "main"
    if tag in _options_cache:
        return _options_cache[tag]
    local = None
    if MANIFEST.is_file():
        try:
            local = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            local = None
    if local and (local.get("version") or "") == tag.lstrip("v"):
        result = {**local, "tag": tag, "from": "the last build of this release"}
        _options_cache[tag] = result
        return result

    sys.path.insert(0, str(ROOT / "tools"))
    from extract_build_options import diff, manifest  # a local tool, not a package

    clone, error = fetch_release_files(tag)
    if clone is None:
        result = {"error": f"could not read {REPO}/{tag}: {error}", "tag": tag,
                  "cmake_options": [], "setup_choices": [], "from": "unreachable"}
    else:
        try:
            result = manifest(clone)
        except Exception as exc:  # noqa: BLE001 - a release we cannot parse is reported, not raised
            result = {"error": f"the release's files could not be read: {exc}", "tag": tag,
                      "cmake_options": [], "setup_choices": [], "from": "unreachable"}
        finally:
            shutil.rmtree(clone, ignore_errors=True)   # tmp/ is scratch
        result["tag"] = tag
        result["from"] = "github"
        if local:
            result["changes"] = diff(local, result)
    _options_cache[tag] = result
    return result


#: The five groups the Build tab lists. Everything else a setup.py accepts is
#: counted and named in one line, not shown as a control.
VISIBLE_GROUPS = ("model", "context", "kv-cache", "images", "experimental")
#: Only for the case where tools/extract_build_options.py cannot be imported.
_FALLBACK_NEEDLES = {"model": ("family", "model"), "context": ("context", "rope"),
                     "kv-cache": ("kv",), "images": ("image", "vision"),
                     "experimental": ("experimental",)}


def choice_group(flag: str) -> str:
    """What a setup.py flag is about. The tool that reads setup.py decides; this
    function only falls back to its own names if that tool is not importable."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        from extract_build_options import setup_group  # a local tool, not a package
        return setup_group(flag)
    except Exception:  # noqa: BLE001 - a missing local tool must not break the tab
        name = flag.lstrip("-").lower()
        for group, needles in _FALLBACK_NEEDLES.items():
            if any(needle in name for needle in needles):
                return group
        return "other"


def normalise_choice(choice: dict[str, Any]) -> dict[str, Any]:
    """Add the group and visibility a manifest written by an older build lacks.

    config/build_options.json from before the Build tab listed choices has no
    "group" or "visible" key; without these the card would show nothing at all.
    """
    if "group" in choice and "visible" in choice:
        return choice
    group = choice.get("group") or choice_group(choice.get("flag", ""))
    visible = choice.get("visible")
    if visible is None:
        visible = group in VISIBLE_GROUPS
    return {**choice, "group": group, "visible": bool(visible)}


def setup_choices(options: dict[str, Any]) -> dict[str, Any]:
    """The setup.py flags grouped the way the Build tab shows them.

    Returns {"groups": {name: [flag, ...]}, "hidden": n}: the five groups a person
    chooses before building are listed, the housekeeping flags (paths, ports, source
    mirrors) are counted so the tab can say they exist rather than omit them.
    """
    choices = [normalise_choice(c) for c in options.get("setup_choices", []) if isinstance(c, dict)]
    groups: dict[str, list[dict[str, Any]]] = {}
    hidden = 0
    for choice in choices:
        if choice["visible"] and choice["group"] in VISIBLE_GROUPS:
            groups.setdefault(choice["group"], []).append(choice)
        else:
            hidden += 1
    return {"groups": groups, "hidden": hidden, "total": len(choices)}


def published_digests(release: dict[str, Any]) -> dict[str, str]:
    """The SHA256.txt a release publishes, as {asset name: digest}."""
    for a in release.get("assets", []):
        if a["name"] == "SHA256.txt":
            try:
                text = urllib.request.urlopen(a["url"], timeout=20).read().decode()
                return {line.split()[1]: line.split()[0] for line in text.splitlines() if len(line.split()) == 2}
            except Exception:  # noqa: BLE001 - a missing digest is not fatal
                return {}
    return {}


def asset_for(release: dict[str, Any]) -> dict[str, Any] | None:
    """The release engine archive for this machine.

    Upstream publishes ready-made engines for Windows (plain, CUDA 12, HIP); on Linux
    setup.py compiles the engine on the box. When there is no archive for this OS the
    Build tab says so and offers the source build instead.
    """
    names = [a["name"] for a in release.get("assets", [])]
    want = "strata-windows-x64.zip" if IS_WIN else "strata-linux-x64.zip"
    for name in names:
        if name == want:
            return next(a for a in release["assets"] if a["name"] == name)
    for name in names:
        if name.endswith(".zip") and ("windows" if IS_WIN else "linux") in name:
            return next(a for a in release["assets"] if a["name"] == name)
    return None


# ---------------------------------------------------------------- the build job
class BuildJob:
    """One clone -> configure -> build -> transfer. Only one may run at a time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"          # idle | cloning | configuring | building | transferring | done | failed
        self.log: list[str] = []
        self.log_path: Path | None = None   # the same lines, on disk, for a tail reader
        self.version: str | None = None
        self.quant: str | None = None     # the quant this build is for, if one was named
        self.started: float | None = None
        self.result: dict[str, Any] | None = None
        self.proc: subprocess.Popen | None = None

    def cancel(self) -> dict[str, Any]:
        """Stop a build in progress. tmp/ is left in place; the next build removes it."""
        if self.proc and self.state not in ("idle", "done", "failed"):
            self.proc.terminate()
            self.state = "cancelled"
            self.log_line("build cancelled")
            return {"cancelled": True}
        return {"cancelled": False}

    def log_line(self, line: str) -> None:
        self.log.append(line)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # the in-memory log is the record; a log file is a convenience

    def tail(self, offset: int = 0) -> dict[str, Any]:
        """The build log from a byte offset onward.

        The tab polls this while a build runs. Reading the whole 400-line log every
        poll is the difference between a tab that streams and one that re-sends.
        """
        if not self.log_path:
            return {"lines": [], "offset": 0, "bytes": 0}
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return {"lines": [], "offset": 0, "bytes": 0}
        offset = max(0, min(int(offset), len(data)))
        chunk = data[offset:]
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            chunk = chunk[:cut + 1]     # stop at a complete line; the next read starts clean
        return {"lines": [line.decode("utf-8", errors="replace").rstrip() for line in chunk.splitlines()],
                "offset": offset + len(chunk), "bytes": len(data)}

    def run_stream(self, cmd: list[str]) -> int:
        self.log_line("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
        for line in proc.stdout:
            self.log_line(line.rstrip())
        proc.wait()
        return proc.returncode

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "version": self.version,
            "quant": self.quant,
            "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
            "log": self.log[-400:],
            "result": self.result,
        }

    def clean_tmp(self) -> None:
        """Only one Strata clone may exist in tmp/."""
        for path in TMP_DIR.iterdir():
            if path.is_dir() and path.name.startswith("strata-"):
                shutil.rmtree(path, ignore_errors=True)

    def build_targets(self, build_dir: Path) -> bool:
        """Build the three targets in one cmake invocation.

        Returns whether the HyperCore worker was built. The interface is ours and the
        engine is upstream's: when the worker target is what fails against a given
        Strata revision, strata and strata-vision are still buildable, and the server
        drives the engine over its stdin/stdout protocol instead (serve/direct_engine.py).
        The fallback is logged and flagged in engine/BUILD.json so the Monitor can say
        "worker: not built" rather than reporting the whole build as failed.
        """
        nproc = str(os.cpu_count() or 4)
        # one --target with the list: repeating the flag keeps only the last one
        code = self.run_stream(["cmake", "--build", str(build_dir),
                               "--target", "strata", "strata-vision", "hypercore-worker",
                               "-j", nproc])
        if code == 0:
            return True
        self.log_line("the hypercore-worker target failed; building strata and strata-vision only")
        code = self.run_stream(["cmake", "--build", str(build_dir),
                               "--target", "strata", "strata-vision", "-j", nproc])
        if code != 0:
            raise RuntimeError("the engine build failed")
        self.log_line("worker: not built - the engine runs in direct mode")
        return False

    def transfer_binaries(self, build_dir: Path, meta: dict[str, Any],
                         quant: str | None) -> tuple[list[str], str | None]:
        """Move the binaries a build produced into the store.

        With a quant named they are kept under engine/versions/<quant>/ and that version
        is made active, so a second quant can be built without losing the first. Without
        one they are simply the default engine in engine/. Returns the files placed and
        the key they were stored under.
        """
        built = [build_dir / name for name in (EXE, VEXE, WORKER) if (build_dir / name).is_file()]
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        key = normalise_or_none(quant)
        if quant and key is None:
            self.log_line(f"quant {quant!r} is not a valid name. "
                          "The build is stored as the default engine instead.")
        copied: list[str] = []
        if key:
            # engine/strata is made to point at this build, so every run config that
            # names engine/strata keeps working unchanged.
            meta["quant"] = key
            meta["label"] = key
            entry = quant_store.store_binaries(key, built, meta)
            copied = sorted(entry["files"])
            self.log_line(f"stored engine/versions/{key}: {', '.join(copied)}")
            chosen = quant_store.select(key)
            self.log_line(f"active engine: {chosen['note']}"
                         if chosen["active"] else f"could not activate {key}: {chosen['note']}")
        else:
            for src in built:
                shutil.copy2(src, ENGINE_DIR / src.name)
                copied.append(src.name)
            (ENGINE_DIR / "BUILD.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return copied, key

    def start(self, tag: str, options: dict[str, Any], quant: str | None = None) -> dict[str, Any]:
        """Start a build. `quant` names the quant this engine is built for, so it is
        stored under engine/versions/<quant> instead of overwriting the engine in engine/.
        """
        with self.lock:
            if self.state not in ("idle", "done", "failed"):
                return {"error": "a build is already running"}
            self.state = "cloning"
            self.log = []
            (ROOT / "logs").mkdir(parents=True, exist_ok=True)
            self.log_path = ROOT / "logs" / f"build-{tag}.log"
            self.version = tag
            self.quant = normalise_or_none(quant)
            self.started = time.time()
            self.result = None
        threading.Thread(target=self._run, args=(tag, options, self.quant), daemon=True).start()
        return self.status()

    def _run(self, tag: str, options: dict[str, Any], quant: str | None = None) -> None:
        clone = TMP_DIR / f"strata-{tag}"
        try:
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            self.clean_tmp()

            self.state = "cloning"
            code = self.run_stream(["git", "clone", "--depth", "1", "--branch", tag,
                                   f"https://github.com/{REPO}.git", str(clone)])
            if code != 0:
                raise RuntimeError("git clone failed")

            # Inject the HyperCore process boundary so the worker targets exist.
            engine_src = ROOT / "engine-src"
            if engine_src.is_dir():
                shutil.copytree(engine_src, clone / "hypercore-interface", dirs_exist_ok=True)
                self.log_line("injected hypercore-interface (worker + shared library)")

            self.state = "configuring"
            prev = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else None
            sys.path.insert(0, str(ROOT / "tools"))
            from extract_build_options import (  # a local tool, not a package
                diff,
                manifest,
            )
            m = manifest(clone)
            if prev:
                m["changes"] = diff(prev, m)
            MANIFEST.parent.mkdir(parents=True, exist_ok=True)
            MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")
            if m.get("changes"):
                c = m["changes"]
                self.log_line(f"options changed from {c.get('from')} to {c.get('to')}: "
                              f"+{len(c.get('added_options', []))} -{len(c.get('removed_options', []))} "
                              f"~{len(c.get('changed_defaults', []))}")

            defs = [f"-D{key}={value}" for key, value in options.items()]
            build_dir = clone / "build"
            self.state = "building"
            code = self.run_stream(["cmake", "-G", "Ninja", "-S", str(clone), "-B", str(build_dir), *defs])
            if code != 0:
                raise RuntimeError("cmake configure failed")
            worker_built = self.build_targets(build_dir)

            self.state = "transferring"
            ENGINE_DIR.mkdir(parents=True, exist_ok=True)
            meta = {
                "source": "built-from-source",
                "version": m.get("version") or tag,
                "tag": tag,
                "archs": options.get("STRATA_CUDA_ARCH_LIST", ""),
                "worker": worker_built,
                "built_at": _now(),
                "src": hashlib.sha256((clone / "CMakeLists.txt").read_bytes()).hexdigest()[:16],
                "options": options,
            }
            copied, key = self.transfer_binaries(build_dir, meta, quant)

            self.clean_tmp()          # tmp/ is scratch: nothing at runtime reads it
            self.result = {"copied": copied, "version": meta["version"], "quant": key}
            self.state = "done"
        except Exception as exc:  # noqa: BLE001 - report it to the tab, do not take the server down
            self.state = "failed"
            self.log_line(f"error: {exc}")
            self.result = {"error": str(exc)}

    def fetch_prebuilt(self, release: dict[str, Any]) -> dict[str, Any]:
        """Download the ready-made engine for this OS and unpack it into engine/."""
        asset = asset_for(release)
        if not asset:
            return {"error": "this release has no engine archive for this operating system"}
        zip_path = TMP_DIR / asset["name"]
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(asset["url"], zip_path)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"download failed: {exc}"}
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        expected = published_digests(release).get(asset["name"])
        if expected and expected != digest:
            zip_path.unlink(missing_ok=True)
            return {"error": f"the downloaded archive does not match the published SHA-256 ({expected[:16]}...)"}
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(ENGINE_DIR)
        zip_path.unlink(missing_ok=True)
        meta = {"source": "prebuilt", "version": release.get("version"), "tag": release.get("tag"),
               "asset": asset["name"], "sha256": digest, "built_at": _now()}
        (ENGINE_DIR / "BUILD.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return {"ok": True, "version": release.get("version"), "sha256": digest, "files": sorted(
            p.name for p in ENGINE_DIR.iterdir())}


JOB = BuildJob()
