"""optuna_tune/history.py - previous sweeps as priors, keyed to the machine they ran on.

One file per (machine, model, objective, context):

    optuna_tune/history/<CPU>_<GPU>_<RAM>_<PCIe>x<width>_<model>_<objective>_<cntx>.json

The file is a merge of every sweep that ran on that machine: the trials it measured,
deduplicated by configuration signature, plus a short list of the runs that produced
them. Loading it does three things:

  1. the configurations already measured on this machine are not booted again;
  2. they are added to the Optuna study as completed trials, so TPE starts from them
     instead of from a cold prior;
  3. a suggestion that matches one of them is redrawn, so --trials means new points.

draft_floor is part of the configuration signature. Older records that stored it only as
the run-level spec_min_p still load: the missing key is filled from that field.

A record from a different CPU, GPU, RAM or PCIe generation is not loaded unless
--allow-hardware-mismatch is passed, because a pcie-frac or pool-workers winner on one
host is not a winner on another.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hardware import Hardware, differences, hardware_from, slug
from prompt_transfer import TRANSFER_COLUMNS
from search_space import Config, Param, signature

SCHEMA = "hypercore.tuning_history/1"
CFG_KEYS: tuple[str, ...] = (
    "vram", "pcie", "ple_io", "kv", "kvgrow", "host_core", "affinity", "worker", "tail",
    "draft_floor", "prefill_cap",
)


def history_path(history_dir: Path, model: str, objective: str, context: int,
                 hw: Hardware) -> Path:
    """The data file for this machine, model, objective and context length."""
    return history_dir / f"{slug(hw)}_{model.lower()}_{objective}_{context}.json"


def load(path: Path) -> dict[str, Any] | None:
    """A record, or None if the file is missing, unreadable or not a history file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"note: cannot read {path} ({exc})", file=sys.stderr)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("trials"), list):
        return None
    return raw


def find_history(history_dir: Path, model: str, objective: str, context: int,
                 hw: Hardware, allow_mismatch: bool = False) -> list[Path]:
    """Every history file for this model/objective/context whose machine matches."""
    if not history_dir.is_dir():
        return []
    needle = f"_{model.lower()}_{objective}_{context}.json"
    found: list[Path] = []
    for path in sorted(history_dir.glob("*" + needle)):
        record = load(path)
        if record is None:
            continue
        diffs = differences(hw, hardware_from(record))
        if not diffs:
            found.append(path)
        elif allow_mismatch:
            print(f"note: {path.name} was measured on another machine "
                f"({'; '.join(diffs)}); loaded because --allow-hardware-mismatch",
                file=sys.stderr)
            found.append(path)
    return found


def normalise_trial(row: dict[str, Any], max_new: int, spec_min_p: float) -> dict[str, Any] | None:
    """A stored trial, or None if it is missing the fields a prior needs."""
    cfg_raw = row.get("cfg")
    if not isinstance(cfg_raw, dict):
        cfg_raw = {k: row[k] for k in CFG_KEYS if k in row}
    cfg: Config = {}
    for key in CFG_KEYS:
        if key not in cfg_raw:
            # Pre-draft_floor records stored the floor on the run, not the trial.
            if key == "draft_floor":
                cfg[key] = float(row.get("spec_min_p", spec_min_p))
                continue
            # Records written before the prompt-chunk dimension existed used the engine
            # default, which is "auto"; filling it in keeps their signatures comparable.
            if key == "prefill_cap":
                cfg[key] = str(row.get("prefill_cap", "auto"))
                continue
            return None
        cfg[key] = cfg_raw[key]
    try:
        score = float(row.get("score", 0.0))
    except (TypeError, ValueError):
        return None
    metrics = row.get("metrics") or {k: row.get(k) for k in
                                    ("gen_tok_s", "prefill_tok_s", "latency_ms",
                                     "prompt_ms", "decode_ms") + TRANSFER_COLUMNS}
    return {
        "cfg": cfg,
        "signature": list(signature(cfg)),
        "score": score,
        "status": str(row.get("status", "UNKNOWN")),
        "layer": str(row.get("layer", "history")),
        "metrics": {k: float(v) if v is not None else 0.0 for k, v in metrics.items()},
        "max_new": int(row.get("max_new", max_new)),
        "spec_min_p": float(row.get("spec_min_p", spec_min_p)),
        "measured_utc": str(row.get("measured_utc", "")),
        "hardware_slug": str(row.get("hardware_slug", "")),
        "measured": int(row.get("measured", 1)),
    }


def usable_trials(record: dict[str, Any], max_new: int, spec_min_p: float) -> list[dict[str, Any]]:
    """Trials whose request shape matches the sweep about to run.

    Metrics only compare against each other when the prompt and the generation length are
    the same, so a trial measured with a different --max-new is dropped rather than
    silently mixed in. --spec-min-p is a searched dimension (draft_floor); it lives on
    the configuration signature instead of filtering the whole record.

    A row written by the CSV writer has no measured_utc column, so the record's own
    timestamp is used: a sweep that ends up choosing a prior needs to say when that
    measurement was taken, not just that it exists.
    """
    out: list[dict[str, Any]] = []
    fallback = str(record.get("updated_utc") or record.get("generated_utc") or "")
    for row in record.get("trials", []):
        if not isinstance(row, dict):
            continue
        # A DRY row is a --dry-run bookkeeping entry: the engine was never booted, so its
        # 0.0 score is not a measurement. Seeding TPE with hundreds of them flattens the
        # posterior and pushes the sampler away from points it has never actually seen.
        if str(row.get("status", "")).upper() == "DRY":
            continue
        trial = normalise_trial(row, max_new, spec_min_p)
        if trial is None:
            continue
        if trial["max_new"] != int(max_new):
            continue
        if not trial["measured_utc"]:
            trial["measured_utc"] = fallback
        out.append(trial)
    return out


def machine_rates(record: dict[str, Any] | None) -> list[float]:
    """Every pack read rate this machine has reported.

    The rate belongs to the box and the pack, not to a configuration, so it is collected
    from every trial in a record rather than only the ones matching the current sweep.
    """
    rates: list[float] = []
    for row in (record or {}).get("trials", []):
        if not isinstance(row, dict):
            continue
        for src in (row.get("metrics"), row):
            if not isinstance(src, dict):
                continue
            try:
                rate = float(src.get("machine_mb_per_s", 0.0))
            except (TypeError, ValueError):
                continue
            if rate > 0:
                rates.append(rate)
    return rates


def machine_read_stats(results_dir: Path, limit: int = 200) -> tuple[list[float], dict[int, float]]:
    """(boot read rates, median pack MB read per prompt size) from the newest run logs.

    The second value is keyed by prompt token count because the pack traffic is a property
    of the request shape, not of the configuration: on this box a 20480-token prompt reads
    24963.4 MB in every one of the 75 runs that used it. That is what lets a history row
    with no telemetry still be scored against the bus.
    """
    from prompt_transfer import parse_machine_rate
    logs = sorted(Path(results_dir).glob("run-*.log"), reverse=True)[:limit]
    rates: list[float] = []
    per_size: dict[int, list[float]] = {}
    for log in logs:
        try:
            lines = log.read_text(errors="replace").splitlines()
        except OSError:
            continue
        rate = parse_machine_rate(lines)
        if rate:
            rates.append(rate)
        done = next((line.split() for line in lines if line.startswith("DONE")), None)
        if done and len(done) > 14:
            try:
                file_mb = float(done[13])
                prompt_tokens = int(done[2])
            except (TypeError, ValueError):
                continue
            if file_mb > 0 and prompt_tokens > 0:
                per_size.setdefault(prompt_tokens, []).append(file_mb)
    volumes = {size: statistics.median(v) for size, v in per_size.items()}
    return rates, volumes


def machine_rates_from_logs(results_dir: Path, limit: int = 200) -> list[float]:
    """Read rates the engine printed at boot, taken straight from the run logs.

    A history record only carries the rate once a sweep has run with the transfer columns,
    so the first balance sweep on a box would otherwise have no anchor. Logs are read
    newest-first and capped, keeping this a few hundred file reads.
    """
    return machine_read_stats(results_dir, limit)[0]


def other_objective_history(history_dir: Path, model: str, objective: str, context: int,
                           hw: Hardware, max_new: int, spec_min_p: float) -> list[dict[str, Any]]:
    """Trials measured on this machine and context under a *different* objective.

    A configuration measured with --gen carries a gen score, which is not a balance score,
    so each row is returned with its objective attached and the caller re-scores it before
    use. Nothing is merged into the current objective's file: the point is to avoid
    re-booting a configuration the engine has already run, not to compare unlike metrics.
    """
    out: list[dict[str, Any]] = []
    pattern = f"{slug(hw)}_{model.lower()}_*_{context}.json"
    for path in sorted(history_dir.glob(pattern)):
        record = load(path)
        if not record:
            continue
        other = str(record.get("objective", ""))
        if not other or other == objective:
            continue
        fill = record.get("spec_min_p")
        for trial in usable_trials(record, max_new, 0.8 if fill is None else float(fill)):
            trial["objective"] = other
            out.append(trial)
    return out


def merge(existing: dict[str, Any] | None, record: dict[str, Any]) -> dict[str, Any]:
    """Fold one sweep into the history file: best score wins for each configuration."""
    base = existing or {
        "schema": SCHEMA,
        "model": record["model"],
        "objective": record["objective"],
        "context": record["context"],
        "hardware": record["hardware"],
        "hardware_slug": record["hardware_slug"],
        "runs": [],
        "trials": [],
    }
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for t in base.get("trials", []):
        if not isinstance(t, dict) or not isinstance(t.get("cfg"), dict):
            continue
        # A hand-edited or older record may not carry a signature; derive it from the
        # configuration rather than crashing the sweep on a KeyError.
        sig = t.get("signature") or list(signature(t["cfg"]))
        best[tuple(sig)] = t
    for row in record.get("trials", []):
        fill = record.get("spec_min_p")
        trial = normalise_trial(row, record.get("max_new", 0), 0.8 if fill is None else float(fill))
        if trial is None:
            continue
        key = tuple(trial["signature"])
        prior = best.get(key)
        if prior is None or trial["score"] > prior["score"]:
            trial["measured"] = (prior.get("measured", 0) + 1) if prior else 1
            best[key] = trial
        else:
            prior["measured"] = prior.get("measured", 1) + 1
    base["trials"] = sorted(best.values(), key=lambda t: (-t["score"], t["signature"]))
    base["hardware"] = record["hardware"]
    base["hardware_slug"] = record["hardware_slug"]
    base["updated_utc"] = datetime.now(timezone.utc).isoformat()
    base.setdefault("runs", []).append({
        "generated_utc": record.get("generated_utc"),
        "trials": len(record.get("trials", [])),
        "measured": record.get("study", {}).get("measured", 0),
        "best": record.get("best", {}),
        "metrics": record.get("metrics", {}),
        "csv": record.get("csv"),
        "loaded_priors": record.get("history", {}).get("trials_loaded", 0),
    })
    base["runs"] = base["runs"][-50:]
    return base


def save(path: Path, record: dict[str, Any]) -> Path:
    """Write the merged history. Whole file or no file: a half-written prior is worse than none."""
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = merge(load(path), record)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _int_distribution(low: int, high: int, step: int):
    """The integer distribution, named differently across Optuna releases."""
    import optuna.distributions as dist

    cls = (getattr(dist, "IntDistribution", None)
           or getattr(dist, "UniformIntDistribution", None)
           or getattr(dist, "IntUniformDistribution", None))
    if cls is None:  # pragma: no cover - one of these exists in every supported Optuna
        raise RuntimeError("this Optuna build has no integer distribution")
    return cls(low, high, step=step)


def _float_distribution(low: float, high: float):
    """The continuous distribution, named differently across Optuna releases."""
    import optuna.distributions as dist

    cls = (getattr(dist, "FloatDistribution", None)
           or getattr(dist, "UniformFloatDistribution", None)
           or getattr(dist, "UniformDistribution", None))
    if cls is None:  # pragma: no cover - one of these exists in every supported Optuna
        raise RuntimeError("this Optuna build has no continuous distribution")
    return cls(low, high)


def distributions_for(params: list[Param], cfg: Config) -> dict[str, Any]:
    """The Optuna distributions a stored configuration must sit inside."""
    import optuna.distributions as dist

    out: dict[str, Any] = {}
    for p in params:
        if p.key not in cfg:
            continue
        value: Any = cfg[p.key]
        if p.kind == "choice":
            if value not in p.choices:
                raise ValueError(f"{p.key}={value!r} is outside the current grid")
            out[p.key] = dist.CategoricalDistribution(choices=list(p.choices))
        elif p.kind == "int":
            low, high, step = int(p.low), int(p.high), int(p.step) or 1
            if not (low <= int(value) <= high):
                raise ValueError(f"{p.key}={value!r} is outside [{low}, {high}]")
            out[p.key] = _int_distribution(low, high, step)
        else:
            flo, fhi = float(p.low), float(p.high)
            if not (flo <= float(value) <= fhi):
                raise ValueError(f"{p.key}={value!r} is outside [{flo}, {fhi}]")
            out[p.key] = _float_distribution(flo, fhi)
    return out
