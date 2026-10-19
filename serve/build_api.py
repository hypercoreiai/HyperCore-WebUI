"""serve/build_api.py - the Build tab: check for a new Strata, get it, build it.

Two ways to update the engine, both offered:

  ready-made   download the release asset for this OS, verify its published
               SHA-256, unpack into engine/. Seconds.
  from source  shallow-clone the release into tmp/, inject engine-src/ so the
               HyperCore worker targets exist, configure with the options the
               Build tab's dropdowns came from, build strata + strata-vision +
               hypercore-worker, copy them into engine/, merge the clone's data/ into
               the project's data/, leave tmp/ as it is.

tmp/ holds exactly one clone at a time. It is no longer deleted when a build
finishes: the clone's data/ folder (expert-profile.bin and the other runtime files)
is what a run config points at, and removing the clone removes them. Nothing at
runtime reads it, so a kept clone is only ever used by the next build.

The version manifest is extracted from the clone itself (tools/extract_build_options.py),
never from a list we maintain, so a release that adds an option shows up in the
dropdowns the moment it is cloned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from serve import quant_store, strata_hook
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


def quant_from_choices(choices: Any, clone: Path | None = None) -> str | None:
    """The quant the Setup choices name, when the quant box was left empty.

    The card's model box holds a quant name (Q2_0, IQ2_XS). A build started from those
    boxes is for that model, so it is stored in engine/engine_<quant>/ and gets its own
    config/run-<quant>.json instead of overwriting the default engine - the same model
    "Run setup with these" would set up. None when no model was named.

    When the box names a model rather than a quant, the run config already in `clone` is
    the fallback: that is the quant a previous setup run on this clone chose, and a build
    of that clone is the same model, so it belongs in engine/engine_<quant>/ too.
    """
    flags = quant_store.choice_map(choices) if isinstance(choices, (list, tuple)) else (choices or {})
    key = normalise_or_none(flags.get("--model"))
    if key is None and clone is not None:
        key = quant_store.quant_from_checkout(clone)
    return key


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- the device on the box
#: Upstream defines the engine only inside a GPU branch: the `strata` executable is
#: added under `if(STRATA_ENABLE_CUDA OR STRATA_ENABLE_HIP)` in CMakeLists.txt, and the
#: SYCL engine comes from sycl/CMakeLists.txt. A configuration with all three off has
#: no engine target at all, and ninja answers "unknown target 'strata'". These are the
#: flags the box in front of us can actually build.
ENGINE_TARGETS = ("strata", "strata-vision", "hypercore-worker")
WORKER_TARGET = "hypercore-worker"
GPU_BACKENDS = ("STRATA_ENABLE_CUDA", "STRATA_ENABLE_HIP", "STRATA_ENABLE_SYCL")


#: Upstream has no worker target. The interface this project ships is only compiled if
#: the clone's own project includes it, so the Build tab appends the line. Idempotent:
#: a clone that already mentions it is left alone.
INTERFACE_LINE = "add_subdirectory(hypercore-interface)"


def wire_in_interface(clone: Path) -> bool:
    """Add hypercore-interface to a clone's CMake project. Returns whether it is there."""
    cmake = clone / "CMakeLists.txt"
    if not cmake.is_file():
        return False
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if INTERFACE_LINE in text:
        return True
    try:
        cmake.write_text(text.rstrip() + "\n\n# added by the Build tab: upstream has no worker target\n"
                         + INTERFACE_LINE + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _llama_source(clone: Path) -> Path:
    """The llama.cpp checkout the clone has.

    Upstream keeps it in third_party/ only when it was vendored; a fresh clone fetches
    it into build/_deps/*-src. The vision encoder links that same ggml, so pointing it
    at the checkout the engine used is what makes the two agree.
    """
    vendored = clone / "third_party" / "llama.cpp"
    if (vendored / "CMakeLists.txt").is_file():
        return vendored
    for candidate in sorted((clone / "build" / "_deps").glob("*-src")):
        if (candidate / "CMakeLists.txt").is_file():
            return candidate
    return vendored


def _probe(cmd: list[str], timeout: int = 15) -> str:
    """The stdout of a short probe, or "" when the tool is not there or fails."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=False).stdout
    except Exception:  # noqa: BLE001 - a missing probe describes a device, it is not an error
        return ""


def cuda_toolkit() -> str:
    """The directory holding nvcc, or "" when no CUDA toolkit is installed."""
    nvcc = shutil.which("nvcc")
    if nvcc:
        return str(Path(nvcc).parent)
    for cand in sorted(Path("/usr/local").glob("cuda*")):
        if (cand / "bin" / "nvcc").is_file():
            return str(cand / "bin")
    return ""


def cuda_arch() -> str:
    """The compute capability of the NVIDIA card, as CMake wants it ("89").

    nvidia-smi first; then the arch of the engine already in engine/, which is the
    card this project has been running on; then "native", which CMake resolves
    against the GPU present at configure time.
    """
    for line in _probe(["nvidia-smi", "--query-gpu=compute_cap",
                       "--format=csv,noheader"]).splitlines():
        cap = line.strip().replace(".", "")
        if cap.isdigit() and int(cap) >= 75:
            return cap
    for arch in installed_version().get("archs") or []:
        value = str(arch).split("-")[0]
        if value.isdigit() and int(value) >= 75:
            return value
    return "native"


def device_defaults() -> dict[str, Any]:
    """What this machine can build an engine for: {device, arch, defs}.

    `defs` are the cmake flags to add. Nothing is guessed about a card we cannot see:
    with no CUDA, HIP or oneAPI compiler on the box the answer is "none" and the Build
    tab says so instead of running a build that cannot produce an engine.
    """
    toolkit = cuda_toolkit()
    if toolkit:
        arch = cuda_arch()
        # CMake only finds nvcc when it is on PATH; a toolkit installed under
        # /usr/local/cuda is not, so the compiler path is named explicitly.
        return {"device": "cuda", "toolkit": toolkit, "arch": arch,
                "defs": {"STRATA_ENABLE_CUDA": "ON",
                        "CMAKE_CUDA_ARCHITECTURES": arch,
                        "CMAKE_CUDA_COMPILER": str(Path(toolkit) / "nvcc")}}
    if shutil.which("rocminfo") or shutil.which("hipcc"):
        return {"device": "hip", "arch": "",
                "defs": {"STRATA_ENABLE_HIP": "ON"}}
    if shutil.which("icpx"):
        return {"device": "sycl", "arch": "",
                "defs": {"STRATA_ENABLE_SYCL": "ON"}}
    return {"device": "none", "arch": "", "defs": {}}


def device_label(device: dict[str, Any]) -> str:
    """One line a person can act on: 'CUDA 13 toolkit, sm_89', or why there is none."""
    if device["device"] == "cuda":
        return f"CUDA toolkit at {device['toolkit']}, GPU arch {device['arch']}"
    if device["device"] in ("hip", "sycl"):
        return f"{device['device']} toolchain"
    return "no CUDA, HIP or oneAPI toolchain found"


#: Upstream's CMakeLists.txt refuses some combinations with a FATAL_ERROR before a
#: single file is compiled. The guards in the clone being built: :97 PREFILL_MMQ needs HIP,
#: :1413 PREFILL_MMQ needs NATIVE_EXPERTS (it is compiled from the GGML sources native
#: experts pull in), :1416 ORCA_Q4KS_MMQ the same, :1612 the MMQ pointer table is built
#: only in the CUDA branch, and :17, :67, :101 say the GPU backends exclude one another.
#: The tab applies the same rules before cmake is called, so what it shows and what it
#: sends is a configuration cmake accepts: a feature this box cannot build is turned off
#: with the reason in the log instead of a CMake error.
BACKEND_REQUIREMENTS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("STRATA_PREFILL_MMQ", ("STRATA_ENABLE_HIP",), "the GGML MMQ prefill path is a HIP target"),
    ("STRATA_PREFILL_MMQ", ("STRATA_NATIVE_EXPERTS",), "it is compiled from the GGML sources"),
    ("STRATA_ORCA_Q4KS_MMQ", ("STRATA_NATIVE_EXPERTS",), "it is compiled from the GGML sources"),
    ("STRATA_MMQ_TAB", ("STRATA_ENABLE_CUDA",), "the pointer-table MMQ scheduler is a CUDA target"),
)


def _is_on(value: Any) -> bool:
    """Whether cmake would read this value as ON. Anything else is off."""
    return str(value).strip().lower() in ("on", "true", "1", "yes")


def resolve_build_options(defs: dict[str, Any], device: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The configuration this machine can build, and what had to change to get there.

    `defs` is what the tab has picked, `device` what the box has a toolchain for. Only
    the combinations upstream refuses are touched, and every change is returned as a
    line the log can show, so an option the user left on is not silently dropped.
    """
    out = dict(defs)
    notes: list[str] = []

    picked = [name for name in GPU_BACKENDS if _is_on(out.get(name))]
    if len(picked) > 1:
        keep = next((name for name in GPU_BACKENDS
                     if _is_on((device.get("defs") or {}).get(name))), picked[0])
        for name in picked:
            if name != keep:
                out[name] = "OFF"
                notes.append(f"{name} off: upstream builds one backend at a time, and "
                            f"{keep} is the toolchain this machine has")

    for name, needed, reason in BACKEND_REQUIREMENTS:
        if not _is_on(out.get(name)):
            continue
        missing = [flag for flag in needed if not _is_on(out.get(flag))]
        if missing:
            out[name] = "OFF"
            notes.append(f"{name} off: it needs "
                        f"{' and '.join(f'{flag}=ON' for flag in missing)} ({reason})")
    return out, notes


def sent_options(device_defs: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """The flags to hand to cmake: this machine's defaults, with what the tab chose on top.

    A field the tab left empty is not a choice, so it must not overwrite the backend this
    box has a toolchain for: an empty value is what a form sends for a switch nobody
    touched, and an empty STRATA_ENABLE_CUDA is what made a build configure with no engine
    target in it. A value the tab did send, including an explicit OFF, always wins.
    """
    chosen = dict(device_defs)
    for key, value in (options or {}).items():
        if value is None or str(value).strip() == "":
            continue
        chosen[key] = value
    return chosen


def with_device_defaults(options: dict[str, Any]) -> dict[str, Any]:
    """The release's build options, with this box's defaults filled in.

    The manifest's defaults are upstream's, and every GPU backend defaults to OFF. Left
    as they are, the Build tab configures a build that has no engine in it. The defaults
    shown before a build starts are the ones this machine can build, so what the tab
    shows is what cmake is given.
    """
    if not isinstance(options, dict) or options.get("error"):
        return options   # a release we could not read has no defaults to fill in
    device = device_defaults()
    cmake = [dict(o) for o in (options.get("cmake_options") or []) if isinstance(o, dict)]
    have = {o.get("name") for o in cmake}
    for name, value in device["defs"].items():
        if name in have:
            for o in cmake:
                if o.get("name") == name:
                    o["default"] = value
        else:
            # the arch is a choice a person can see and change; the compiler path is
            # housekeeping the tab fills in for them
            visible = name == "CMAKE_CUDA_ARCHITECTURES"
            cmake.append({"name": name, "kind": "string", "default": value,
                         "help": f"the GPU architecture this build is compiled for "
                                 f"({device_label(device)})",
                         "visible": visible})
    # upstream errors out on a feature whose backend is off, so the defaults shown are
    # the ones cmake will accept on this box, and a switch that cannot be built here
    # says in its help what it would need
    defaults = {o["name"]: o.get("default") for o in cmake if o.get("name")}
    legal, _ = resolve_build_options(defaults, device)
    for o in cmake:
        name = o.get("name")
        needs = [flag for target, needed, _ in BACKEND_REQUIREMENTS
                 if target == name for flag in needed if not _is_on(legal.get(flag))]
        if needs:
            o["requires"] = needs
            o["help"] = (f"{o.get('help', '')} [needs "
                        f"{' and '.join(f'{flag}=ON' for flag in needs)} on this machine]")
        if name in legal and str(legal[name]) != str(o.get("default")):
            o["default"] = legal[name]
    return {**options, "cmake_options": cmake, "device": device, "device_label": device_label(device)}


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


def version_tuple(value: Any) -> tuple[int, ...] | None:
    """A version as numbers: '0.1.45.3' -> (0, 1, 45, 3), 'v0.2.0-beta' -> (0, 2, 0).

    A trailing label ('-beta') is a pre-release marker, not a version part, so it is
    dropped. Anything without a leading number is not a version and returns None.
    """
    parts: list[int] = []
    for piece in str(value or "").strip().lstrip("vV").split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts) if parts else None


def is_newer(newer: Any, older: Any) -> bool:
    """Whether one Strata version is genuinely ahead of another.

    0.1.45.3 is a patch of 0.1.45, so a string compare ("0.1.45.3" != "0.1.45") would
    tell an engine that is already ahead that it needs an update. When either side is
    not a parseable version there is nothing to compare, so the old "different" rule
    still applies rather than guessing.
    """
    left, right = version_tuple(newer), version_tuple(older)
    if left is None or right is None:
        return str(newer) != str(older)
    return left > right


#: A folder in tmp/ is named for the tag it was cloned at (tmp/strata-v0.1.45), but the
#: release it holds is the one its own CMakeLists.txt declares: on this machine
#: tmp/strata-v0.2.0-beta declares project(strata VERSION 0.1.45.3). Both are reported, so
#: the card can name the tree the choices came from rather than the folder it happens to
#: be sitting in.
CLONE_PREFIX = "strata-"
CLONE_TAG_RE = re.compile(r"^strata-(.+)$", re.IGNORECASE)
PROJECT_VERSION_RE = re.compile(r"project\(\s*(\w+)\s+VERSION\s+([0-9.]+)", re.IGNORECASE)


def shown_path(path: Path) -> str:
    """A path as the page shows it: relative to the project when it is inside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _checkout_record(path: Path, source: str) -> dict[str, Any]:
    """One release entry, read from the two files the setup choices come from.

    Returns {} for a folder that is not a checkout - a manifest folder, a data folder,
    or scratch - so the caller skips it. The version is what the checkout's own
    CMakeLists.txt declares, never the folder name; the folder name only gives the tag.
    """
    cmake, setup = path / "CMakeLists.txt", path / "setup.py"
    if not cmake.is_file() and not setup.is_file():
        return {}
    version: str | None = None
    if cmake.is_file():
        try:
            match = PROJECT_VERSION_RE.search(cmake.read_text(encoding="utf-8", errors="replace"))
            version = match.group(2) if match else None
        except OSError:
            version = None      # unreadable: reported as a checkout with no version
    tag = CLONE_TAG_RE.search(path.name)
    try:
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    except OSError:
        modified = ""
    return {
        "tag": tag.group(1) if tag else path.name,
        "version": version,
        "path": str(path),
        "folder": shown_path(path),
        "source": source,
        "has_setup": setup.is_file(),
        "has_cmake": cmake.is_file(),
        "git": (path / ".git").is_dir(),
        "modified": modified,
    }


def tmp_releases() -> list[dict[str, Any]]:
    """The checkouts under tmp/, in folder-name order."""
    if not TMP_DIR.is_dir():
        return []
    try:
        entries = sorted((p for p in TMP_DIR.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []
    return [r for r in (_checkout_record(p, "tmp") for p in entries) if r]


def searched_releases(run_config: str | Path | None = None) -> list[dict[str, Any]]:
    """The checkouts outside tmp/, in the order serve/strata_hook.py would find them.

    A Strata the user keeps elsewhere is a release a setup run can be started from, so
    the scan has to offer it: the hook finds it through the environment, the folder a run
    config starts the engine from, or the folders beside this project. A folder that is
    not a checkout is skipped, and a path already counted under tmp/ is not counted twice.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in strata_hook.search_candidates(run_config):
        try:
            key = str(path.resolve())
        except OSError:
            continue            # a candidate that cannot be resolved is not a checkout
        if key in seen:
            continue
        record = _checkout_record(path, "checkout")
        if not record:
            continue
        seen.add(key)
        out.append(record)
    return out


def local_releases() -> list[dict[str, Any]]:
    """The Strata checkouts already on this machine, newest first.

    A build no longer deletes its clone (loop 41: the clone's data/ is what a run config
    points at), so a machine that has built 0.1.45 and 0.2.0-beta has both trees under
    tmp/. The Build tab's dropdown was filled only from GitHub's listing, so the release
    the setup choices were read from and the release a build would actually use could be
    two different trees, and there was no way to pick between them. This is the scan that
    makes the local ones selectable.

    A checkout the user keeps outside tmp/ is included too, because that is the tree the
    hook would run setup.py from (see searched_releases). tmp/ entries come first, so a
    tree that is both is reported as the clone a build would use.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in tmp_releases() + searched_releases():
        try:
            key = str(Path(record["path"]).resolve())
        except OSError:
            key = record["path"]
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return sorted(out, key=lambda r: (version_tuple(r["version"]) or (0,), str(r["tag"])), reverse=True)


def local_clone_for(tag: Any) -> Path | None:
    """The checkout in tmp/ a tag names, or None when this machine has no such tree.

    The tag is the folder name as local_releases reports it, with or without the leading
    v. A folder with neither of the two files is not a checkout and is not returned, so a
    build asked for it still goes to GitHub rather than building from a tree it cannot
    describe.
    """
    want = str(tag or "").strip().lstrip("vV").lower()
    if not want or not TMP_DIR.is_dir():
        return None
    try:
        entries = sorted((p for p in TMP_DIR.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return None
    for path in entries:
        name = path.name.lower()
        if not name.startswith(CLONE_PREFIX) or name[len(CLONE_PREFIX):].lstrip("vV") != want:
            continue
        if (path / "CMakeLists.txt").is_file() or (path / "setup.py").is_file():
            return path
    return None


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
    # The manifest holds upstream's defaults, and every GPU backend defaults to OFF, so the
    # fields the tab builds from it would send a configuration with no engine in it. The
    # device defaults options_for() applies are applied here too: what the tab shows has to
    # be what cmake is given, otherwise a build on a CUDA box configures a CPU-only tree.
    options = with_device_defaults(options) if options else options
    if options and not options.get("from"):
        # The manifest names the folder it was extracted from but not that it is a local
        # file, so the card needs the word for it.
        options = {**options, "from": "the manifest the last build wrote"}
    return {
        "installed": installed,
        "latest": listing,
        "newest": newest,
        "update_available": bool(newest and have and is_newer(newest.get("version"), have)),
        "options": options,
        # The checkouts this machine already has, so the tab can offer them as build
        # sources and not only the releases GitHub publishes.
        "local": local_releases(),
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
    CMakeLists.txt and setup.py of the tag asked for. Three sources, in the order the tab
    should trust them:

      1. a checkout this machine already has in tmp/ for that tag - it is the tree a
         build of that tag would actually use, so it is the one the choices must come
         from when the user picked it in the dropdown;
      2. the manifest the last build wrote, when it was written from that same release;
      3. GitHub, without cloning.

    `from` names which of the three the answer came from, so the card can say it: a card
    that lists one release's flags under another release's name is what this function used
    to produce, and the second source is exactly that case.
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

    sys.path.insert(0, str(ROOT / "tools"))
    from extract_build_options import diff, manifest  # a local tool, not a package

    clone = local_clone_for(tag)
    if clone is not None:
        try:
            result = manifest(clone)
        except Exception as exc:  # noqa: BLE001 - a tree we cannot parse is reported, not raised
            result = {"error": f"the checkout in tmp/{clone.name} could not be read: {exc}",
                      "tag": tag, "cmake_options": [], "setup_choices": [], "from": "unreadable"}
        else:
            result["tag"] = tag
            result["from"] = f"the checkout in {shown_path(clone)}"
            if local and local.get("version") != result.get("version"):
                result["changes"] = diff(local, result)
        result = with_device_defaults(result)
        _options_cache[tag] = result
        return result

    if local and (local.get("version") or "") == tag.lstrip("v"):
        result = with_device_defaults({**local, "tag": tag,
                                      "from": "the last build of this release"})
        _options_cache[tag] = result
        return result

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
    result = with_device_defaults(result)
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
    # Which release these groups belong to and where it was read from, carried through so
    # the card can name them: the flags of one release shown under another release's name
    # is what the card used to do.
    return {"groups": groups, "hidden": hidden, "total": len(choices),
            "tag": options.get("tag") or options.get("version"),
            # What the release declares, which is not always the name it was fetched as:
            # tmp/strata-v0.2.0-beta declares 0.1.45.3. The card titles itself with this.
            "version": options.get("version") or options.get("tag"),
            "from": options.get("from")}


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


#: The archive shapes a release can carry the engine in. 0.1.45 shipped one zip per
#: OS; 0.2.0 ships a per-toolchain engine bundle as tar.gz beside the app installers
#: and the updater. The pick is by content, never by a name list we maintain.
ENGINE_ARCHIVE_SUFFIXES: tuple[str, ...] = (".zip", ".tar.gz", ".tgz")
#: An archive that is the app, the updater or the terminal - not the engine.
NOT_ENGINE_ASSETS: tuple[str, ...] = ("update", "updater", "terminal", "installer", "app.", "app-")
#: The words upstream uses for a GPU backend in an asset name.
BACKEND_WORDS: dict[str, tuple[str, ...]] = {"cuda": ("cuda",), "hip": ("hip", "rocm"),
                                            "sycl": ("sycl", "oneapi", "intel")}


def is_engine_archive(name: Any) -> bool:
    """Whether a release asset is an engine archive rather than an app or an update."""
    lowered = str(name or "").lower()
    if not lowered.endswith(ENGINE_ARCHIVE_SUFFIXES):
        return False
    return not any(word in lowered for word in NOT_ENGINE_ASSETS)


def backend_of(name: Any) -> str | None:
    """The GPU backend an asset name advertises, or None for a plain archive."""
    lowered = str(name or "").lower()
    for backend, words in BACKEND_WORDS.items():
        if any(word in lowered for word in words):
            return backend
    return None


def cuda_major(text: Any) -> str | None:
    """The CUDA major version in a toolkit path or an asset name: 'cuda-13.4/bin' -> '13'."""
    match = re.search(r"cuda[-_.]?(\d+)", str(text or "").lower())
    return match.group(1) if match else None


def asset_for(release: dict[str, Any], device: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The release asset that carries the engine for this machine.

    Upstream changed the shape of a release: 0.1.45 published one zip per OS
    (strata-linux-x64.zip, strata-windows-x64-cuda12.zip), while 0.2.0 publishes a
    per-toolchain engine bundle (strata-engine-0.1.46-linux-x86_64-cuda-cuda13.tar.gz)
    next to the app installers and the updater artifact. Nothing here is keyed to a
    release number: an asset is a candidate when it is an archive and not an app or an
    update, and the best one is the archive for this OS whose backend and CUDA major
    version match the toolchain this box has. When the OS is not covered at all the
    Build tab says so and offers the source build.
    """
    device = device or device_defaults()
    want_backend = device.get("device") or "none"
    want_cuda = cuda_major(device.get("toolkit") or "")
    os_word = "windows" if IS_WIN else "linux"

    def rank(asset: dict[str, Any]) -> tuple[int, int, int, int]:
        name = str(asset.get("name") or "").lower()
        on_os = 1 if os_word in name else 0
        backend = backend_of(name)
        if backend == want_backend:
            backend_score = 2
        elif backend is None:
            # a toolchain-free archive: usable when the box has no GPU compiler
            backend_score = 1 if want_backend in ("none", "cpu") else 0
        else:
            backend_score = 0
        cuda = cuda_major(name)
        tool_score = 2 if want_cuda and cuda == want_cuda else (1 if not want_cuda and cuda is None else 0)
        bundle = 1 if name.startswith("strata-engine-") else 0
        return (on_os, backend_score, tool_score, bundle)

    candidates = [a for a in release.get("assets", []) if is_engine_archive(a.get("name"))]
    if not candidates:
        return None
    best = max(candidates, key=rank)
    if not rank(best)[0]:
        return None                      # nothing published for this operating system
    if want_backend in ("none", "cpu") and backend_of(best.get("name")) is not None:
        return None                      # every archive is for a GPU this box does not have
    return best


def _safe_member(name: Any) -> bool:
    """Whether an archive member can be written inside the folder it is unpacked into.

    An archive member is attacker-supplied once it is downloaded: a member named
    `../strata` would write into the repo instead of the engine folder.
    """
    text = str(name or "")
    if not text or text.startswith("/"):
        return False
    return all(part != ".." for part in text.replace("\\", "/").split("/"))


def unpack_archive(path: Path, dest: Path) -> list[str]:
    """Unpack a release archive (zip or tar.gz) into `dest` and return the file names.

    Members that would land outside `dest` are refused before anything is written.
    """
    dest.mkdir(parents=True, exist_ok=True)
    lowered = str(path).lower()
    if lowered.endswith((".tar.gz", ".tgz")):
        import tarfile
        with tarfile.open(path, "r:gz") as archive:
            members = [m for m in archive.getmembers() if m.isfile()]
            unsafe = [m.name for m in members if not _safe_member(m.name)]
            if unsafe:
                raise ValueError(f"the archive carries members outside its folder: {', '.join(unsafe[:3])}")
            for member in members:
                archive.extract(member, dest, filter="data")
        return [m.name for m in members]
    import zipfile
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n and not n.endswith("/")]
        unsafe = [n for n in names if not _safe_member(n)]
        if unsafe:
            raise ValueError(f"the archive carries members outside its folder: {', '.join(unsafe[:3])}")
        archive.extractall(dest)
    return names


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
        self.choices: dict[str, str] = {}  # the Setup-choice boxes, as {flag: value}
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
            "choices": self.choices,
            "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
            "log": self.log[-400:],
            "result": self.result,
        }

    def merge_clone_data(self, clone: Path) -> dict[str, list[str]]:
        """Merge the clone's data/ into the project's data/ and report it in the log.

        The build log is where a build's result is read, so the merge says what it put
        there and what it refused to overwrite. Nothing is raised: a clone with no data
        folder is a build that shipped nothing, which is not an error.
        """
        merged = quant_store.merge_data(clone)
        if merged["added"]:
            self.log_line(f"merged data/: {len(merged['added'])} file(s) from the clone")
        if merged["skipped"]:
            self.log_line(f"data/: {len(merged['skipped'])} file(s) already there and "
                          "different, kept as they were")
        return merged

    def clean_tmp(self) -> None:
        """Leave the Strata clone in tmp/ instead of removing it.

        The call sites are kept so the build flow is unchanged; the removal itself is
        commented out. A build that finds its clone already there uses it again - see
        reuse_clone, which is what makes a rebuild of the same tag possible.
        """
        # for path in TMP_DIR.iterdir():
        #     if path.is_dir() and path.name.startswith("strata-"):
        #         shutil.rmtree(path, ignore_errors=True)

    def reuse_clone(self, clone: Path, tag: str) -> bool:
        """Use the clone a previous build left in tmp/ instead of cloning it again.

        clean_tmp no longer removes the clone, so a second build of the same tag finds the
        checkout there and git refuses to clone into a folder that already holds one. The
        clone is never edited by hand - the build only injects hypercore-interface into it,
        and that is idempotent - so fetching the tag again and resetting to it is enough,
        and the build tree inside it is left, so the cmake run stays incremental.

        A folder in tmp/ that is not a git checkout is not a clone: returns False, and the
        caller names it instead of building from a tree it cannot identify.
        """
        if not (clone / ".git").is_dir():
            return False
        for cmd in (["git", "-C", str(clone), "fetch", "--depth", "1", "origin", tag],
                    ["git", "-C", str(clone), "checkout", "-f", tag]):
            if self.run_stream(cmd) != 0:
                return False
        return True

    def configured_targets(self, build_dir: Path) -> set[str]:
        """The target names a configured project defines, read from its build.ninja.

        Returns an empty set when build.ninja is not there or cannot be read; the
        caller then uses the upstream target names rather than guessing about a project
        it cannot see.
        """
        try:
            text = (build_dir / "build.ninja").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
        out: set[str] = set()
        for line in text.splitlines():
            if line.startswith("build "):
                out.add(line[len("build "):].split(":", 1)[0].strip())
        return out

    def build_targets(self, build_dir: Path) -> bool:
        """Build the targets this configuration actually defines, in one cmake call.

        Returns whether the HyperCore worker was built. The interface is ours and the
        engine is upstream's: when the worker target is what fails against a given
        Strata revision, strata and strata-vision are still buildable, and the server
        drives the engine over its stdin/stdout protocol instead (serve/direct_engine.py).
        The fallback is logged and flagged in engine/BUILD.json so the Monitor can say
        "worker: not built" rather than reporting the whole build as failed.

        A configuration with no GPU backend defines no `strata` target at all, so it is
        named before ninja is run rather than after it fails.
        """
        present = self.configured_targets(build_dir)
        if present and "strata" not in present:
            raise RuntimeError(
                "the configured build has no engine target. Upstream defines the strata "
                "executable only when a GPU backend is on: turn on "
                f"{', '.join(GPU_BACKENDS)} (this machine: "
                f"{device_label(device_defaults())})."
            )
        targets = [t for t in ENGINE_TARGETS if not present or t in present]
        skipped = [t for t in ENGINE_TARGETS if t not in targets]
        if skipped:
            self.log_line("this release has no " + ", ".join(skipped) + " target; building "
                          + ", ".join(targets) + " only")
        nproc = str(os.cpu_count() or 4)
        # one --target with the list: repeating the flag keeps only the last one
        code = self.run_stream(["cmake", "--build", str(build_dir),
                               "--target", *targets, "-j", nproc])
        if code == 0:
            return WORKER_TARGET in targets
        engine_only = [t for t in targets if t != WORKER_TARGET]
        self.log_line("the hypercore-worker target failed; building "
                      "strata and strata-vision only")
        code = self.run_stream(["cmake", "--build", str(build_dir),
                               "--target", *engine_only, "-j", nproc])
        if code != 0:
            raise RuntimeError("the engine build failed")
        self.log_line("worker: not built - the engine runs in direct mode")
        return False

    def build_vision(self, clone: Path, defs: dict[str, Any]) -> Path | None:
        """Build the image encoder, which upstream keeps as its own project.

        tools/vision is not a subdirectory of the engine's CMakeLists.txt, so it needs
        its own configure and build. It is optional: an engine without it answers text
        requests, and a vision build that fails is reported, not fatal.
        """
        source = clone / "tools" / "vision"
        if not (source / "CMakeLists.txt").is_file():
            self.log_line("this release has no tools/vision: the image encoder is not built")
            return None
        out = clone / "build-vision"
        flags = [f"-DLLAMA_DIR={_llama_source(clone)}"]
        if str(defs.get("STRATA_ENABLE_CUDA", "")) == "ON":
            flags.append("-DSTRATA_VISION_CUDA=ON")
            flags.append("-DCMAKE_CUDA_ARCHITECTURES=" + str(defs.get("CMAKE_CUDA_ARCHITECTURES", "native")))
        if defs.get("CMAKE_CUDA_COMPILER"):
            flags.append("-DCMAKE_CUDA_COMPILER=" + str(defs["CMAKE_CUDA_COMPILER"]))
        if self.run_stream(["cmake", "-G", "Ninja", "-S", str(source), "-B", str(out), *flags]) != 0:
            self.log_line("the image encoder could not be configured; the engine is built without it")
            return None
        if self.run_stream(["cmake", "--build", str(out), "--target", "strata-vision",
                           "-j", str(os.cpu_count() or 4)]) != 0:
            self.log_line("the image encoder build failed; the engine is built without it")
            return None
        for candidate in (out / "bin" / VEXE, out / VEXE):
            if candidate.is_file():
                return candidate
        self.log_line("the image encoder build produced no binary")
        return None

    def built_binaries(self, build_dir: Path) -> list[Path]:
        """Every engine binary this build produced.

        The engine and the vision engine are linked in the build root; the injected
        HyperCore interface is a subproject, so ninja puts its worker under
        build_dir/hypercore-interface/. Looking in both places is what makes
        "worker: built" true instead of a guess.
        """
        places = (build_dir, build_dir / "hypercore-interface")
        out: list[Path] = []
        seen: set[str] = set()
        for name in (EXE, VEXE, WORKER):
            for place in places:
                path = place / name
                if path.is_file() and name not in seen:
                    out.append(path)
                    seen.add(name)
                    break
        # The K-quant prompt kernels are a shared library the engine dlopen's "next to
        # the engine", so they have to travel with the binary or the experts silently
        # fall back to the FP16 prompt path.
        for place in places:
            for path in sorted(place.glob("*.so")) + sorted(place.glob("*.dll")):
                if quant_store.is_artifact(path.name) and path.name not in seen:
                    out.append(path)
                    seen.add(path.name)
        return out

    def transfer_binaries(self, build_dir: Path, meta: dict[str, Any],
                         quant: str | None, extra: list[Path] | None = None) -> tuple[list[str], str | None]:
        """Move the binaries a build produced into the store.

        `extra` is anything built outside the engine's own build folder, such as the
        image encoder. With a quant named they are kept in engine/engine_<quant>/ and
        that version is made active, so a second quant can be built without losing the
        first, and the run config for that quant is written or repaired to point at it.
        Without one they are simply the default engine in engine/. Returns the files
        placed and the key they were stored under.
        """
        built = self.built_binaries(build_dir) + [Path(p) for p in (extra or []) if Path(p).is_file()]
        if not built:
            raise RuntimeError(
                "the build finished but produced no engine binary. "
                "Check the cmake output above: a configuration with no GPU backend has "
                "no engine to compile."
            )
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        clone = build_dir.parent          # the release clone this build ran in
        key = normalise_or_none(quant)
        store_name = quant_store.store_dir(key).name if key else ENGINE_DIR.name
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
            self.log_line(f"stored engine/{store_name}: {', '.join(copied)}")
            cfg_path, cfg_note = quant_store.ensure_run_config(key, source_root=clone)
            if cfg_path is None:
                self.log_line(f"no run config for {key}: {cfg_note}")
            else:
                self.log_line(f"run config: {cfg_note}")
            # The run config is copied from the nearest config on the box, so the settings
            # the Setup choices hold are written after the copy: without this a build
            # started with --context 262144 runs on the template's 131072.
            written, applied = quant_store.apply_build_choices(key, self.choices)
            if applied and written is not None:
                self.log_line(f"setup choices written to config/{written.name}: "
                              f"{', '.join(applied)}")
            model_named = str(self.choices.get("--model") or "")
            if model_named and model_named.lower().replace("-", "_") != key:
                self.log_line(f"the model box names {model_named}, but this build is stored "
                              f"for {key}: the quant box decides where the engine goes.")
            unwritten = [f"{flag} {value}" for flag, value in self.choices.items()
                         if value and flag not in quant_store.RUN_CHOICE_FIELDS and flag != "--model"]
            if unwritten:
                self.log_line("setup choices a run config does not carry (setup.py decides "
                              f"these): {', '.join(unwritten)}")
            chosen = quant_store.select(key)
            self.log_line(f"active engine: {chosen['note']}"
                         if chosen["active"] else f"could not activate {key}: {chosen['note']}")
        else:
            for src in built:
                shutil.copy2(src, ENGINE_DIR / src.name)
                copied.append(src.name)
            (ENGINE_DIR / "BUILD.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            # A build with no quant named is the default engine, and the run configs that
            # name no quant read their runtime files from engine/data/, next to the
            # binary. Without this they have no data folder at all.
            stored = quant_store.carry_data_dir(None, clone)
            if stored:
                self.log_line(f"stored engine/data: {len(stored)} runtime file(s)")
            if self.choices:
                # The Setup choices have nowhere to go: a build with no quant named is the
                # default engine, and the run configs that start it belong to their own
                # models. Saying so is better than letting the boxes look applied.
                self.log_line("setup choices not written: this build is the default engine "
                              "and writes no run config of its own. Name a quant, or pick "
                              "one in the model box, to have them written.")
        return copied, key

    def start(self, tag: str, options: dict[str, Any], quant: str | None = None,
             choices: Any = None) -> dict[str, Any]:
        """Start a build. `quant` names the quant this engine is built for, so it is
        stored under engine/versions/<quant> instead of overwriting the engine in engine/.

        `choices` are the Setup-choice boxes of the Build tab, in the flat
        ["--context", "32768", "--kv", "int8"] form the setup hook gets. They are written
        into the run config this build writes, so a build started from the card runs with
        the context, KV cache and port the card holds rather than the ones the shipped
        template happens to name. When no quant is named but the card names a model, that
        model's quant is the quant the build is stored under.
        """
        self.choices = quant_store.choice_map(choices) if isinstance(choices, (list, tuple)) else dict(choices or {})
        if not str(quant or "").strip():
            derived = quant_from_choices(self.choices, TMP_DIR / f"strata-{tag}")
            if derived:
                quant = derived
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
            # clean_tmp no longer removes the clone, so a second build of the same tag
            # finds the checkout already there. It is reused: the clone data/ is what a
            # run config points at, and git would refuse to clone into a folder that
            # already holds a checkout, so reuse is the only path that keeps a rebuild of
            # a tag possible without deleting what was asked to be kept.
            if clone.is_dir():
                if not self.reuse_clone(clone, tag):
                    raise RuntimeError(
                        f"tmp/{clone.name} is not a git checkout of {REPO}, so this build "
                        "has nothing to build from")
                self.log_line(f"reused the clone already in tmp/{clone.name}")
            else:
                code = self.run_stream(["git", "clone", "--depth", "1", "--branch", tag,
                                       f"https://github.com/{REPO}.git", str(clone)])
                if code != 0:
                    raise RuntimeError("git clone failed")

            # Inject the HyperCore process boundary so the worker targets exist.
            engine_src = ROOT / "engine-src"
            if engine_src.is_dir():
                shutil.copytree(engine_src, clone / "hypercore-interface", dirs_exist_ok=True)
                if wire_in_interface(clone):
                    self.log_line("injected hypercore-interface (worker + shared library)")
                else:
                    self.log_line("hypercore-interface was copied into the clone but the "
                                  "clone's CMakeLists.txt could not be updated: the worker "
                                  "will not be built")

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

            device = device_defaults()
            # the tab's fields win; a key the tab never sent is filled in from the box.
            # An empty arch is not "no GPU": it means "this machine", not CMake's sm_120 default.
            chosen = sent_options(device["defs"], options)
            if not str(chosen.get("CMAKE_CUDA_ARCHITECTURES", "")).strip():
                chosen["CMAKE_CUDA_ARCHITECTURES"] = device["arch"] or "native"
            chosen, notes = resolve_build_options(chosen, device)
            for note in notes:
                self.log_line(note)
            self.log_line(f"device: {device_label(device)}")
            defs = [f"-D{key}={value}" for key, value in chosen.items()]
            build_dir = clone / "build"
            self.state = "building"
            code = self.run_stream(["cmake", "-G", "Ninja", "-S", str(clone), "-B", str(build_dir), *defs])
            if code != 0:
                raise RuntimeError("cmake configure failed")
            worker_built = self.build_targets(build_dir)
            vision = self.build_vision(clone, chosen)

            self.state = "transferring"
            ENGINE_DIR.mkdir(parents=True, exist_ok=True)
            meta = {
                "source": "built-from-source",
                "version": m.get("version") or tag,
                "tag": tag,
                "archs": chosen.get("CMAKE_CUDA_ARCHITECTURES", ""),
                "device": device["device"],
                "worker": worker_built,
                "built_at": _now(),
                "src": hashlib.sha256((clone / "CMakeLists.txt").read_bytes()).hexdigest()[:16],
                "options": options,
                "choices": self.choices,
            }
            copied, key = self.transfer_binaries(build_dir, meta, quant, [vision] if vision else [])

            # The clone's data/ is merged into the project's data/ before tmp/ is
            # touched, so the runtime files the release ships survive the build.
            merged = self.merge_clone_data(clone)

            # Every run config in config/ is pointed at the build stored for its model,
            # not just the config of the quant this build was asked for.
            refreshed = quant_store.refresh_run_configs(key, clone)
            for line in refreshed:
                self.log_line(f"run configs updated: {line}")

            self.clean_tmp()          # tmp/ is left in place: nothing at runtime reads it
            self.result = {"copied": copied, "version": meta["version"], "quant": key,
                           "merged_data": merged, "refreshed_configs": refreshed}
            self.state = "done"
        except Exception as exc:  # noqa: BLE001 - report it to the tab, do not take the server down
            self.state = "failed"
            self.log_line(f"error: {exc}")
            self.result = {"error": str(exc)}

    def fetch_prebuilt(self, release: dict[str, Any], quant: str | None = None,
                      choices: Any = None) -> dict[str, Any]:
        """Download the ready-made engine for this machine and install it.

        With a quant named it is installed as a model version: unpacked into tmp/,
        then stored in engine/engine_<quant>/ with its own run config, exactly like a
        build from source. Without one it is simply the engine in engine/.

        `choices` are the Setup-choice boxes, and they mean the same thing here as they
        do for a source build: the quant named by the model box is the quant the engine
        is installed as, and the settings the boxes hold are written into the run config
        the install writes.
        """
        self.choices = (quant_store.choice_map(choices) if isinstance(choices, (list, tuple))
                        else dict(choices or {}))
        if not str(quant or "").strip():
            derived = quant_from_choices(self.choices)
            if derived:
                quant = derived
        asset = asset_for(release)
        if not asset:
            return {"error": "this release has no ready-made engine for this machine "
                             "(operating system and GPU toolchain); build from source instead"}
        archive_path = TMP_DIR / asset["name"]
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(asset["url"], archive_path)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"download failed: {exc}"}
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        expected = published_digests(release).get(asset["name"])
        if expected and expected != digest:
            archive_path.unlink(missing_ok=True)
            return {"error": f"the downloaded archive does not match the published SHA-256 ({expected[:16]}...)"}
        key = normalise_or_none(quant)
        if quant and key is None:
            self.log_line(f"quant {quant!r} is not a valid name; "
                         "the ready-made engine is installed as the default engine instead.")
        meta = {"source": "prebuilt", "version": release.get("version"), "tag": release.get("tag"),
               "asset": asset["name"], "sha256": digest, "built_at": _now()}
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        if key:
            unpacked = TMP_DIR / f"prebuilt-{key}"
            unpacked.mkdir(parents=True, exist_ok=True)
            unpack_archive(archive_path, unpacked)
            archive_path.unlink(missing_ok=True)
            meta["quant"] = key
            meta["label"] = key
            entry = quant_store.store_binaries(key, [p for p in unpacked.rglob("*") if p.is_file()], meta)
            # store_binaries keeps the engine artifacts; the runtime data the archive
            # carries (the expert profile, the draft vocab) goes next to them.
            carried = quant_store.carry_data_dir(key, unpacked)
            if carried:
                self.log_line(f"stored engine/{quant_store.store_dir(key).name}/data: "
                              f"{len(carried)} runtime file(s)")
            self.clean_tmp()
            cfg_path, cfg_note = quant_store.ensure_run_config(key)
            written, applied = quant_store.apply_build_choices(key, self.choices)
            if applied and written is not None:
                self.log_line(f"setup choices written to config/{written.name}: {', '.join(applied)}")
            refreshed = quant_store.refresh_run_configs(key)
            for line in refreshed:
                self.log_line(f"run configs updated: {line}")
            chosen = quant_store.select(key)
            return {"ok": True, "version": release.get("version"), "quant": key,
                    "sha256": digest, "files": sorted(entry["files"]),
                    "engine_dir": str(quant_store.store_dir(key)),
                    "run_config": str(cfg_path) if cfg_path else None,
                    "run_config_note": cfg_note,
                    "refreshed_configs": refreshed,
                    "active": chosen["active"], "note": chosen["note"]}
        unpack_archive(archive_path, ENGINE_DIR)
        archive_path.unlink(missing_ok=True)
        (ENGINE_DIR / "BUILD.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        refreshed = quant_store.refresh_run_configs(None)
        for line in refreshed:
            self.log_line(f"run configs updated: {line}")
        return {"ok": True, "version": release.get("version"), "sha256": digest, "files": sorted(
            p.name for p in ENGINE_DIR.iterdir()), "refreshed_configs": refreshed}


JOB = BuildJob()
