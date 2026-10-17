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

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
TMP_DIR = ROOT / "tmp"
MANIFEST = ROOT / "config" / "build_options.json"
REPO = "Niko1221/Strata"
IS_WIN = os.name == "nt"
EXE = "strata.exe" if IS_WIN else "strata"
VEXE = "strata-vision.exe" if IS_WIN else "strata-vision"
WORKER = "hypercore-worker.exe" if IS_WIN else "hypercore-worker"


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
        self.version: str | None = None
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
            "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
            "log": self.log[-400:],
            "result": self.result,
        }

    def clean_tmp(self) -> None:
        """Only one Strata clone may exist in tmp/."""
        for path in TMP_DIR.iterdir():
            if path.is_dir() and path.name.startswith("strata-"):
                shutil.rmtree(path, ignore_errors=True)

    def start(self, tag: str, options: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.state not in ("idle", "done", "failed"):
                return {"error": "a build is already running"}
            self.state = "cloning"
            self.log = []
            self.version = tag
            self.started = time.time()
            self.result = None
        threading.Thread(target=self._run, args=(tag, options), daemon=True).start()
        return self.status()

    def _run(self, tag: str, options: dict[str, Any]) -> None:
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
            from extract_build_options import diff, manifest  # a local tool, not a package
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
            targets = ["strata", "strata-vision", "hypercore-worker"]
            # one --target with the list: repeating the flag keeps only the last one
            code = self.run_stream(["cmake", "--build", str(build_dir),
                                   "--target", *targets,
                                   "-j", str(os.cpu_count() or 4)])
            if code != 0:
                raise RuntimeError("the engine build failed")

            self.state = "transferring"
            ENGINE_DIR.mkdir(parents=True, exist_ok=True)
            copied = []
            for target, name in (("strata", EXE), ("strata-vision", VEXE), ("hypercore-worker", WORKER)):
                src = build_dir / name
                if src.is_file():
                    shutil.copy2(src, ENGINE_DIR / name)
                    copied.append(name)
            meta = {
                "source": "built-from-source",
                "version": m.get("version") or tag,
                "tag": tag,
                "archs": options.get("STRATA_CUDA_ARCH_LIST", ""),
                "built_at": _now(),
                "src": hashlib.sha256((clone / "CMakeLists.txt").read_bytes()).hexdigest()[:16],
            }
            (ENGINE_DIR / "BUILD.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

            self.clean_tmp()          # tmp/ is scratch: nothing at runtime reads it
            self.result = {"copied": copied, "version": meta["version"]}
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
