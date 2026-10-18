"""serve/quant_store.py - the engine store: one folder per engine build.

Why keyed by quant: the build options a Strata engine needs depend on the quant it
will run. Q4_K / Q5_K / Q6_K want the MMQ prompt kernels (STRATA_MMQ_KQUANTS), an
IQ2_S or IQ3_S expert model wants native IQ experts (STRATA_NATIVE_EXPERTS), and a
q2_0 pack wants neither. Building once for "whatever is on the box" means switching
quant later means rebuilding. So a build is kept under the quant it was configured
for, in its own folder named for it:

    engine/
      BUILD.json              the default engine, as before
      VERSIONS.json           {"active": "q2_0", "versions": {"q2_0": {...}}}
      engine_q2_0/          one folder per engine build, named engine_<quant>
        strata              the engine
        strata-vision       the image encoder, when one was built
        libstrata_kq.so     the K-quant kernels the engine dlopen's next to itself
        data/               the runtime files a build ships (expert-profile.bin, ...)
        BUILD.json          what this build is
      strata                the active engine, symlinked at the build above

The last line is the compatibility trick: every run config written before this still
says "engine/strata", and pointing that at the selected build is what the Model
version dropdown in the header does. Nothing else in the server has to learn a new
path. A run config the Build tab writes points at engine/engine_<quant>/strata
directly instead.

Every file this module writes is under this repository's root. A build never puts
anything outside ~/HyperCore-WebUI/: the clone and the cmake build stay in tmp/,
the binaries land in engine/engine_<quant>/, the run config in config/, the engine
log in logs/. Model weights and tokeniser packs are read-only inputs the user
installed elsewhere; nothing here writes to them.

Every function here is safe to call with no engine running and no index present:
a missing or corrupt VERSIONS.json is rebuilt by scanning engine/ and config/, and
a store with nothing in it returns an empty list rather than raising.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
INDEX_PATH = ENGINE_DIR / "VERSIONS.json"
CONFIG_DIR = ROOT / "config"
LOG_DIR = ROOT / "logs"
TMP_DIR = ROOT / "tmp"

#: An engine build is stored in engine/engine_<quant>/. This is the layout the Build
#: tab writes and the one a hand-built engine follows.
STORE_PREFIX = "engine_"
#: The older layout, engine/versions/<quant>. Still read, so a build made before this
#: change keeps showing in the dropdown; nothing is written there any more.
LEGACY_DIR = ENGINE_DIR / "versions"
VERSIONS_DIR = LEGACY_DIR

IS_WIN = os.name == "nt"
EXE = "strata.exe" if IS_WIN else "strata"
VEXE = "strata-vision.exe" if IS_WIN else "strata-vision"
WORKER = "hypercore-worker.exe" if IS_WIN else "hypercore-worker"
BINARIES = (EXE, VEXE, WORKER)

#: Shared libraries a build produces that the engine loads at run time. strata dlopen's
#: libstrata_kq.so "next to the engine", so these must travel with the binary.
SIDE_LIB = re.compile(r"^(libstrata|libhypercore)[a-z0-9_]*\.(so|dll)$")

#: q2_0, IQ2_S, Q4_K_M, q3_K_S: a leading letter, then letter/digit groups joined
#: by single underscores. Anything else is a typo, not a quant.
QUANT_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
#: The same shape found inside a path, a model name or a pack directory.
QUANT_IN_TEXT = re.compile(r"\b(?:iq|q)\d(?:_[a-z0-9]+)*\b", re.IGNORECASE)

DEFAULT_KEY = "default"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _digest(path: Path) -> str:
    try:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _is_scratch(path: Path) -> bool:
    """Whether a path is in tmp/, the build scratch directory that is deleted when a
    build finishes. Nothing that has to survive a build may live there."""
    try:
        Path(path).resolve().relative_to(TMP_DIR)
        return True
    except (ValueError, OSError):
        return False


def _under(base: Path, path: Any) -> bool:
    """Whether `path` resolves inside `base`."""
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    try:
        target.resolve().relative_to(base)
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------- names
def normalise_quant(raw: Any) -> str:
    """The store key for a quant: 'IQ2_S', 'Q2_0', 'q4-k-m' all become lowercase keys.

    Raises ValueError on a name that cannot be a quant, so a typo in the Build tab
    is reported instead of silently creating engine/engine_q2 0/.
    """
    if raw is None or not str(raw).strip():
        raise ValueError("a quant name is required (for example q2_0 or IQ2_S)")
    key = str(raw).strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    key = re.sub(r"_+", "_", key).strip("_")
    if not key:
        raise ValueError(f"{raw!r} is not a quant name")
    if not QUANT_RE.match(key):
        raise ValueError(f"{raw!r} is not a quant name: expected something like q2_0 or IQ2_S")
    return key


def quant_from_config(cfg: dict) -> str | None:
    """Which quant a run config is for, from the paths it names.

    A config does not have a "quant" key; the quant is in the pack path, the model
    file name and the model_name. Any of them is enough. None when none of them
    names a quant.
    """
    text = " ".join([str(cfg.get("model_name") or "")] + [str(a) for a in (cfg.get("args") or [])])
    for token in QUANT_IN_TEXT.findall(text):
        return normalise_quant(token)
    return None


def store_dir(quant: Any) -> Path:
    """engine/engine_<quant>/, the folder one build lives in."""
    return ENGINE_DIR / f"{STORE_PREFIX}{normalise_quant(quant)}"


def data_dir(quant: Any) -> Path:
    """The runtime files a build ships, kept next to the engine that uses them."""
    return store_dir(quant) / "data"


def engine_binary(quant: Any) -> Path:
    return store_dir(quant) / EXE


def is_artifact(name: str) -> bool:
    """A file an engine build produces that has to travel with it."""
    return name in BINARIES or bool(SIDE_LIB.match(name))


# ---------------------------------------------------------------- the index
def _build_dirs() -> list[tuple[str, Path]]:
    """Every engine build folder on disk: engine/engine_<quant>/, plus the older
    engine/versions/<quant>/."""
    out: list[tuple[str, Path]] = []
    if not ENGINE_DIR.is_dir():
        return out
    for folder in sorted(ENGINE_DIR.iterdir()):
        if not folder.is_dir():
            continue
        if folder.name == "versions":
            for inner in sorted(folder.iterdir()):
                if not inner.is_dir():
                    continue
                try:
                    out.append((normalise_quant(inner.name), inner))
                except ValueError:
                    continue
            continue
        if not folder.name.startswith(STORE_PREFIX):
            continue
        try:
            out.append((normalise_quant(folder.name[len(STORE_PREFIX):]), folder))
        except ValueError:
            continue
    return out


def _artifacts(folder: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for item in sorted(folder.iterdir()):
        if item.is_file() and is_artifact(item.name):
            files[item.name] = _file_info(item)
    return files


def _file_info(path: Path) -> dict[str, Any]:
    try:
        return {"path": str(path), "bytes": path.stat().st_size, "sha256": _digest(path)}
    except OSError:
        return {"path": str(path), "bytes": 0, "sha256": ""}


def _run_configs() -> list[dict[str, Any]]:
    """config/*.json that are run configs, with the quant each one runs.

    The template (config/run-example.json, the one run.sh refuses to pick) is left
    out: its paths are placeholders, so it is a shape, not a model version. Where two
    configs name the same quant, the one whose name or model_name matches it wins.
    """
    out: list[dict[str, Any]] = []
    if not CONFIG_DIR.is_dir():
        return out
    for path in sorted(CONFIG_DIR.glob("*.json")):
        if path.name == "build_options.json" or path.name.endswith(".bak"):
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        if "ABSOLUTE/PATH/TO" in json.dumps(cfg):
            continue
        quant = quant_from_config(cfg)
        out.append({
            "path": str(path),
            "name": path.name,
            "model_name": str(cfg.get("model_name") or ""),
            "quant": quant,
        })
    # a config named for the quant it runs is the one to offer
    def rank(c: dict[str, Any]) -> tuple[int, int, str, str]:
        key = c["quant"] or ""
        named = 0 if key and (key in c["name"] or key in c["model_name"]) else 1
        return (c["quant"] is None, named, c["name"], c["model_name"])
    out.sort(key=rank)
    return out


def index() -> dict[str, Any]:
    """The store as it stands: {active, versions, updated}.

    A missing or unreadable VERSIONS.json is rebuilt from engine/, so the dropdown is
    never empty because of a bookkeeping file.
    """
    if INDEX_PATH.is_file():
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("versions"), dict):
                return data
        except (OSError, ValueError):
            pass
    return rebuild_index()


def rebuild_index() -> dict[str, Any]:
    """Write the index from what is actually on disk and return it."""
    versions: dict[str, dict[str, Any]] = {}
    for key, folder in _build_dirs():
        meta: dict[str, Any] = {}
        meta_path = folder / "BUILD.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        files = _artifacts(folder)
        if not files:
            continue
        versions[key] = {
            "key": key,
            "label": meta.get("label") or key,
            "quant": key,
            "version": meta.get("version"),
            "built_at": meta.get("built_at"),
            "source": meta.get("source"),
            "worker": bool(files.get(WORKER)) or bool(meta.get("worker")),
            "files": files,
            "options": meta.get("options") or {},
            "engine_dir": str(folder),
        }
    active = None
    # The engine the server is running now is the active one, if it is in the store.
    running = ENGINE_DIR / EXE
    if running.is_file():
        for key, entry in versions.items():
            if entry["files"].get(EXE, {}).get("sha256") == _digest(running):
                active = key
                break
    data = {"active": active, "versions": versions, "updated": _now()}
    _write_index(data)
    return data


def _write_index(data: dict[str, Any]) -> None:
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, INDEX_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- storing a build
def store_binaries(quant: Any, sources: list[Path], meta: dict[str, Any]) -> dict[str, Any]:
    """Copy a set of built binaries into engine/engine_<quant>/ and index them.

    `sources` is the list of files the build produced; only the ones that exist and
    are engine artifacts are stored. Returns the index entry for the quant.
    """
    key = normalise_quant(quant)
    dest = store_dir(key)
    if not _under(ENGINE_DIR, dest):
        raise ValueError(f"an engine build must stay inside {ENGINE_DIR}")
    dest.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    for src in sources:
        src = Path(src)
        if not src.is_file() or not is_artifact(src.name):
            continue
        shutil.copy2(src, dest / src.name)
        files[src.name] = _file_info(dest / src.name)
    entry = {
        "key": key,
        "label": meta.get("label") or key,
        "quant": key,
        "version": meta.get("version"),
        "built_at": meta.get("built_at") or _now(),
        "source": meta.get("source"),
        "worker": bool(files.get(WORKER)),
        "files": files,
        "options": meta.get("options") or {},
        "engine_dir": str(dest),
    }
    (dest / "BUILD.json").write_text(json.dumps(entry, indent=1), encoding="utf-8")
    data = index()
    data["versions"][key] = entry
    _write_index(data)
    return entry


def carry_runtime_data(quant: Any, cfg: dict[str, Any], source_root: Path) -> list[str]:
    """Move the runtime files a run config points at out of the build scratch dir.

    A build's clone is deleted once the engine is stored, so a config that names
    tmp/strata-v0.1.45/data/expert-profile.bin would be pointing at nothing the next
    day. Each path under `source_root` is copied to engine/engine_<quant>/data/ and
    rewritten in `cfg`; the caller writes the config. Returns the paths rewritten.
    """
    key = normalise_quant(quant)
    source_root = Path(source_root).resolve()
    moved: list[str] = []

    def rewrite(value: str) -> str:
        head, _, tail = value.partition(":")
        target = Path(head)
        if not target.is_absolute():
            target = ROOT / target
        try:
            rel = target.resolve().relative_to(source_root)
        except ValueError:
            return value
        # the clone's own data/ folder is the runtime data: keep it flat under
        # engine/engine_<quant>/data rather than nesting data/data/.
        parts = list(rel.parts)
        if parts and parts[0] == "data":
            parts = parts[1:]
        if not parts:
            return value
        dest = data_dir(key) / Path(*parts)
        if not _under(ENGINE_DIR, dest):
            return value
        if not target.is_file():
            return value          # nothing to carry; the config keeps the path it had
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(target, dest)
        except OSError:
            return value
        moved.append(value)
        return f"{dest}{(':' + tail) if tail else ''}"

    args = cfg.get("args")
    if isinstance(args, list):
        for i, item in enumerate(args):
            if isinstance(item, str) and "/" in item and not item.startswith("-"):
                new = rewrite(item)
                if new != item:
                    args[i] = new
    for field in ("tokenizer", "log"):
        if isinstance(cfg.get(field), str):
            cfg[field] = rewrite(cfg[field])
    return moved


# ---------------------------------------------------------------- choosing one
def materialise(key: str) -> tuple[bool, str]:
    """Put the binaries of one build where the old run configs look: engine/.

    On Linux the active build is symlinked, on Windows copied; either way
    "engine/strata" is the selected engine and no run config has to change. Returns
    (ok, what was placed or why it could not be).
    """
    key = normalise_quant(key)
    src = store_dir(key)
    if not src.is_dir() and (LEGACY_DIR / key).is_dir():
        src = LEGACY_DIR / key
    if not src.is_dir():
        return False, f"no engine build stored for {key}"
    placed = []
    for name in BINARIES:
        source = src / name
        if not source.is_file():
            continue
        target = ENGINE_DIR / name
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
            if IS_WIN:
                shutil.copy2(source, target)
            else:
                os.symlink(source, target)
            placed.append(name)
        except OSError as exc:
            return False, f"could not put {name} in engine/: {exc}"
    if placed:
        # engine/BUILD.json is what the Monitor and the Build tab read as "what is
        # installed", so it has to describe the build that is now active.
        meta = {}
        meta_path = src / "BUILD.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        meta["active_from"] = str(src)
        meta.setdefault("built_at", _now())
        try:
            _write_json(ENGINE_DIR / "BUILD.json", meta)
        except OSError:
            pass
        return True, f"engine/{', engine/'.join(placed)} now points at {src.name}"
    return False, f"{src} has no engine binaries"


def select(quant: Any) -> dict[str, Any]:
    """Make one version the one the server runs.

    Two kinds of version are selectable: a quant with an engine build in engine/, and
    a run config that names a quant while using the engine already in engine/. The
    first is placed; the second is only pointed at.
    """
    key = normalise_quant(quant)
    data = index()
    entry = data["versions"].get(key)
    if entry is None:
        cfg = next((c for c in _run_configs() if c["quant"] == key), None)
        if cfg is None:
            stored = ", ".join(sorted(data["versions"])) or "none"
            raise ValueError(f"no model version {key} is available. "
                            f"Engine builds stored: {stored}. "
                            f"Run configs in config/: "
                            f"{', '.join(c['name'] for c in _run_configs()) or 'none'}")
        entry = {"key": key, "label": cfg["model_name"] or cfg["name"], "quant": key,
                 "version": None, "built_at": None, "source": "the engine already in engine/",
                 "worker": (ENGINE_DIR / WORKER).is_file(), "files": {}, "options": {},
                 "config": cfg["path"], "config_name": cfg["name"]}
    if entry["files"]:
        ok, note = materialise(key)
    else:
        ok, note = (ENGINE_DIR / EXE).is_file(), "uses the engine already in engine/"
    entry["active"] = bool(ok)
    entry["note"] = note
    entry["ready"] = bool(ok)
    data["active"] = key if ok else data.get("active")
    # the index records engine builds only; a config-only version has nothing to store
    if entry["files"]:
        data["versions"][key] = {k: v for k, v in entry.items()
                                 if k not in ("config", "config_name", "active", "note")}
    _write_index(data)
    return entry


# ---------------------------------------------------------------- what the UI sees
# ---------------------------------------------------------------- a config for a new build
def _quant_style(match: str, target: str) -> str:
    """The new quant written in the case style of the one it replaces (Q2_0 -> IQ2_XS)."""
    return target.upper() if any(c.isupper() for c in match) else target.lower()


def _swap_quant(text: str, key: str) -> str:
    """Every quant named in a path or a model name, rewritten to `key`. Only the
    fields that name a model are touched, never a flag value such as --kv q4_0."""
    return QUANT_IN_TEXT.sub(lambda m: _quant_style(m.group(0), key), text)


def _paths_in(cfg: dict[str, Any]) -> list[str]:
    """Every file path a run config names: the tokenizer and the file arguments."""
    out: list[str] = []
    tokenizer = cfg.get("tokenizer")
    if isinstance(tokenizer, str) and tokenizer:
        out.append(tokenizer)
    for item in cfg.get("args") or []:
        if isinstance(item, str) and "/" in item and not item.startswith("-"):
            out.append(item)
    return out


def _exists(path: str) -> bool:
    """Whether a path a config names is really here. A 'file:weight' argument is
    checked as the file part."""
    target = Path(path.split(":", 1)[0])
    if not target.is_absolute():
        target = ROOT / target
    return target.is_file() or target.is_dir()


def point_config_at_build(cfg: dict[str, Any], key: str, source_root: Path | None = None) -> list[str]:
    """Make a run config point at the engine build the Build tab stored for its quant.

    Three things are corrected, and only when they are wrong:
      exe   the engine binary of this build, inside engine/engine_<quant>/
      cwd   never the build scratch directory, which is deleted after the build
      log   inside logs/, so an engine never writes outside this repository
    Anything the config names under `source_root` (the clone the build ran in) is
    copied into engine/engine_<quant>/data/ and rewritten. Returns what changed.
    """
    changes: list[str] = []
    built = store_dir(key)

    want_exe = built / EXE
    if want_exe.is_file() and cfg.get("exe") != str(want_exe):
        cfg["exe"] = str(want_exe)
        changes.append(f"exe -> engine/{built.name}/{EXE}")

    cwd = cfg.get("cwd")
    if isinstance(cwd, str) and cwd:
        resolved = Path(cwd)
        if not resolved.is_absolute():
            resolved = ROOT / resolved
        if _is_scratch(resolved):
            cfg["cwd"] = str(ROOT)
            changes.append("cwd -> the repository root")

    log = cfg.get("log")
    if not isinstance(log, str) or not _under(LOG_DIR, log):
        cfg["log"] = str(LOG_DIR / f"strata-{key}.log")
        changes.append("log -> logs/strata-<quant>.log")

    if source_root:
        moved = carry_runtime_data(key, cfg, source_root)
        if moved:
            changes.append(f"{len(moved)} runtime file path(s) moved into engine/{built.name}/data")
    return changes


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON, replacing a file the app user cannot open for writing.

    On a two-account box the config directory belongs to the app but a run config
    someone made by hand belongs to their own account and is not writable by the app.
    Writing to a temporary file in the same directory and renaming it needs write on
    the directory, not on the file, so the config is still updated.
    """
    text = json.dumps(data, indent=1)
    try:
        path.write_text(text, encoding="utf-8")
        return
    except OSError:
        if not path.parent.is_dir():
            raise
        tmp = path.with_name(path.name + ".hypercore-tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def _label_from_config(key: str, cfg: dict[str, Any]) -> None:
    """Record the model name in the build's BUILD.json, so the dropdown can show a
    model name rather than a bare quant."""
    name = str(cfg.get("model_name") or "").strip()
    if not name:
        return
    meta_path = store_dir(key) / "BUILD.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if meta.get("label") == name:
        return
    meta["label"] = name
    try:
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    except OSError:
        return
    data = index()
    if key in data["versions"]:
        data["versions"][key]["label"] = name
        _write_index(data)


def ensure_run_config(quant: Any, source_root: Path | None = None) -> tuple[Path | None, str]:
    """Write config/run-<quant>.json for a build, or repair the one that exists.

    An engine built for a quant needs a run config to start from: without one the
    Model version dropdown has nothing to point at. A config that already exists is
    pointed at the new build (its exe, cwd, log and any path still inside the build
    scratch directory are rewritten). A new one is the nearest run config on the box
    - the active version's, otherwise any - with the quant in its paths, model name
    and log file rewritten to this one, and then pointed at the build. Paths that are
    not here are named in a _note so they can be filled in; the file is still a valid
    run config.

    Returns (path, note), or (None, why nothing was written).
    """
    key = normalise_quant(quant)
    target = CONFIG_DIR / f"run-{key}.json"
    if not _under(CONFIG_DIR, target):
        return None, f"{target} is outside the config directory"

    if target.is_file():
        try:
            cfg = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return None, f"config/{target.name} could not be read: {exc}"
        if not isinstance(cfg, dict):
            return None, f"config/{target.name} is not an object"
        changes = point_config_at_build(cfg, key, source_root)
        if not changes:
            _label_from_config(key, cfg)
            return target, f"config/{target.name} already points at this build"
        try:
            _write_json(target, cfg)
        except OSError as exc:
            return None, f"could not write {target}: {exc}"
        _label_from_config(key, cfg)
        return target, f"updated config/{target.name}: {', '.join(changes)}"

    configs = _run_configs()
    if not configs:
        base = CONFIG_DIR / "run-example.json"
        if not base.is_file():
            return None, "there is no run config to base a new one on"
    else:
        active = index().get("active")
        chosen = next((c for c in configs if c["quant"] == active), configs[0])
        base = Path(chosen["path"])
    try:
        cfg = json.loads(base.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, f"the run config {base.name} could not be read: {exc}"
    if not isinstance(cfg, dict):
        return None, f"the run config {base.name} is not an object"
    for field in ("model_name", "tokenizer", "log"):
        if isinstance(cfg.get(field), str):
            cfg[field] = _swap_quant(cfg[field], key)
    args = cfg.get("args")
    if isinstance(args, list):
        cfg["args"] = [_swap_quant(a, key) if isinstance(a, str) and "/" in a else a for a in args]
    changes = point_config_at_build(cfg, key, source_root)
    missing = [p for p in _paths_in(cfg) if not _exists(p)]
    if missing:
        cfg["_note"] = (f"Written by the Build tab for quant {key}. "
                       f"These paths are not on this box yet: {', '.join(missing)}")
    try:
        _write_json(target, cfg)
    except OSError as exc:
        return None, f"could not write {target}: {exc}"
    _label_from_config(key, cfg)
    note = f"created config/{target.name}"
    if changes:
        note += f": {', '.join(changes)}"
    if missing:
        note += f"; fill in: {', '.join(missing)}"
    return target, note


def list_versions() -> list[dict[str, Any]]:
    """Every model version the app can start.

    A version is a quant with an engine build, or a run config that names a quant and
    uses the engine already in engine/. Both are listed: the dropdown is about what
    the user can start, not about how many binaries were compiled.
    """
    data = index()
    active = data.get("active")
    configs = _run_configs()
    out: list[dict[str, Any]] = []
    for key, entry in sorted(data["versions"].items()):
        cfg = next((c for c in configs if c["quant"] == key), None)
        out.append({
            **entry,
            "engine_dir": str(store_dir(key)) if store_dir(key).is_dir() else entry.get("engine_dir"),
            "config": cfg["path"] if cfg else None,
            "config_name": cfg["name"] if cfg else None,
            "active": key == active,
            "ready": bool(entry["files"].get(EXE)),
        })
    for cfg in configs:
        key = cfg["quant"]
        if key and any(v["quant"] == key for v in out):
            continue
        key = key or DEFAULT_KEY
        out.append({
            "key": key,
            "label": cfg["model_name"] or cfg["name"],
            "quant": key,
            "version": None,
            "built_at": None,
            "source": "the engine already in engine/",
            "worker": (ENGINE_DIR / WORKER).is_file(),
            "files": {},
            "options": {},
            "engine_dir": str(ENGINE_DIR),
            "config": cfg["path"],
            "config_name": cfg["name"],
            "active": False,
            "ready": (ENGINE_DIR / EXE).is_file(),
        })
    if not out and (ENGINE_DIR / EXE).is_file():
        out.append({
            "key": DEFAULT_KEY, "label": "the engine in engine/", "quant": DEFAULT_KEY,
            "version": None, "built_at": None, "source": "installed", "worker": False,
            "files": {}, "options": {}, "engine_dir": str(ENGINE_DIR),
            "config": None, "config_name": None,
            "active": True, "ready": True,
        })
    return out


def view() -> dict[str, Any]:
    """The payload GET /versions answers with."""
    versions = list_versions()
    active = next((v for v in versions if v["active"]), None)
    return {"active": active, "versions": versions, "count": len(versions)}
