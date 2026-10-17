"""optuna_tune/run_config.py - the sweep's winner as a run config the server can read.

Deliverables, in the project's own format (the same pair setup.py writes for a model):

    max_run_<model>.json   a run config: the keys setup.py owns (exe, args, cwd, tokenizer,
                           model_name, log, lib_dirs, port, gpu, host, api_key,
                           turn_tail_check) with the winning engine arguments in "args"
    max_run_<model>.sh     the launcher setup.py writes: serve/server.py with that config

Only "args" is replaced. Everything the model's existing config carries that setup does not
own (sampling, reasoning, mcp_servers, ...) is kept, so the deliverable is a drop-in startup
pair rather than a tuning dump. The full trial record - every configuration measured and its
metrics - stays in optuna_tune/results/max_run_<model>.record.json.

setup.py is imported when it is available so the defaults, the run flags and the atomic write
are the ones the real installer uses; a stand-in is used when it cannot be imported.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Flags that exist only because the sweep measures one request in one process. They are not
# part of a production run config: the server owns the process, and the request length is
# whatever the client asks for.
SWEEP_ONLY: tuple[tuple[str, int], ...] = (
    ("--serve", 0),
    ("--max-new", 1),
    ("--expert-profile-save-every", 1),
)

# Keys a file must have to be treated as a run config rather than a tuning record.
RUN_CONFIG_MARKERS = ("exe", "args", "cwd")


def _setup_module() -> Any | None:
    """The repository's setup.py, or None if it cannot be imported."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import setup  # the installer module, resolved at call time
    except Exception as exc:  # noqa: BLE001 - a broken or partial install must not lose a finished sweep
        print(f"note: setup.py not importable ({exc}); using built-in defaults", file=sys.stderr)
        return None
    return setup


def _cuda_lib_dirs() -> list[str]:
    """The CUDA bin/lib64 directories actually present, the way the run configs list them."""
    out: list[str] = []
    for cand in sorted(Path("/usr/local").glob("cuda*")):
        lib = cand / "lib64"
        if lib.is_dir():
            out.append(str(cand / "bin"))
            out.append(str(lib))
    return out


def clean_args(args: list[str]) -> list[str]:
    """The engine arguments without the sweep-only flags, order preserved."""
    out: list[str] = []
    i = 0
    while i < len(args):
        flag = args[i]
        skip = next((n for name, n in SWEEP_ONLY if name == flag), None)
        if skip is not None:
            i += 1 + skip
            continue
        out.append(flag)
        i += 1
    return out


def find_base_config(model: str, root: Path) -> dict[str, Any] | None:
    """The model's current run config, if there is one.

    Preferred order: the installed model config, then a deliverable from an earlier sweep.
    A file is only accepted when it has the run-config shape, so a tuning record is never
    mistaken for a config.
    """
    quant = model.lower()
    for name in (f"ni_hypercore-{quant}.json", f"hypercore-{quant}.json", f"max_run_{quant}.json"):
        path = root / name
        if not path.is_file():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict) and all(k in cfg for k in RUN_CONFIG_MARKERS):
            return cfg
    return None


def build_run_config(
    model: str,
    paths: dict[str, str],
    args: list[str],
    record: dict[str, Any],
    base: dict[str, Any] | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    """The run config for the winning configuration.

    `base` is the model's existing config; its non-args keys survive untouched. `paths` is
    ModelPaths.as_dict(), used only when the base has nothing to say about a path.
    """
    cfg: dict[str, Any] = dict(base) if base else {}
    setup = _setup_module()

    engine = str(record.get("engine") or ROOT / "engine" / "hypercore")
    cfg["exe"] = engine
    cfg["args"] = clean_args(args)
    cfg["cwd"] = str(ROOT)

    # The tokenizer has to match the pack the args point at. A base config left over from
    # another account's data folder is rewritten when this sweep's pack carries its own.
    tokenizer = str(cfg.get("tokenizer") or "")
    beside_pack = Path(paths["pack"]) / "tokenizer" if paths.get("pack") else None
    if beside_pack is not None and (beside_pack.is_dir() or beside_pack.is_file()):
        if tokenizer and Path(tokenizer) != beside_pack:
            print(f"note: {tokenizer} is not beside the pack in use ({paths['pack']}); "
                  f"using {beside_pack}", file=sys.stderr)
        tokenizer = str(beside_pack)
    if tokenizer:
        cfg["tokenizer"] = tokenizer

    if not cfg.get("model_name"):
        cfg["model_name"] = f"hypercore-{model.lower()}"

    cfg["log"] = str(ROOT / f"max_run_{model.lower()}.log")

    if not cfg.get("lib_dirs"):
        libs = _cuda_lib_dirs()
        if libs:
            cfg["lib_dirs"] = libs

    if port is not None:
        cfg["port"] = int(port)
    if setup is not None:
        # The installer's own precedence: what this run asked for, then the model's config,
        # then the default. A key someone set by hand is never replaced.
        setup.apply_server_defaults(cfg, base)
    else:
        fallback = {
            "host": os.environ.get("HYPERCORE_DEFAULT_HOST", "0.0.0.0"),
            "api_key": os.environ.get("HYPERCORE_DEFAULT_API_KEY", "0000"),
            "port": int(os.environ.get("HYPERCORE_DEFAULT_PORT", "8081")),
            "turn_tail_check": os.environ.get("HYPERCORE_DEFAULT_TURN_TAIL_CHECK", "1")
            not in ("0", "", "false"),
        }
        for key, default in fallback.items():
            if cfg.get(key) is None:
                cfg[key] = default
    cfg["port"] = int(cfg["port"])
    cfg["turn_tail_check"] = bool(cfg["turn_tail_check"])
    cfg.setdefault("gpu", 0)

    cfg["tuned_by"] = {
        "schema": "hypercore.max_run/1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "objective": record.get("objective"),
        "context": record.get("context"),
        "hardware": record.get("hardware", {}),
        "hardware_slug": record.get("hardware_slug", ""),
        "best": record.get("best", {}),
        "verified": bool(record.get("verified", False)),
        # history | dry_run | study: who produced the winning numbers.
        "source": record.get("source", "study"),
        "metrics": record.get("metrics", {}),
        "study": record.get("study", {}),
        "history": record.get("history", {}),
        "csv": record.get("csv"),
    }
    return cfg


def _write_json(path: Path, cfg: dict[str, Any]) -> Path:
    """Whole or not at all, the way setup.py writes a run config."""
    setup = _setup_module()
    if setup is not None:
        setup.write_config(path, cfg)
        return path
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def launcher_text(
    cfg_path: Path,
    engine: str,
    port: int,
    open_browser: bool,
    turn_tail_check: bool,
    note: tuple[str, ...],
) -> str:
    """The launcher body: the server with this config, exactly as setup.py writes it."""
    serve = [sys.executable, str(ROOT / "serve" / "server.py"), "--engine", engine,
             "--config", str(cfg_path), "--port", str(port)]
    if open_browser:
        serve.append("--open")
    if turn_tail_check:
        serve.append("--turn-tail-check")
    lines = ["#!/bin/sh"]
    lines += [f"# {line}" for line in note]
    lines.append(f'cd "{ROOT}"')
    lines.append("exec " + " ".join(f'"{x}"' for x in serve))
    return "\n".join(lines) + "\n"


def _emit_pair(
    model: str,
    paths: dict[str, str],
    args: list[str],
    record: dict[str, Any],
    deliverable_dir: Path,
    port: int | None,
    open_browser: bool,
) -> tuple[Path, Path]:
    """Write the pair into deliverable_dir; raises OSError if that directory cannot be written."""
    quant = model.lower()
    base = find_base_config(quant, ROOT)
    cfg = build_run_config(quant, paths, args, record, base=base, port=port)
    js = _write_json(deliverable_dir / f"max_run_{quant}.json", cfg)

    note = (
        f"max_run_{quant}.sh - generated {cfg['tuned_by']['generated_utc']} by optuna_tune/max_tune.sh",
        (f"Bayesian (Optuna TPE) winner for the {cfg['tuned_by']['objective']} objective "
         f"at --max-context {cfg['tuned_by']['context']}"),
        (f"gen {cfg['tuned_by']['metrics'].get('gen_tok_s', 0):.2f} tok/s, "
         f"prefill {cfg['tuned_by']['metrics'].get('prefill_tok_s', 0):.2f} tok/s, "
         f"latency {cfg['tuned_by']['metrics'].get('latency_ms', 0):.1f} ms"),
        f"tuned on {cfg['tuned_by'].get('hardware_slug', 'unknown machine')}",
        f"the trial record is optuna_tune/results/max_run_{quant}.record.json",
    )
    sh = deliverable_dir / f"max_run_{quant}.sh"
    # "--engine" is the server's backend selector (mock | hypercore), not the binary path:
    # the binary the worker runs is the "exe" key in the config beside the script.
    sh.write_text(
        launcher_text(js, "hypercore", cfg["port"], open_browser, cfg["turn_tail_check"], note),
        encoding="utf-8",
    )
    sh.chmod(0o755)
    return sh, js


def write_deliverables(
    model: str,
    paths: dict[str, str],
    args: list[str],
    record: dict[str, Any],
    deliverable_dir: Path,
    port: int | None = None,
    open_browser: bool = True,
    fallback_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Write max_run_<model>.sh and max_run_<model>.json; returns (script, config).

    A launcher left by an earlier sweep can belong to another user. A PermissionError on
    the deliverable directory must not throw away a finished sweep, so the pair is written
    to fallback_dir instead and the fallback is printed.
    """
    try:
        return _emit_pair(model, paths, args, record, deliverable_dir, port, open_browser)
    except OSError as exc:
        fb = Path(fallback_dir) if fallback_dir else None
        if fb is None or fb == Path(deliverable_dir):
            raise
        fb.mkdir(parents=True, exist_ok=True)
        print(f"note: cannot write {deliverable_dir} ({exc}); the deliverables go to {fb}",
              file=sys.stderr)
        return _emit_pair(model, paths, args, record, fb, port, open_browser)
