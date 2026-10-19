#!/usr/bin/env python3
"""optuna_tune/bayesian_tune.py - Bayesian (TPE) tuning of the HyperCore engine.

Searches the dimensions the brief names - vram, pcie, ple-io, kv, kvgrow, hostcore,
affinity, worker, tail, draft-floor - against one of three user-selected objectives:

    --prefill   maximise prompt tokens/s
    --gen       maximise generated tokens/s
    --latency   minimise prompt_ms + decode_ms for the request

Two search modes:

    joint    one study over the whole space (the Bayesian sweep proper)
    staged   one study per layer, other layers frozen at the incumbent best. `layer` in the
             CSV is then the layer under test, which is how tune/max_tune.sh rows are read.

Deliverables, written at the repository root as requested:

    max_run_<model>.sh     the production launcher for the winning configuration
    max_run_<model>.json   the winning configuration, its metrics, and every trial

Run data from previous sweeps is kept in optuna_tune/history/, one file per machine,
model, objective and context:

    <CPU>_<GPU>_<RAM>_<PCIe>x<width>_<model>_<objective>_<cntx>.json

A sweep on the same machine loads it, never re-boots a configuration it has already
measured, seeds the TPE sampler with those trials, and keeps drawing until --trials
unseen configurations have been evaluated. --no-load starts cold; a record from
different hardware is ignored unless --allow-hardware-mismatch is given.

Run it through the venv:  ./optuna_tune/max_tune.sh --model q2_0 --gen --cntx 131072
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import optuna
from optuna.samplers import RandomSampler, TPESampler

try:
    from optuna.samplers import GPSampler
except ImportError:  # Older Optuna needed scipy; Optuna 5 lazy-imports torch instead.
    GPSampler = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_runner import Metrics, build_prompt, run_config
from hardware import Hardware, fingerprint, hardware_from
from history import (
    distributions_for,
    find_history,
    history_path,
    machine_rates,
    machine_read_stats,
    other_objective_history,
    usable_trials,
)
from history import load as load_history
from history import save as save_history
from prompt_transfer import (
    TRANSFER_COLUMNS,
    balance_score,
    best_per_resource,
    cross_check,
    estimated_transfer,
    frontier,
    knee,
    off_card_share,
    stable_machine_rate,
)
from run_config import write_deliverables as write_run_pair
from search_space import (
    DEFAULT_CONFIG,
    LAYERS,
    ROW_COLUMNS,
    Config,
    ModelPaths,
    Param,
    build_space,
    engine_args,
    grid_values,
    physical_cores,
    resolve_model_paths,
    sample,
    signature,
)

OBJECTIVES = ("gen", "prefill", "latency", "balance")
PENALTY = {"gen": 0.0, "prefill": 0.0, "latency": -1e9, "balance": 0.0}  # every score is "higher is better"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def signed_score(m: Metrics, objective: str) -> float:
    """One number to maximise. Failed boots score at the bottom of the range."""
    if not m.ok():
        return PENALTY[objective]
    if objective == "gen":
        return m.gen_tok_s
    if objective == "prefill":
        return m.prefill_tok_s
    if objective == "balance":
        # prefill discounted by the share of the prompt time spent reading the pack
        return balance_score(m.prefill_tok_s, m.transfer)
    return -m.latency_ms


def _gp_or_tpe(seed: int) -> optuna.samplers.BaseSampler:
    """GPSampler if its runtime deps are present; TPE otherwise.

    Optuna 5 constructs GPSampler without importing torch, then lazy-loads it on the
    first ask. Check torch here so --sampler gp cannot crash mid-sweep.
    """
    missing: list[str] = []
    if GPSampler is None:
        missing.append("GPSampler (scipy)")
    else:
        try:
            import torch  # noqa: F401
        except ImportError:
            missing.append("torch")
    if missing:
        print(f"note: --sampler gp needs {' and '.join(missing)}; falling back to TPE",
              file=sys.stderr)
        return TPESampler(seed=seed, n_startup_trials=10, multivariate=True)
    return GPSampler(seed=seed)


def make_sampler(name: str, seed: int) -> optuna.samplers.BaseSampler:
    """TPE is the Bayesian sampler; GP is the Gaussian-process one (needs torch)."""
    if name == "random":
        return RandomSampler(seed=seed)
    if name == "gp":
        return _gp_or_tpe(seed)
    return TPESampler(seed=seed, n_startup_trials=10, multivariate=True)


def _cfg_matches_frozen(cfg: dict[str, Any], frozen: dict[str, object]) -> bool:
    """True when every pinned dimension already has the value this sweep is holding."""
    return all(str(cfg.get(key)) == str(value) for key, value in frozen.items())


def frozen_from_args(args: argparse.Namespace) -> dict[str, object]:
    """Dimensions the operator pinned to a constant, keyed as in the search space."""
    frozen: dict[str, object] = {}
    if getattr(args, "pcie", None) is not None:
        frozen["pcie"] = float(args.pcie)
    if getattr(args, "ple_io", None) is not None:
        frozen["ple_io"] = str(args.ple_io)
    if getattr(args, "kv", None) is not None:
        frozen["kv"] = str(args.kv)
    if getattr(args, "kvgrow", None) is not None:
        frozen["kvgrow"] = str(args.kvgrow)
    if getattr(args, "host_core", None) is not None:
        frozen["host_core"] = str(args.host_core)
    if getattr(args, "affinity", None) is not None:
        frozen["affinity"] = str(args.affinity)
    if getattr(args, "worker", None) is not None:
        frozen["worker"] = int(args.worker)
    if getattr(args, "tail", None) is not None:
        frozen["tail"] = str(args.tail)
    if getattr(args, "draft_floor", None) is not None:
        frozen["draft_floor"] = float(args.draft_floor)
    if getattr(args, "prefill_cap", None) is not None:
        frozen["prefill_cap"] = str(args.prefill_cap)
    return frozen


def draft_floor_of(args: argparse.Namespace) -> float:
    """The pinned floor, or the pre-search default used to fill old records."""
    value = getattr(args, "draft_floor", None)
    return float(value) if value is not None else 0.8


@dataclass
class TrialRow:
    layer: str
    cfg: Config
    metrics: Metrics
    score: float
    from_history: bool = False
    # When a prior was recorded. Empty for a row this sweep measured itself.
    measured_utc: str = ""

    def csv_row(self) -> dict[str, object]:
        m = self.metrics
        row: dict[str, object] = {
            "layer": self.layer,
            "vram": self.cfg["vram"],
            "pcie": self.cfg["pcie"],
            "ple_io": self.cfg["ple_io"],
            "kv": self.cfg["kv"],
            "kvgrow": self.cfg["kvgrow"],
            "hostcore": self.cfg["host_core"],
            "affinity": self.cfg["affinity"],
            "worker": self.cfg["worker"],
            "tail": self.cfg["tail"],
            "draft_floor": self.cfg["draft_floor"],
            "prefill_cap": self.cfg.get("prefill_cap", "auto"),
            "status": m.status,
        }
        row.update({
            "gen_tok_s": f"{m.gen_tok_s:.2f}",
            "prefill_tok_s": f"{m.prefill_tok_s:.2f}",
            "latency_ms": f"{m.latency_ms:.1f}",
            "prompt_ms": f"{m.prompt_ms:.1f}",
            "decode_ms": f"{m.decode_ms:.1f}",
            "generated": m.generated,
            "prompt_tokens": m.prompt_tokens,
            "wall_ms": f"{m.wall_ms:.1f}",
            "boot_ms": f"{m.boot_ms:.1f}",
            "disk_read_mb": f"{m.disk_read_mb:.1f}",
            "free_ram_mb": f"{m.ambient.get('free_ram_mb', 0.0):.1f}",
            "swap_used_mb": f"{m.ambient.get('swap_used_mb', 0.0):.1f}",
            "score": f"{self.score:.4f}",
            "note": m.note,
        })
        row.update(m.transfer)
        return row

    def to_dict(self) -> dict[str, Any]:
        """The CSV row plus the configuration as a dict, so a record can be replayed."""
        row = self.csv_row()
        row["cfg"] = dict(self.cfg)
        row["signature"] = list(signature(self.cfg))
        row["from_history"] = self.from_history
        return row


class Sweeper:
    """Runs the study, keeps the incumbent best, and writes the deliverables."""

    def __init__(self, args: argparse.Namespace, root: Path, params: list[Param], paths,
                 hw: Hardware, priors: list[dict[str, Any]]) -> None:
        self.args = args
        self.root = root
        self.params = params
        self.paths = paths
        self.hw = hw
        self.priors = priors
        self.rows: list[TrialRow] = []
        self.seen: set[tuple[object, ...]] = set()
        self.prior_rows: dict[tuple[object, ...], TrialRow] = {}
        self.best_row: TrialRow | None = None
        self.priors_skipped = 0
        self.duplicates_skipped = 0
        self.frozen: dict[str, object] = dict(getattr(args, "frozen", {}) or {})
        if self.frozen:
            self.priors = [p for p in priors if _cfg_matches_frozen(p.get("cfg", {}), self.frozen)]
        self.prompt_ids = build_prompt(args.prompt_tokens_file, args.prompt_tokens)
        self.model = args.model
        self._seed_from_history()

    def _seed_from_history(self) -> None:
        """Turn every prior measured on this machine into a row that never needs a boot."""
        for prior in self.priors:
            stored = prior["metrics"]
            metrics = Metrics(status="CACHED")
            metrics.gen_tok_s = float(stored.get("gen_tok_s", 0.0) or 0.0)
            metrics.prefill_tok_s = float(stored.get("prefill_tok_s", 0.0) or 0.0)
            metrics.latency_ms = float(stored.get("latency_ms", 0.0) or 0.0)
            metrics.prompt_ms = float(stored.get("prompt_ms", 0.0) or 0.0)
            metrics.decode_ms = float(stored.get("decode_ms", 0.0) or 0.0)
            metrics.generated = int(stored.get("generated", 0) or 0)
            metrics.prompt_tokens = int(stored.get("prompt_tokens", 0) or 0)
            metrics.transfer = {k: float(stored.get(k, 0.0) or 0.0) for k in TRANSFER_COLUMNS}
            # A history row written before the DONE counters existed has no telemetry. Left
            # empty, the balance objective degenerates into the prefill objective, so the
            # bus share is reconstructed from the pack's read volume per prompt token.
            if not metrics.transfer.get("file_mb") and getattr(self.args, "machine_rate", None) \
                    and getattr(self.args, "pack_read_mb", 0.0):
                metrics.transfer = estimated_transfer(
                    metrics.prefill_tok_s,
                    metrics.prompt_tokens or self.args.prompt_tokens,
                    self.args.machine_rate,
                    self.args.pack_read_mb,
                )
            row = TrialRow(layer="history", cfg=dict(prior["cfg"]),
                          metrics=metrics, score=float(prior["score"]),
                          from_history=True,
                          measured_utc=str(prior.get("measured_utc", "")))
            # A prior measured under another objective has a score for a different metric.
            # Re-score it with the objective actually running, so the incumbent is real.
            if prior.get("objective") and prior["objective"] != self.args.objective:
                probe = Metrics(status="PASS", gen_tok_s=metrics.gen_tok_s,
                                prefill_tok_s=metrics.prefill_tok_s,
                                latency_ms=metrics.latency_ms, prompt_ms=metrics.prompt_ms,
                                decode_ms=metrics.decode_ms, transfer=metrics.transfer)
                row.score = signed_score(probe, self.args.objective)
            sig = tuple(prior["signature"])
            self.seen.add(sig)
            self.prior_rows[sig] = row
            if self.best_row is None or row.score > self.best_row.score:
                self.best_row = row

    # ---- one configuration ----------------------------------------------------
    def evaluate(self, layer: str, cfg: Config) -> TrialRow:
        sig = signature(cfg)
        if sig in self.seen:
            # Safety net: the study loop skips seen points before calling evaluate.
            # Reuse the measurement we already have rather than boot again.
            prior = self.prior_rows.get(sig) or self.best_row
            if prior is None:
                row = TrialRow(layer, cfg, Metrics(status="DUPLICATE"),
                              PENALTY[self.args.objective])
            else:
                row = TrialRow(layer, cfg, prior.metrics, prior.score,
                              from_history=prior.from_history)
            self.rows.append(row)
            if self.best_row is None or row.score > self.best_row.score:
                self.best_row = row
            return row
        self.seen.add(sig)

        args = engine_args(cfg, self.paths.as_dict(), self.args.cntx,
                           self.args.max_new, draft_floor_of(self.args))
        if self.args.dry_run:
            m = Metrics(status="DRY")
            m.note = " ".join(args)
            row = TrialRow(layer, cfg, m, PENALTY[self.args.objective])
            self.rows.append(row)
            if self.best_row is None:
                self.best_row = row
            return row

        runs: list[Metrics] = []
        for i in range(self.args.repeat):
            # The trial index keeps the name unique: two coverage probes of the same layer
            # can start in the same second and would otherwise overwrite each other's log.
            log = self.args.results_dir / f"run-{utc_stamp()}-{len(self.rows)}-{layer}-{i}.log"
            runs.append(run_config(
                engine=self.args.engine, args=args, prompt_ids=self.prompt_ids,
                max_new=self.args.max_new, log_path=log,
                boot_timeout=self.args.boot_timeout, run_timeout=self.args.run_timeout,
                machine_rate=self.args.machine_rate,
            ))
        # Median of the repeats: one boot is noisy enough to flip a ranking.
        best = max(runs, key=lambda m: signed_score(m, self.args.objective))
        if self.args.repeat > 1 and best.ok():
            best.gen_tok_s = statistics.median(r.gen_tok_s for r in runs if r.ok())
            best.prefill_tok_s = statistics.median(r.prefill_tok_s for r in runs if r.ok())
            best.latency_ms = statistics.median(r.latency_ms for r in runs if r.ok())

        row = TrialRow(layer, cfg, best, signed_score(best, self.args.objective))
        self.rows.append(row)
        if self.best_row is None or row.score > self.best_row.score:
            self.best_row = row
        m = row.metrics
        # The off-card share is the one number that explains a slow decode at a high pcie
        # fraction: it is the percentage of the routed experts the GPU had to have brought in
        # over the bus rather than read from its own VRAM (#588).
        off = off_card_share(m.transfer)
        off_txt = f" off-card {off:.1f}%" if off is not None else " off-card -"
        print(f"  trial {len(self.rows):>3} layer={layer} "
              f"vram={cfg['vram']} pcie={cfg['pcie']} ple-io={cfg['ple_io']} kv={cfg['kv']} "
              f"kvgrow={cfg['kvgrow']} hostcore={cfg['host_core']} affinity={cfg['affinity']} "
              f"worker={cfg['worker']} tail={cfg['tail']} "
              f"draft-floor={cfg['draft_floor']} -> {m.status} "
              f"gen {m.gen_tok_s:.2f} tok/s prefill {m.prefill_tok_s:.2f} tok/s "
              f"latency {m.latency_ms:.0f} ms{off_txt}" + (f"  [{m.note}]" if m.note else ""))
        sys.stdout.flush()
        return row

    # ---- the study -------------------------------------------------------------
    def run(self) -> bool:
        """Run the study. Returns True if it was interrupted by Ctrl-C."""
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        sampler = make_sampler(self.args.sampler, self.args.seed)

        if self.args.mode == "joint":
            study = optuna.create_study(direction="maximize", sampler=sampler)
            self._warm_study(study, self.params)
            self._seed_grid_coverage(study, self.params)
            interrupted = self._run_study(study, self.params, dict(self.frozen), self.args.trials)
            if interrupted:
                print("\ninterrupted: keeping the best configuration measured so far")
            return interrupted

        frozen: dict[str, object] = dict(self.frozen)
        for layer in self.layers():
            layer_params = [p for p in self.params if p.layer == layer]
            if not layer_params:
                continue
            study = optuna.create_study(direction="maximize",
                                      sampler=make_sampler(self.args.sampler, self.args.seed))
            self._warm_study(study, layer_params)
            self._seed_grid_coverage(study, layer_params)
            interrupted = self._run_study(
                study, layer_params, frozen, self.args.trials_per_layer, layer_hint=layer)
            if interrupted:
                print(f"\ninterrupted during layer {layer}; keeping what has been measured")
                return True
            if study.best_trial is not None and study.best_trial.value > PENALTY[self.args.objective]:
                for p in layer_params:
                    frozen[p.key] = study.best_trial.params[p.key]
                print(f"--- layer {layer}: locked {frozen} at {study.best_trial.value:.4f}")
        return False

    def _run_study(
        self,
        study: optuna.Study,
        params: list[Param],
        frozen: dict[str, object],
        n_trials: int,
        layer_hint: str | None = None,
    ) -> bool:
        """Ask Optuna until `n_trials` unseen configurations have been evaluated.

        A suggestion already in history (or already tried this sweep) is pruned, not
        scored, so it does not consume the --trials budget and does not double-count
        in TPE. Returns True if the operator hit Ctrl-C.
        """
        new = 0
        asks = 0
        streak = 0
        max_asks = max(n_trials * 50, n_trials + 32)
        try:
            while new < n_trials and asks < max_asks:
                trial = study.ask()
                asks += 1
                cfg = sample(trial, params, frozen)
                if signature(cfg) in self.seen:
                    study.tell(trial, state=optuna.trial.TrialState.PRUNED)
                    self.duplicates_skipped += 1
                    streak += 1
                    if streak >= 32:
                        print(f"note: Optuna suggested {streak} already-measured "
                              f"configurations in a row; stopping with {new} new trials",
                              file=sys.stderr)
                        break
                    continue
                streak = 0
                study.tell(trial, self._score_cfg(cfg, params, layer_hint))
                new += 1
        except KeyboardInterrupt:
            return True
        if new < n_trials and streak < 32:
            print(f"note: reached the redraw limit with {new} of {n_trials} new trials "
                  f"({self.duplicates_skipped} duplicate suggestions skipped)",
                  file=sys.stderr)
        return False

    def _score_cfg(self, cfg: Config, params: list[Param], layer_hint: str | None) -> float:
        """Evaluate one unseen configuration and return its Optuna score."""
        if layer_hint:
            layer = layer_hint
        else:
            incumbent = self.best_row.cfg if self.best_row else {}
            changed = sorted({p.layer for p in params
                             if p.key in cfg and cfg[p.key] != incumbent.get(p.key, cfg[p.key])})
            layer = "joint" if not self.best_row else (",".join(changed) or "joint")
        return self.evaluate(layer, cfg).score

    def expected_trials(self) -> int:
        if self.args.mode == "joint":
            return self.args.trials
        return self.args.trials_per_layer * len(self.layers())

    def layers(self) -> Sequence[str]:
        wanted = self.args.layers or LAYERS
        return [layer for layer in wanted if layer in LAYERS]

    def _warm_study(self, study, params: list[Param]) -> int:
        """Hand the study the configurations already measured on this machine.

        TPE then samples from a posterior instead of a cold prior. A prior outside the
        grid the current run is searching (--vram-max, --max-workers, ...) is skipped and
        counted, never silently clamped.
        """
        added = 0
        for prior in self.priors:
            try:
                dists = distributions_for(params, prior["cfg"])
            except ValueError:
                self.priors_skipped += 1
                continue
            if not dists:
                continue
            try:
                study.add_trial(optuna.trial.create_trial(
                    params={k: prior["cfg"][k] for k in dists},
                    distributions=dists,
                    value=float(prior["score"]),
                ))
            except Exception as exc:  # noqa: BLE001 - a bad prior must not stop the sweep
                self.priors_skipped += 1
                print(f"note: prior {prior['cfg']} cannot seed the study ({exc})",
                      file=sys.stderr)
                continue
            added += 1
        return added

    def _seed_grid_coverage(self, study, params: list[Param]) -> int:
        """Enqueue the rungs of a dimension TPE cannot reach on its own.

        Two ways a seeded study stops searching a dimension it was asked to search:
        the priors hold one value, or one value holds most of the priors. Both Oct 16
        sweeps put all 25 trials at vram=1280 with a grid of {768, 1024, 1280}: the
        balance priors were 81/91 vram=1280, and TPE draws from the mode. Each missing
        or under-sampled rung is enqueued with every other knob held at the incumbent, so
        the comparison is a clean A/B rather than a new random point.

        Only priors inside the bounds the operator gave are counted. The Oct 16 history
        holds vram values (896, 1152) outside the current grid; counting them makes a
        dimension look explored when the sampler can never draw it.

        Returns the number of trials enqueued.
        """
        if self.args.no_grid_coverage or not self.priors:
            return 0
        base: Config = dict(DEFAULT_CONFIG)
        base.update(self.frozen)
        if self.best_row is not None:
            base.update({k: v for k, v in self.best_row.cfg.items() if k in base})
        budget = max(2, self.args.trials // 4)   # never more than a quarter of the sweep
        probes: list[tuple[Param, object]] = []
        reasons: dict[str, str] = {}
        for p in params:
            if p.key in self.frozen:
                continue
            values = grid_values(p)
            if len(values) < 2:
                continue
            counts: dict[object, int] = {}
            for pr in self.priors:
                value = pr["cfg"].get(p.key, DEFAULT_CONFIG.get(p.key))
                if value in values:
                    counts[value] = counts.get(value, 0) + 1
            if not counts:
                continue
            total = sum(counts.values())
            modal = max(counts.values())
            # A dimension that every rung reached, with no rung holding more than 70%
            # of the priors, is genuinely explored: leave the sampler alone.
            if len(counts) == len(values) and modal / total < 0.7:
                continue
            missing = [v for v in values if counts.get(v, 0) == 0]
            under = [v for v in values
                     if 0 < counts.get(v, 0) < modal * 0.3]
            top = max(counts.items(), key=lambda kv: kv[1])
            for value in (missing + under)[:2]:
                probes.append((p, value))
                reasons[p.key] = f"{p.key}={top[0]} in {top[1]}/{total} priors"
                if len(probes) >= budget:
                    break
            if len(probes) >= budget:
                break
        if not probes:
            return 0
        for p, value in probes:
            cfg = dict(base)
            cfg[p.key] = value
            study.enqueue_trial({q.key: cfg[q.key] for q in params if q.key in cfg})
        detail = "; ".join(sorted({reasons[p.key] for p, _ in probes if p.key in reasons})) or \
               "the priors never varied it"
        print(f"coverage: {detail}; queueing {len(probes)} unmeasured value(s) against "
              "the incumbent so the grid the operator asked for is searched")
        return len(probes)

    # ---- the outputs -----------------------------------------------------------
    def write_csv(self) -> Path:
        self.args.results_dir.mkdir(parents=True, exist_ok=True)
        out = self.args.results_dir / (
            f"bayesian-{self.hw.slug()}-{self.model}-{self.args.objective}-{utc_stamp()}.csv")
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(ROW_COLUMNS) + [
                "gen_tok_s", "prefill_tok_s", "latency_ms", "prompt_ms", "decode_ms",
                "generated", "prompt_tokens", "wall_ms", "boot_ms", "disk_read_mb",
                "free_ram_mb", "swap_used_mb", "score", "note"] + list(TRANSFER_COLUMNS))
            writer.writeheader()
            for row in self.rows:
                writer.writerow(row.csv_row())
        return out

    def winner_args(self) -> list[str]:
        if not self.best_row:
            return []
        return engine_args(self.best_row.cfg, self.paths.as_dict(), self.args.cntx,
                           self.args.max_new, draft_floor_of(self.args))

    def record(self, csv_path: Path) -> dict[str, Any]:
        best = self.best_row
        measured = sum(1 for r in self.rows if r.metrics.ok())
        failed = sum(1 for r in self.rows if not r.metrics.ok() and r.metrics.status != "DRY")
        return {
            "schema": "hypercore.max_run/1",
            "model": self.model,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "engine": str(self.args.engine),
            "hardware": self.hw.to_dict(),
            "hardware_slug": self.hw.slug(),
            "objective": self.args.objective,
            "context": self.args.cntx,
            "max_new": self.args.max_new,
            "spec_min_p": (best.cfg.get("draft_floor") if best else None) or draft_floor_of(self.args),
            "best": dict(best.cfg) if best else {},
            "metrics": {
                "gen_tok_s": best.metrics.gen_tok_s if best else 0.0,
                "prefill_tok_s": best.metrics.prefill_tok_s if best else 0.0,
                "latency_ms": best.metrics.latency_ms if best else 0.0,
                "prompt_ms": best.metrics.prompt_ms if best else 0.0,
                "decode_ms": best.metrics.decode_ms if best else 0.0,
            },
            "effective": best.metrics.effective if best else {},
            # A winner is only a winner if a boot produced tokens. A run that booted
            # nothing (GPU busy, --dry-run) has a best_row but no measurement; a prior
            # carried in from history is a measurement taken by an earlier sweep.
            "verified": bool(best and (best.metrics.ok() or best.from_history)),
            # Where the winning numbers came from: this sweep, a prior recorded on
            # this machine, or a stored record replayed with --replay/--from-records.
            "source": ("history" if best and best.from_history else
                       "dry_run" if self.args.dry_run else "study"),
            "winner_measured_utc": (best.measured_utc if best and best.from_history else
                                    utc_stamp()),
            "args": self.winner_args(),
            "study": {
                "mode": self.args.mode,
                "sampler": self.args.sampler,
                "seed": self.args.seed,
                "trials": len(self.rows),
                "measured": measured,
                "failed": failed,
                "repeat": self.args.repeat,
                "complete": not self.args.trials or len(self.rows) >= self.expected_trials(),
            },
            "history": {
                "file": str(self.args.history_file or history_path(
                    self.args.history_dir, self.model, self.args.objective,
                    self.args.cntx, self.hw)),
                "trials_loaded": len(self.priors),
                "priors_skipped": self.priors_skipped,
                "reused": sum(1 for r in self.rows if r.from_history),
                "duplicates_skipped": self.duplicates_skipped,
            },
            "trials": [r.to_dict() for r in self.rows],
            "csv": str(csv_path),
        }

    def write_record(self, record: dict[str, Any]) -> Path:
        """The full trial record: every configuration measured, with its metrics."""
        out = self.args.results_dir / f"max_run_{self.model}.record.json"
        out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return out

    def write_deliverables(self, record: dict[str, Any]) -> tuple[Path, Path]:
        """Write max_run_<model>.sh and max_run_<model>.json in the project's run-config format.

        A root-level launcher left by an earlier sweep may belong to another user; a
        PermissionError there must not throw away a finished sweep, so the pair falls
        back to the results directory and the fallback is printed.
        """
        return write_run_pair(
            self.model, self.paths.as_dict(), self.winner_args(), record,
            deliverable_dir=self.args.deliverable_dir, port=self.args.port,
            open_browser=not self.args.no_browser,
            fallback_dir=self.args.results_dir,
        )

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(
        description="Bayesian (Optuna) tuning of the HyperCore engine.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--model", default="q2_0", help="quant name; names the deliverables")
    metric = p.add_mutually_exclusive_group()
    metric.add_argument("--gen", action="store_const", const="gen", dest="objective",
                       help="maximise generation tokens/s")
    metric.add_argument("--prefill", action="store_const", const="prefill", dest="objective",
                       help="maximise prefill tokens/s")
    metric.add_argument("--latency", action="store_const", const="latency", dest="objective",
                       help="minimise prompt_ms + decode_ms")
    metric.add_argument("--balance", action="store_const", const="balance", dest="objective",
                       help="maximise prefill tokens/s discounted by the share of the prompt "
                            "time the engine spent reading the pack (see prompt_transfer.py)")
    p.add_argument("--objective", choices=OBJECTIVES, default=None,
                   help="same as the metric flag above; the flag wins")
    p.add_argument("--cntx", "--context", type=int, default=131072, dest="cntx",
                   help="context length the sweep is tuned at")
    p.add_argument("--trials", type=int, default=40,
                   help="unseen configurations to evaluate; repeats of a point already "
                        "in history are redrawn and do not count")
    p.add_argument("--mode", choices=("joint", "staged"), default="joint")
    p.add_argument("--layers", default=None, help="comma separated layers for staged mode")
    p.add_argument("--sampler", choices=("tpe", "gp", "random"), default="tpe")
    p.add_argument("--seed", type=int, default=20261012, help="reproducible search")
    p.add_argument("--repeat", type=int, default=1, help="boots per configuration (median)")
    p.add_argument("--max-new", type=int, default=512, help="tokens generated per request")
    p.add_argument("--prompt-tokens", type=int, default=10240,
                   help="prompt length; >8192 is needed for the tail arm to differ")
    p.add_argument("--prompt-tokens-file", default=None, help="file of token ids to cycle")
    p.add_argument("--vram-min", type=int, default=256)
    p.add_argument("--vram-max", type=int, default=1536)
    p.add_argument("--vram-step", type=int, default=256)
    p.add_argument("--max-workers", type=int, default=None, help="cap the pool-workers grid")
    p.add_argument("--pcie", type=float, default=None,
                   help="hold --pcie-frac at this value instead of searching it")
    p.add_argument("--ple-io", default=None, choices=("direct", "mmap", "ram"), dest="ple_io",
                   help="hold --ple-io at this value instead of searching it")
    p.add_argument("--kv", default=None, choices=("q4_0", "k8v4", "int8", "fp16"),
                   help="hold --kv at this value instead of searching it")
    p.add_argument("--kvgrow", default=None, choices=("0", "1"),
                   help="hold --kv-grow at 0 or 1 instead of searching it")
    p.add_argument("--hostcore", "--host-core", default=None, choices=("first", "last"),
                   dest="host_core",
                   help="hold --host-core at this value instead of searching it")
    p.add_argument("--affinity", default=None, choices=("all", "p-cores", "auto"),
                   help="hold --pool-affinity at this value instead of searching it")
    p.add_argument("--worker", type=int, default=None,
                   help="hold --pool-workers at this value instead of searching it")
    p.add_argument("--tail", default=None, choices=("0", "1"),
                   help="hold --prompt-cache-tail at 0 or 1 instead of searching it")
    p.add_argument("--draft-floor", "--spec-min-p", type=float, default=None,
                   dest="draft_floor",
                   help="hold --spec-min-p at this value (0.001-1) instead of searching it")
    p.add_argument("--prefill-cap", "--prefill-chunk", default=None,
                   choices=("auto", "auto:16384", "auto:32768"), dest="prefill_cap",
                   help="hold the prompt chunk at this value instead of searching it. Plain "
                        "auto is capped at 8192 by the engine (generate.cpp:1782-1790); "
                        "auto:N raises it, and N above 32768 is clamped to 32768.")
    p.add_argument("--engine", default=str(root / "engine" / "strata"))
    p.add_argument("--pack", default=None)
    p.add_argument("--native", default=None)
    p.add_argument("--ple-gguf", default=None)
    p.add_argument("--mtp", default=None)
    p.add_argument("--data-dir", default=None,
                   help="folder holding packs/models/mtp; default the one the installer "
                        "recorded in ~/.config/hypercore/settings.json, then a "
                        "HyperCore-data next to this repo")
    p.add_argument("--boot-timeout", type=int, default=300)
    p.add_argument("--run-timeout", type=int, default=180)
    p.add_argument("--results-dir", default=str(Path(__file__).resolve().parent / "results"))
    p.add_argument("--history-dir", default=str(Path(__file__).resolve().parent / "history"),
                   help="where run data keyed to this machine is kept")
    p.add_argument("--replay", default=None,
                   help="re-emit max_run_<model>.sh/.json from a stored record without "
                        "booting the engine (optuna_tune/results/max_run_<model>.record.json)")
    p.add_argument("--from-records", action="store_true",
                   help="rebuild max_run_<model>.sh/.json from the best measured trial in "
                        "the CSVs under --results-dir, without running a sweep")
    p.add_argument("--history-file", default=None,
                   help="load run data from this file instead of the machine-matched one")
    p.add_argument("--no-load", action="store_true",
                   help="ignore previous runs; start the study cold")
    p.add_argument("--cross-objective", action="store_true",
                   help="also seed the study with configurations measured on this machine "
                        "under a different objective, re-scored for the one running")
    p.add_argument("--no-grid-coverage", action="store_true",
                   help="do not queue the unmeasured values of a dimension every prior "
                        "used identically; by default those rungs are tried against the "
                        "incumbent so the grid the operator asked for is searched")
    p.add_argument("--read-rate", type=float, default=None,
                   help="MB/s pack read rate used to score the balance objective. Default: "
                        "the median of the rates already recorded on this box, so every trial "
                        "in a sweep is measured against one anchor instead of its own boot "
                        "line, which varies 2x run to run")
    p.add_argument("--allow-hardware-mismatch", action="store_true",
                   help="load run data even when it was measured on different hardware")
    p.add_argument("--hardware", default=None,
                   help="pin the machine fingerprint to this JSON object or file instead of "
                        "probing the host (for tests, containers, and a farm of boxes)")
    p.add_argument("--dry-run", action="store_true", help="print the command lines, launch nothing")
    p.add_argument("--no-deliverables", action="store_true",
                   help="write only the CSV; skip max_run_<model>.sh/.json")
    p.add_argument("--deliverable-dir", default=None,
                   help="where max_run_<model>.sh/.json go; default the repository root")
    p.add_argument("--port", type=int, default=None,
                   help="port the generated launcher uses; default the model config's, else 8081")
    p.add_argument("--no-browser", action="store_true",
                   help="the generated launcher leaves out --open")
    p.add_argument("--no-benchmark", action="store_true",
                   help="skip the winner benchmark a sweep runs at the end of a search")
    p.add_argument("--bench-repeat", type=int, default=2,
                   help="boots the end-of-sweep winner benchmark makes (0 = skip it)")
    args = p.parse_args(argv)
    args.root = root
    if not args.objective:
        args.objective = "gen"
    args.layers = [x.strip() for x in args.layers.split(",")] if args.layers else list(LAYERS)
    if args.no_benchmark:
        args.bench_repeat = 0
    args.results_dir = Path(args.results_dir)
    args.history_dir = Path(args.history_dir)
    args.deliverable_dir = Path(args.deliverable_dir) if args.deliverable_dir else root
    if args.trials < 1:
        p.error("--trials must be at least 1")
    args.trials_per_layer = max(2, args.trials // max(1, len(args.layers)))
    args.frozen = frozen_from_args(args)
    return args


def _hardware(spec: str | None) -> Hardware:
    """The machine this sweep ran on: a pinned fingerprint if given, otherwise the host."""
    if not spec:
        return fingerprint()
    try:
        raw = json.loads(Path(spec).read_text(encoding="utf-8")) if Path(spec).is_file() \
            else json.loads(spec)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"optuna_tune: --hardware is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("optuna_tune: --hardware must be a JSON object")
    return hardware_from(raw)


def best_from_records(results_dir: Path, model: str, objective: str) -> TrialRow | None:
    """The highest-scoring measured row across every CSV this tool has written for a model.

    The CSV rows are the measurements; a record's `best` can come from a run that never
    booted the engine. Rebuilding from the rows is the only way to recover a winner that
    is backed by a number.
    """
    best: TrialRow | None = None
    pattern = f"bayesian-*{model}-{objective}-*.csv"
    for csv_path in sorted(results_dir.glob(pattern)):
        try:
            rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        except OSError as exc:
            print(f"note: cannot read {csv_path}: {exc}", file=sys.stderr)
            continue
        for raw in rows:
            if raw.get("status") != "PASS":
                continue
            try:
                metrics = Metrics(
                    status="PASS",
                    gen_tok_s=float(raw.get("gen_tok_s") or 0.0),
                    prefill_tok_s=float(raw.get("prefill_tok_s") or 0.0),
                    latency_ms=float(raw.get("latency_ms") or 0.0),
                    prompt_ms=float(raw.get("prompt_ms") or 0.0),
                    decode_ms=float(raw.get("decode_ms") or 0.0),
                    generated=int(raw.get("generated") or 0),
                    prompt_tokens=int(raw.get("prompt_tokens") or 0),
                )
                cfg: Config = {
                    "vram": int(raw["vram"]),
                    "pcie": float(raw["pcie"]),
                    "ple_io": raw["ple_io"],
                    "kv": raw["kv"],
                    "kvgrow": raw["kvgrow"],
                    "host_core": raw["hostcore"],
                    "affinity": raw["affinity"],
                    "worker": int(raw["worker"]),
                    "tail": raw["tail"],
                    "draft_floor": float(raw["draft_floor"] if raw.get("draft_floor") is not None
                                         else raw.get("spec_min_p") or 0.8),
                }
            except (KeyError, ValueError, TypeError):
                continue
            if not metrics.ok():
                continue
            row = TrialRow("records", cfg, metrics, signed_score(metrics, objective))
            if best is None or row.score > best.score:
                best = row
    return best


def rebuild_from_records(args: argparse.Namespace, paths: ModelPaths, hw: Hardware) -> int:
    """Write the deliverable pair from measured trials already on disk. No engine boots."""
    row = best_from_records(Path(args.results_dir), args.model, args.objective)
    if row is None:
        raise SystemExit(
            f"optuna_tune: no measured (status PASS) trial for model {args.model} "
            f"objective {args.objective} in {args.results_dir}"
        )
    engine = engine_args(row.cfg, paths.as_dict(), args.cntx, args.max_new, draft_floor_of(args))
    record = {
        "schema": "hypercore.max_run/1",
        "model": args.model,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "engine": str(args.engine),
        "hardware": hw.to_dict(),
        "hardware_slug": hw.slug(),
        "objective": args.objective,
        "context": args.cntx,
        "max_new": args.max_new,
        "spec_min_p": row.cfg.get("draft_floor", draft_floor_of(args)),
        "best": dict(row.cfg),
        "metrics": {
            "gen_tok_s": row.metrics.gen_tok_s,
            "prefill_tok_s": row.metrics.prefill_tok_s,
            "latency_ms": row.metrics.latency_ms,
            "prompt_ms": row.metrics.prompt_ms,
            "decode_ms": row.metrics.decode_ms,
        },
        "effective": row.metrics.effective,
        "verified": True,
        "args": engine,
        "study": {"mode": "records", "sampler": "none", "seed": args.seed,
                  "trials": 0, "measured": 1, "failed": 0, "repeat": args.repeat,
                  "complete": False},
        "source": "best measured row in the CSVs under " + str(args.results_dir),
        "trials": [],
    }
    args.deliverable_dir.mkdir(parents=True, exist_ok=True)
    sh, cfg = write_run_pair(
        args.model, paths.as_dict(), engine, record,
        deliverable_dir=args.deliverable_dir, port=args.port,
        open_browser=not args.no_browser,
        fallback_dir=args.results_dir,
    )
    print(f"rebuilt from measured records -> {sh}")
    print(f"                             {cfg}")
    print(f"  winner {json.dumps(row.cfg)}")
    print(f"  gen {row.metrics.gen_tok_s:.2f} tok/s, prefill {row.metrics.prefill_tok_s:.2f} "
          f"tok/s, latency {row.metrics.latency_ms:.1f} ms ({args.objective})")
    return 0


def replay(record_path: Path, args: argparse.Namespace, paths: ModelPaths,
           hw: Hardware) -> int:
    """Re-emit the deliverables from a stored record, without booting the engine.

    A sweep that finished before the run-config deliverables existed, or one whose
    launcher was written to a read-only root, can be rebuilt from its record. The
    hardware block is refreshed to the machine doing the replay, so the pair always
    states where the numbers came from.
    """
    record = load_history(record_path)
    if record is None:
        raise SystemExit(f"optuna_tune: {record_path} is not a readable tuning record")
    if not record.get("best") or not record.get("args"):
        raise SystemExit(f"optuna_tune: {record_path} has no winning configuration to replay")
    if not record.get("verified", any(t.get("status") == "PASS" for t in record.get("trials", []))):
        raise SystemExit(
            f"optuna_tune: {record_path} has no measured trial behind its best; "
            "use --from-records to rebuild from the CSVs instead"
        )
    record.setdefault("hardware", hw.to_dict())
    record["hardware_slug"] = hw.slug()
    record["model"] = args.model
    record["objective"] = record.get("objective") or args.objective
    record["context"] = record.get("context") or args.cntx
    sh, cfg = write_run_pair(
        args.model, paths.as_dict(), list(record["args"]), record,
        deliverable_dir=args.deliverable_dir, port=args.port,
        open_browser=not args.no_browser,
        fallback_dir=args.results_dir,
    )
    print(f"replayed {record_path} -> {sh}")
    print(f"                        {cfg}")
    print(f"  objective {record['objective']} at --max-context {record['context']}, "
          f"machine {hw.slug()}")
    return 0


def benchmark_winner(sweeper: Sweeper, config_path: Path) -> None:
    """Re-run the winning configuration, the way ./bench.sh measures an installed model.

    The benchmark reads the deliverable the sweep just wrote, so it measures what a user would
    actually run rather than the sweep's internal argument list. Nothing here is required: a
    busy card or an unwritable results directory is reported and does not undo the sweep, whose
    deliverables are already on disk.
    """
    from winner_bench import PROMPT_LADDER, report_lines, run_benchmark, write_report

    try:
        result = run_benchmark(
            config=config_path, engine=str(sweeper.args.engine),
            prompt_ids=sweeper.prompt_ids, max_new=sweeper.args.max_new,
            repeat=sweeper.args.bench_repeat, log_dir=sweeper.args.results_dir,
            model=sweeper.model, hw=sweeper.hw, machine_rate=sweeper.args.machine_rate,
            prompt_file=str(sweeper.args.prompt_tokens_file), ladder=PROMPT_LADDER,
        )
    except Exception as exc:  # noqa: BLE001 - a failed confirmation must not lose a finished sweep
        print(f"winner benchmark: could not run ({exc})", file=sys.stderr)
        return
    print("\nwinner benchmark (the winning configuration, re-run):")
    print("\n".join(report_lines(result)))
    try:
        js, md = write_report(result, sweeper.args.results_dir)
        print(f"bench report: {js}")
        print(f"              {md}")
    except OSError as exc:
        print(f"note: the bench report could not be written: {exc}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root

    if args.pack and args.native:
        paths = ModelPaths(pack=args.pack, native=args.native, ple_gguf=args.ple_gguf or "",
                          mtp=args.mtp or "")
    else:
        paths = resolve_model_paths(root, args.model, args.data_dir)
    if args.pack:
        paths.pack = args.pack
    if args.native:
        paths.native = args.native
    if args.ple_gguf:
        paths.ple_gguf = args.ple_gguf
    if args.mtp:
        paths.mtp = args.mtp
    if args.prompt_tokens_file:
        paths.prompt_tokens = args.prompt_tokens_file
    if not paths.prompt_tokens:
        raise SystemExit(
            "optuna_tune: no prompt token file found. Pass --prompt-tokens-file <json with an 'input' list> "
            "(bench/results/exchange-rotation-token-ids.json is the usual one)."
        )
    args.prompt_tokens_file = paths.prompt_tokens

    params = build_space(vram_low=args.vram_min, vram_high=args.vram_max, vram_step=args.vram_step,
                        max_workers=args.max_workers)
    frozen = dict(args.frozen)
    for p in params:
        if p.key not in frozen:
            continue
        value = frozen[p.key]
        if p.kind == "choice" and str(value) not in {str(c) for c in p.choices}:
            raise SystemExit(f"optuna_tune: frozen {p.key}={value!r} is not one of {p.choices}")
        if p.kind == "int" and not (int(p.low) <= int(value) <= int(p.high)):
            raise SystemExit(
                f"optuna_tune: frozen {p.key}={value} is outside [{int(p.low)}, {int(p.high)}]")
        if p.kind == "float" and not (float(p.low) <= float(value) <= float(p.high)):
            raise SystemExit(
                f"optuna_tune: frozen {p.key}={value} is outside [{p.low}, {p.high}]")
    params = [p for p in params if p.key not in frozen]
    if not params:
        raise SystemExit("optuna_tune: every dimension is frozen; nothing to search")

    hw = _hardware(args.hardware)
    history_file = Path(args.history_file) if args.history_file else history_path(
        args.history_dir, args.model, args.objective, args.cntx, hw)

    if args.replay:
        return replay(Path(args.replay), args, paths, hw)

    if args.from_records:
        return rebuild_from_records(args, paths, hw)

    priors: list[dict[str, Any]] = []
    sources: list[Path] = []
    observed_rates: list[float] = []
    if args.no_load:
        print("history: --no-load, starting cold")
    else:
        sources = [history_file] if args.history_file else find_history(
            args.history_dir, args.model, args.objective, args.cntx, hw,
            allow_mismatch=args.allow_hardware_mismatch)
        for source in sources:
            record = load_history(source)
            if record:
                observed_rates.extend(machine_rates(record))
                fill = record.get("spec_min_p")
                priors.extend(usable_trials(
                    record, args.max_new, 0.8 if fill is None else float(fill)))
        if args.cross_objective:
            foreign = other_objective_history(
                args.history_dir, args.model, args.objective, args.cntx, hw, args.max_new, 0.8)
            if foreign:
                objs = ", ".join(sorted({f["objective"] for f in foreign}))
                print(f"history: {len(foreign)} configurations measured under {objs}; "
                      f"re-scored for {args.objective}, never re-booted")
                priors.extend(foreign)
        if priors:
            print(f"history: {len(priors)} configurations from "
                  f"{', '.join(str(s) for s in sources)}")
        else:
            print(f"history: nothing for {hw.slug()} / {args.model} / {args.objective} "
                  f"/ {args.cntx}; starting cold")

    # One read-rate anchor for the whole sweep, so io_share is comparable between trials.
    log_rates, read_volumes = machine_read_stats(args.results_dir)
    if not observed_rates:
        observed_rates = log_rates
    args.machine_rate = stable_machine_rate(args.read_rate, observed_rates)
    args.pack_read_mb = read_volumes.get(args.prompt_tokens, 0.0)
    if args.machine_rate:
        origin = "explicit --read-rate" if args.read_rate else f"median of {len(observed_rates)} recorded boots"
        print(f"read-rate anchor: {args.machine_rate:.0f} MB/s ({origin}); "
              f"{len(read_volumes)} prompt sizes seen, {args.prompt_tokens} tokens reads "
              f"{args.pack_read_mb:.1f} MB on this pack")
    else:
        args.machine_rate = None
        print("read-rate anchor: none recorded yet; each run uses its own boot line")

    print(f"optuna {optuna.__version__} | objective {args.objective} | mode {args.mode} "
          f"| sampler {args.sampler} | seed {args.seed} | context {args.cntx} "
          f"| trials {args.trials} | physical cores {physical_cores()}")
    print(f"machine: {hw.slug()}")
    print(f"engine: {args.engine}")
    print(f"pack:   {paths.pack}")
    if frozen:
        print("frozen: " + ", ".join(f"{k}={v}" for k, v in frozen.items()))

    sweeper = Sweeper(args, root, params, paths, hw, priors)
    t0 = time.monotonic()
    interrupted = sweeper.run()
    elapsed = time.monotonic() - t0

    csv_path = sweeper.write_csv()
    record = sweeper.record(csv_path)
    record["elapsed_s"] = round(elapsed, 1)
    record["interrupted"] = interrupted

    record_path = sweeper.write_record(record)
    print(f"record: {record_path}")
    hist_path = save_history(history_file, record)
    print(f"history: {hist_path} ({record['history']['trials_loaded']} priors loaded, "
          f"{record['history']['reused']} configurations reused, "
          f"{record['history']['duplicates_skipped']} duplicate suggestions skipped)")

    if not args.no_deliverables:
        if not sweeper.best_row:
            print("no configuration was evaluated; nothing written", file=sys.stderr)
            return 1
        if not record["verified"]:
            if args.dry_run:
                print("dry run: nothing was measured, so no deliverable was written; "
                      "the command lines are in the CSV")
                return 0
            print("no configuration produced tokens in this run, so the deliverables were "
                  "not written. Free the GPU (./restart.sh stop) and re-run, or use "
                  "--from-records to rebuild them from measured trials.", file=sys.stderr)
            return 1
        args.deliverable_dir.mkdir(parents=True, exist_ok=True)
        sh_path, json_path = sweeper.write_deliverables(record)
        provenance = ""
        if sweeper.best_row.from_history:
            when = sweeper.best_row.measured_utc or "an earlier sweep"
            provenance = (f"  [from history, measured {when}; not re-booted in this "
                         "sweep, the benchmark below re-runs it]")
        print(f"\nwinner: {json.dumps(sweeper.best_row.cfg)}{provenance}")
        off_txt = (f", off-card {winner_off:.1f}%"
                   if (winner_off := off_card_share(sweeper.best_row.metrics.transfer)) is not None
                   else "")
        print(f"score:  {sweeper.best_row.score:.4f} ({args.objective}) "
              f"gen {sweeper.best_row.metrics.gen_tok_s:.2f} tok/s, "
              f"prefill {sweeper.best_row.metrics.prefill_tok_s:.2f} tok/s, "
              f"latency {sweeper.best_row.metrics.latency_ms:.1f} ms{off_txt}")
        print(f"wrote  {sh_path}")
        print(f"wrote  {json_path}")

        # Confirm the winner by running it again, the way ./bench.sh measures an install.
        if not args.dry_run and args.bench_repeat > 0:
            benchmark_winner(sweeper, json_path)

    # The transfer report is printed for every objective: it is the only place a sweep
    # shows whether its winner is fast because it reads less, or because it reads faster.
    # A dry run measures nothing, so it falls back to the configurations already recorded
    # on this box; DRY rows are never included.
    report = [r.csv_row() for r in sweeper.rows if r.metrics.ok()]
    source = "this sweep"
    if not report:
        report = [r.csv_row() for r in sweeper.prior_rows.values()
                  if r.metrics.prefill_tok_s > 0.0]
        source = "the configurations already measured on this box"
    lines = frontier(report)
    if lines:
        print(f"\nfrontier (prefill up, bus time and bytes read down), from {source}:")
        shown = lines[:10]
        print("\n".join(shown))
        if len(lines) > len(shown):
            print(f"  ... {len(lines) - len(shown)} more non-dominated configurations")
        ladder = best_per_resource((int(r["vram"]), float(r["prefill_tok_s"] or 0.0))
                                  for r in report)
        stop = knee(ladder)
        if stop is not None:
            print(f"vram ladder ({source}, {len(ladder)} rungs): the reserve at "
                  f"{ladder[stop][0]} MiB is the first that does not beat "
                  f"{ladder[stop - 1][0]} MiB ({ladder[stop - 1][1]:.0f} vs "
                  f"{ladder[stop][1]:.0f} tok/s)")
        elif len(ladder) < 2:
            print(f"vram ladder: only {ladder[0][0]} MiB was sampled, so the reserve "
                  "dimension is untested in this sweep")

    # The balance score rests on the engine's own boot MB/s. psutil's block counters are
    # sampled around every boot, so a sweep can say whether that anchor is real bus traffic
    # or page cache before any of it is trusted.
    checks = [cross_check(float(r.get("machine_mb_per_s") or 0.0),
                         float(r.get("file_mb") or 0.0),
                         float(r.get("disk_read_mb") or 0.0),
                         float(r.get("wall_ms") or 0.0)) for r in report]
    live = [c for c in checks if c["present"]]
    if live:
        ratios = sorted(c["ratio"] for c in live)
        median = ratios[len(ratios) // 2]
        print(f"\nread-rate cross-check ({len(live)} runs): kernel moved "
              f"{sum(c['observed_mb'] for c in live):.0f} MB against "
              f"{sum(c['requested_mb'] for c in live):.0f} MB requested, "
              f"median ratio {median:.2f}")
        if median < 0.5:
            print("  ratio under 0.5: most of the pack read came from page cache, so "
                  "io_share is an upper bound and the balance discount is overstated")
    else:
        print("\nread-rate cross-check: no psutil disk counters recorded "
              "(psutil missing, or the sweep measured nothing)")

    # A regime change on this box was traced to system RAM, not the GPU: the same config
    # measured 9.54 tok/s with the tier resident and 3.53 tok/s once it started swapping.
    ram = [float(r.get("free_ram_mb") or 0.0) for r in report if r.get("free_ram_mb")]
    swap = [float(r.get("swap_used_mb") or 0.0) for r in report if r.get("swap_used_mb")]
    if ram:
        print(f"ambient: free RAM {min(ram):.0f}-{max(ram):.0f} MB over the sweep, "
              f"swap in use up to {max(swap, default=0.0):.0f} MB")
        if max(swap, default=0.0) > 500.0:
            print("  swap is in use: the expert tier cannot stay resident, so these "
                  "numbers are not comparable to a swap-free run")

    print(f"csv:    {csv_path}")
    print(f"trials: {len(sweeper.rows)} evaluated, {record['study']['measured']} measured, "
          f"{record['study']['failed']} failed, in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
