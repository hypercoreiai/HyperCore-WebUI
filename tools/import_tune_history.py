#!/usr/bin/env python3
"""tools/import_tune_history.py - bring measured priors in from another HyperCore install.

~/black_box/optuna_tune/history holds sweeps measured on this box. The record format is
the one optuna_tune/history.py writes (schema hypercore.tuning_history/1), so the files
are readable as they are and a sweep will use them without any conversion.

What should not go into a repository is the bulk: one 850-trial prior is 750 KB of
per-trial metrics for one machine. This keeps the shape and drops the weight - the top
N configurations by score, the sweep bookkeeping ("runs") removed, and the provenance
recorded so a later sweep can say where a prior came from.

    python tools/import_tune_history.py                       # from ~/black_box
    python tools/import_tune_history.py --dry-run
    python tools/import_tune_history.py --top 50
    python tools/import_tune_history.py --all                 # keep every trial

Imported files are named seed-<slug>_<model>_<objective>_<context>.json. The prefix is
only there so .gitignore can keep them while ignoring everything else in history/; the
tuner's glob ("*_<model>_<objective>_<context>.json") matches them the same way.

A prior measured on a different machine is still loaded only with
--allow-hardware-mismatch, which the Tune tab's "use priors from other hardware" box
turns on. Nothing here makes a foreign measurement silently authoritative.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "optuna_tune"))

from history import SCHEMA, normalise_trial  # noqa: E402  (optuna_tune on sys.path)

DEFAULT_SOURCE = Path.home() / "black_box" / "optuna_tune" / "history"
DEFAULT_DEST = ROOT / "optuna_tune" / "history"
DEFAULT_TOP = 30


def read_record(path: Path) -> dict[str, Any] | None:
    """A history record, or None if the file is not one."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"skip {path.name}: {exc}", file=sys.stderr)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("trials"), list):
        print(f"skip {path.name}: not a tuning history record", file=sys.stderr)
        return None
    if raw.get("schema") != SCHEMA:
        print(f"skip {path.name}: schema {raw.get('schema')!r}, expected {SCHEMA!r}",
              file=sys.stderr)
        return None
    return raw


def seed_trials(record: dict[str, Any], top: int) -> list[dict[str, Any]]:
    """The best configurations in a record, best first.

    DRY rows are bookkeeping from a --dry-run: the engine never booted, so their 0.0
    score is not a measurement and seeding a sampler with them flattens the posterior.
    Trials are deduplicated by signature, keeping the higher score, exactly as a merge
    during a sweep would.
    """
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in record.get("trials", []):
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).upper() == "DRY":
            continue
        trial = normalise_trial(row, 0, 0.8)
        if trial is None:
            continue
        key = tuple(trial["signature"])
        prior = best.get(key)
        if prior is None or trial["score"] > prior["score"]:
            trial["measured"] = (prior.get("measured", 0) + 1) if prior else 1
            best[key] = trial
        else:
            prior["measured"] = prior.get("measured", 1) + 1
    ordered = sorted(best.values(), key=lambda t: (-t["score"], t["signature"]))
    return ordered[:top] if top > 0 else ordered


def fold(existing: dict[str, Any] | None, trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge imported trials into whatever is already in the destination file."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in (existing or {}).get("trials", []):
        if isinstance(row, dict) and isinstance(row.get("cfg"), dict):
            best[tuple(row.get("signature") or [])] = row
    for trial in trials:
        key = tuple(trial["signature"])
        prior = best.get(key)
        if prior is None or trial["score"] > prior.get("score", 0.0):
            trial["measured"] = (prior.get("measured", 0) + 1) if prior else 1
            best[key] = trial
        else:
            prior["measured"] = prior.get("measured", 1) + 1
    return sorted(best.values(), key=lambda t: (-t["score"], t["signature"]))


def build_seed(record: dict[str, Any], source: Path, top: int) -> dict[str, Any]:
    """A history file carrying only the configurations worth shipping."""
    trials = seed_trials(record, top)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema": SCHEMA,
        "model": record.get("model"),
        "objective": record.get("objective"),
        "context": record.get("context"),
        "hardware": record.get("hardware"),
        "hardware_slug": record.get("hardware_slug"),
        "runs": [],
        "trials": trials,
        "updated_utc": record.get("updated_utc") or now,
        "seed": True,
        "seed_source": str(source),
        "seed_imported_utc": now,
        "trials_available": len(seed_trials(record, 0)),
        "trials_shipped": len(trials),
    }


def import_history(source: Path, dest: Path, top: int, force: bool, dry_run: bool) -> int:
    """Import every history file under source. Returns the number written."""
    if not source.is_dir():
        print(f"no history to import: {source} is not a directory", file=sys.stderr)
        return 0
    written = 0
    for path in sorted(source.glob("*.json")):
        record = read_record(path)
        if record is None:
            continue
        seed = build_seed(record, path, top)
        out = dest / f"seed-{path.name}"
        if out.is_file() and not force:
            existing = read_record(out)
            if existing is not None:
                seed["trials"] = fold(existing, seed["trials"])
                seed["trials_shipped"] = len(seed["trials"])
        before = len(record.get("trials", []))
        after = len(seed["trials"])
        size = len(json.dumps(seed))
        print(f"{path.name}: {before} trials -> {after} kept ({size} bytes) -> {out.name}")
        written += 1
        if dry_run:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        try:
            out.write_text(json.dumps(seed, indent=1) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"could not write {out}: {exc}", file=sys.stderr)
            continue
    if written and not dry_run:
        print(f"imported {written} prior file(s) into {dest}")
        print("a sweep on a different machine needs --allow-hardware-mismatch "
              "(the Tune tab's 'use priors from other hardware' box).")
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", default=str(DEFAULT_SOURCE),
                   help="folder of history JSON files (default %(default)s)")
    p.add_argument("--dest", default=str(DEFAULT_DEST),
                   help="where to write the seeds (default %(default)s)")
    p.add_argument("--top", type=int, default=DEFAULT_TOP,
                   help="keep the best N configurations per record; 0 keeps all (default %(default)s)")
    p.add_argument("--all", action="store_true", help="keep every measured configuration")
    p.add_argument("--force", action="store_true", help="overwrite instead of merging")
    p.add_argument("--dry-run", action="store_true", help="report what would be written")
    a = p.parse_args(argv)
    top = 0 if a.all else a.top
    written = import_history(Path(a.source), Path(a.dest), top, a.force, a.dry_run)
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
