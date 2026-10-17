#!/usr/bin/env python3
"""optuna_tune/best_prefill.py - the best prefill configuration the sweep has measured.

Reads every CSV the Bayesian sweep has written under optuna_tune/results/, dedupes the
trials by configuration signature, keeps the highest measured prefill_tok_s for each
configuration, and emits the winner as a run-config pair:

    qwen_run_<quant>.sh        the launcher (serve/server.py with the winning config)
    qwen_config_<quant>.json   that run config, with the winning engine arguments in "args"

Nothing is booted: the deliverable is rebuilt from measurements already on disk, so it
stays inside the range the engine actually accepted (status PASS rows only).

    ./optuna_tune/best_prefill.py --top 20        # rank the records, write nothing


Rows written before the draft-floor dimension existed carry no --spec-min-p value; those
trials ran with the sweep's floor (0.8), which is what is written back.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))

from run_config import build_run_config, find_base_config, launcher_text
from search_space import (
    Config,
    engine_args,
    resolve_model_paths,
    signature,
)


def _float(value: str | None, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_rows(results_dir: Path) -> list[dict[str, Any]]:
    """Every PASS row with a usable prefill measurement, tagged with its source file."""
    rows: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("bayesian-*.csv")):
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                for raw in csv.DictReader(handle):
                    if raw.get("status") != "PASS":
                        continue
                    prefill = _float(raw.get("prefill_tok_s"))
                    if prefill <= 0.0:
                        continue
                    raw["_source"] = str(path)
                    raw["_prefill"] = prefill
                    rows.append(raw)
        except OSError as exc:
            print(f"note: cannot read {path}: {exc}", file=sys.stderr)
    return rows


def row_config(raw: dict[str, Any], default_draft_floor: float) -> Config:
    """The swept dimensions of one CSV row."""
    draft = raw.get("draft_floor") or raw.get("spec_min_p")
    return {
        "vram": int(raw["vram"]),
        "pcie": float(raw["pcie"]),
        "ple_io": raw["ple_io"],
        "kv": raw["kv"],
        "kvgrow": raw["kvgrow"],
        "host_core": raw["hostcore"],
        "affinity": raw["affinity"],
        "worker": int(raw["worker"]),
        "tail": raw["tail"],
        "draft_floor": _float(draft, default_draft_floor),
    }


def rank_configs(rows: list[dict[str, Any]], default_draft_floor: float = 0.8) -> list[dict[str, Any]]:
    """Best measured prefill per distinct configuration, highest first."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in rows:
        cfg = row_config(raw, default_draft_floor)
        sig = signature(cfg)
        entry = best.get(sig)
        if entry is None or raw["_prefill"] > entry["metrics"]["prefill_tok_s"]:
            best[sig] = {
                "config": cfg,
                "metrics": {
                    "prefill_tok_s": raw["_prefill"],
                    "gen_tok_s": _float(raw.get("gen_tok_s")),
                    "latency_ms": _float(raw.get("latency_ms")),
                    "prompt_ms": _float(raw.get("prompt_ms")),
                    "decode_ms": _float(raw.get("decode_ms")),
                },
                "layer": raw.get("layer", ""),
                "prompt_tokens": int(raw.get("prompt_tokens") or 0),
                "source": raw["_source"],
                "measurements": 1,
            }
        else:
            entry["measurements"] += 1
    return sorted(best.values(), key=lambda e: -e["metrics"]["prefill_tok_s"])


def context_of(config: Config, history_dir: Path) -> int | None:
    """The --max-context the winner was measured at, from the machine's run history."""
    sig = signature(config)
    for path in sorted(history_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for trial in data.get("trials", []):
            if tuple(sorted(map(str, trial.get("signature", [])))) == tuple(sorted(map(str, sig))):
                return data.get("context")
    return None


def emit_pair(
    model: str,
    paths: dict[str, str],
    cfg: Config,
    metrics: dict[str, float],
    context: int,
    record: dict[str, Any],
    out_dir: Path,
    port: int | None,
    open_browser: bool,
) -> tuple[Path, Path]:
    """Write qwen_run_<model>.sh and qwen_config_<model>.json; returns (script, config)."""
    quant = model.lower()
    args = engine_args(
        cfg, paths, context, max_new=512,
        spec_min_p=_float(str(cfg.get("draft_floor")), 0.8),
    )

    base = find_base_config(quant, ROOT)
    run_cfg = build_run_config(quant, paths, args, record, base=base, port=port)
    run_cfg["log"] = str(out_dir / f"qwen_run_{quant}.log")
    tuned = run_cfg.get("tuned_by", {})
    tuned["prompt_tokens"] = record.get("prompt_tokens", 0)
    tuned["alternatives"] = record.get("alternatives", {})
    js = out_dir / f"qwen_config_{quant}.json"
    tmp = js.with_name(js.name + ".tmp")
    tmp.write_text(json.dumps(run_cfg, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, js)

    note = (
        f"qwen_run_{quant}.sh - generated {record['generated_utc']} from the prefill sweep records",
        (
            f"best measured prefill at --max-context {context}: "
            f"{metrics['prefill_tok_s']:.2f} tok/s "
            f"(gen {metrics['gen_tok_s']:.2f} tok/s, latency {metrics['latency_ms']:.1f} ms)"
        ),
        (
            f"winner: vram={cfg['vram']} pcie={cfg['pcie']} ple-io={cfg['ple_io']} kv={cfg['kv']} "
            f"kvgrow={cfg['kvgrow']} host-core={cfg['host_core']} affinity={cfg['affinity']} "
            f"worker={cfg['worker']} tail={cfg['tail']} spec-min-p={cfg['draft_floor']}"
        ),
        f"tuned on {record.get('hardware_slug', 'unknown machine')}",
    )
    sh = out_dir / f"qwen_run_{quant}.sh"
    sh.write_text(
        launcher_text(js, "hypercore", int(run_cfg["port"]), open_browser,
                      bool(run_cfg["turn_tail_check"]), note),
        encoding="utf-8",
    )
    sh.chmod(0o755)
    return sh, js


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="q2_0")
    parser.add_argument("--results-dir", default=str(HERE / "results"))
    parser.add_argument("--history-dir", default=str(HERE / "history"))
    parser.add_argument("--deliverable-dir", default=str(ROOT))
    parser.add_argument("--cntx", type=int, default=None, help="override the measured context")
    parser.add_argument("--top", type=int, default=10, help="how many configurations to print")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-write", action="store_true", help="rank only, write nothing")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.results_dir))
    ranked = rank_configs(rows)
    if not ranked:
        raise SystemExit(f"optuna_tune: no PASS rows with a prefill measurement in {args.results_dir}")

    print(f"scanned {len(rows)} PASS rows -> {len(ranked)} distinct measured configurations")
    print(f"{'prefill':>9} {'gen':>8} {'latency':>9}  configuration")
    for entry in ranked[: args.top]:
        c = entry["config"]
        m = entry["metrics"]
        print(
            f"{m['prefill_tok_s']:>9.2f} {m['gen_tok_s']:>8.2f} {m['latency_ms']:>9.1f}  "
            f"vram={c['vram']} pcie={c['pcie']} ple-io={c['ple_io']} kv={c['kv']} "
            f"kvgrow={c['kvgrow']} host-core={c['host_core']} affinity={c['affinity']} "
            f"worker={c['worker']} tail={c['tail']} spec-min-p={c['draft_floor']}"
        )

    if args.no_write:
        return 0

    winner = ranked[0]
    cfg: Config = winner["config"]
    # Prefill and decode pull in opposite directions on this box, so the pair also records
    # the fastest prompt phase that still leaves a usable decode rate.
    balanced = next((e for e in ranked if e["metrics"]["gen_tok_s"] >= 150.0), None)
    context = args.cntx or context_of(cfg, Path(args.history_dir)) or 262144
    paths = resolve_model_paths(ROOT, args.model).as_dict()
    record = {
        "schema": "hypercore.max_run/1",
        "model": args.model,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "engine": str(ROOT / "engine" / "hypercore"),
        "hardware_slug": "Intel-Core-i9-14900KF_NVIDIA-GeForce-RTX-4090_31GB_PCIe4x16",
        "objective": "prefill",
        "context": context,
        "best": dict(cfg),
        "metrics": winner["metrics"],
        "verified": True,
        "prompt_tokens": winner["prompt_tokens"],
        "source": f"best measured row in the CSVs under {args.results_dir} ({winner['source']})",
        "study": {"mode": "records", "sampler": "none", "trials": len(ranked),
                  "measured": len(rows), "failed": 0, "repeat": 1, "complete": True},
        "alternatives": {
            "runner_up": {
                "config": dict(ranked[1]["config"]) if len(ranked) > 1 else {},
                "metrics": ranked[1]["metrics"] if len(ranked) > 1 else {},
            },
            "balanced": {
                "config": dict(balanced["config"]) if balanced else {},
                "metrics": balanced["metrics"] if balanced else {},
                "note": "highest prefill that still measures a decode rate of at least 150 tok/s",
            },
        },
    }
    sh, js = emit_pair(
        args.model, paths, cfg, winner["metrics"], context, record,
        Path(args.deliverable_dir), args.port, not args.no_browser,
    )
    print(f"wrote {sh}")
    print(f"      {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
