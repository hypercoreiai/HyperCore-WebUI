"""serve/quant_store.py - the engine store, keyed by quant.

Why keyed by quant: the build options a Strata engine needs depend on the quant it
will run. Q4_K / Q5_K / Q6_K want the MMQ prompt kernels (STRATA_MMQ_KQUANTS), an
IQ2_S or IQ3_S expert model wants native IQ experts (STRATA_NATIVE_EXPERTS), and a
q2_0 pack wants neither. Building once for "whatever is on the box" means switching
quant later means rebuilding. So a build is kept under the quant it was configured
for:

    engine/
      BUILD.json              the active build, as before
      VERSIONS.json           {"active": "q2_0", "versions": {"q2_0": {...}}}
      versions/q2_0/strata    the binaries for that quant
      versions/q2_0/BUILD.json
      strata                  the active binaries, materialised from versions/<active>

The last line is the compatibility trick: every existing run config still says
"engine/strata", and pointing that at the selected quant is what the Model version
dropdown in the header does. Nothing else in the server has to learn a new path.

Every function here is safe to call with no engine running and no index present:
a missing or corrupt VERSIONS.json is rebuilt by scanning engine/versions/ and
config/, and a store with nothing in it returns an empty list rather than raising.
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
VERSIONS_DIR = ENGINE_DIR / "versions"
INDEX_PATH = ENGINE_DIR / "VERSIONS.json"
CONFIG_DIR = ROOT / "config"

IS_WIN = os.name == "nt"
EXE = "strata.exe" if IS_WIN else "strata"
VEXE = "strata-vision.exe" if IS_WIN else "strata-vision"
WORKER = "hypercore-worker.exe" if IS_WIN else "hypercore-worker"
BINARIES = (EXE, VEXE, WORKER)

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


# ---------------------------------------------------------------- names
def normalise_quant(raw: Any) -> str:
    """The store key for a quant: 'IQ2_S', 'Q2_0', 'q4-k-m' all become lowercase keys.

    Raises ValueError on a name that cannot be a quant, so a typo in the Build tab
    is reported instead of silently creating engine/versions/q2 0/.
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


def version_dir(key: str) -> Path:
    return VERSIONS_DIR / normalise_quant(key)


# ---------------------------------------------------------------- the index
def _scan_versions() -> dict[str, dict[str, Any]]:
    """Every engine build under engine/versions/, read from what is on disk."""
    out: dict[str, dict[str, Any]] = {}
    if not VERSIONS_DIR.is_dir():
        return out
    for folder in sorted(VERSIONS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        key = normalise_quant(folder.name)
        meta: dict[str, Any] = {}
        meta_path = folder / "BUILD.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        files = {name: _file_info(folder / name) for name in BINARIES if (folder / name).is_file()}
        if not files:
            continue
        out[key] = {
            "key": key,
            "label": meta.get("label") or key,
            "quant": key,
            "version": meta.get("version"),
            "built_at": meta.get("built_at"),
            "source": meta.get("source"),
            "worker": bool(files.get(WORKER)) or bool(meta.get("worker")),
            "files": files,
            "options": meta.get("options") or {},
        }
    return out


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

    A missing or unreadable VERSIONS.json is rebuilt from engine/versions/, so the
    dropdown is never empty because of a bookkeeping file.
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
    versions = _scan_versions()
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
    """Copy a set of built binaries into engine/versions/<quant>/ and index them.

    `sources` is the list of files the build produced; only the ones that exist are
    stored. Returns the index entry for the quant.
    """
    key = normalise_quant(quant)
    dest = version_dir(key)
    dest.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    for src in sources:
        src = Path(src)
        if not src.is_file():
            continue
        name = src.name
        if name not in BINARIES:
            continue
        shutil.copy2(src, dest / name)
        files[name] = _file_info(dest / name)
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
    }
    (dest / "BUILD.json").write_text(json.dumps(entry, indent=1), encoding="utf-8")
    data = index()
    data["versions"][key] = entry
    _write_index(data)
    return entry


# ---------------------------------------------------------------- choosing one
def materialise(key: str) -> tuple[bool, str]:
    """Put the binaries of one quant where the run configs expect them: engine/.

    On Linux the active build is symlinked, on Windows copied; either way
    "engine/strata" is the selected engine and no run config has to change. Returns
    (ok, what was placed or why it could not be).
    """
    key = normalise_quant(key)
    src = version_dir(key)
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
    if not placed:
        return False, f"engine/versions/{key} has no engine binaries"
    meta = (src / "BUILD.json").read_text(encoding="utf-8") if (src / "BUILD.json").is_file() else ""
    if meta:
        (ENGINE_DIR / "BUILD.json").write_text(meta, encoding="utf-8")
    return True, ", ".join(placed)


def select(quant: Any) -> dict[str, Any]:
    """Make one version the one the server runs.

    Two kinds of version are selectable: a quant with an engine build under
    engine/versions/, and a run config that names a quant while using the engine
    already in engine/. The first is placed; the second is only pointed at.
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
            "config": cfg["path"],
            "config_name": cfg["name"],
            "active": False,
            "ready": (ENGINE_DIR / EXE).is_file(),
        })
    if not out and (ENGINE_DIR / EXE).is_file():
        out.append({
            "key": DEFAULT_KEY, "label": "the engine in engine/", "quant": DEFAULT_KEY,
            "version": None, "built_at": None, "source": "installed", "worker": False,
            "files": {}, "options": {}, "config": None, "config_name": None,
            "active": True, "ready": True,
        })
    return out


def view() -> dict[str, Any]:
    """The payload GET /versions answers with."""
    versions = list_versions()
    active = next((v for v in versions if v["active"]), None)
    return {"active": active, "versions": versions, "count": len(versions)}
