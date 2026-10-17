"""serve/tune_api.py - the Tune tab: run optuna_tune/max_tune.sh in the background.

The sweep is a subprocess, not an in-process study. That keeps the web server
responsive during a run that can take an hour, and it means the CLI stays the
single source of truth for what a sweep does.

Before a sweep starts the chat engine is stopped, because a sweep boots its own
engine and needs the GPU. The tab says so, and the note "do not close this page
while a tune is running" is the honest one: the results are written by the
subprocess to disk and reported here when it exits.

Progress is read from the CLI's own stdout. Each trial line looks like:

  trial  12 layer=vram vram=1024 pcie=0.35 ple-io=direct kv=q4_0 ... -> OK gen 141.2 tok/s ...

so the table the tab shows is the table the CLI printed, not a second format we
have to keep in sync.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TUNE_DIR = ROOT / "optuna_tune"
RESULTS = TUNE_DIR / "results"
HISTORY = TUNE_DIR / "history"
IS_WIN = os.name == "nt"

TRIAL_RE = re.compile(
    r"trial\s+(\d+)\s+layer=(\S+)\s+vram=(\S+)\s+pcie=(\S+)\s+ple-io=(\S+)\s+kv=(\S+)\s+"
    r"kvgrow=(\S+)\s+hostcore=(\S+)\s+affinity=(\S+)\s+worker=(\S+)\s+tail=(\S+)\s+"
    r"draft-floor=(\S+).*?->\s+(\w+)\s+gen\s+([\d.]+)\s+tok/s\s+prefill\s+([\d.]+)\s+tok/s\s+"
    r"latency\s+([\d.]+)\s*ms(?:\s+off-card\s+([\d.]+)%)?"
)
FILE_RE = re.compile(r"(record|history|bench report|report|csv|deliverable)s?:\s+(\S+)")

OBJECTIVES = ("gen", "prefill", "latency", "balance")
PINABLE = ("ple-io", "kv", "kvgrow", "hostcore", "affinity", "worker", "tail", "prefill-cap")


def available_models() -> list[str]:
    """Quant names the tuner can be pointed at, from the packs on disk."""
    out = []
    for path in (ROOT / "config").glob("run-*.json"):
        out.append(path.stem.replace("run-", ""))
    return sorted(set(out)) or ["q2_0"]


def build_command(request: dict[str, Any]) -> list[str]:
    """max_tune.sh arguments from the tab's form. Unknown keys are ignored."""
    py = sys.executable
    cmd = [py, str(TUNE_DIR / "bayesian_tune.py")]
    if request.get("model"):
        cmd += ["--model", str(request["model"])]
    for objective in OBJECTIVES:
        if request.get("objective") == objective:
            cmd.append(f"--{objective}")
            break
    if request.get("context"):
        cmd += ["--max-context", str(int(request["context"]))]
    if request.get("trials"):
        cmd += ["--trials", str(int(request["trials"]))]
    if request.get("seed") is not None:
        cmd += ["--seed", str(int(request["seed"]))]
    if request.get("repeat"):
        cmd += ["--repeat", str(int(request["repeat"]))]
    if request.get("sampler"):
        cmd += ["--sampler", str(request["sampler"])]
    if request.get("mode"):
        cmd += ["--mode", str(request["mode"])]
    for key, flag in (("vram_min", "--vram-min"), ("vram_max", "--vram-max"),
                      ("vram_step", "--vram-step"), ("pcie", "--pcie"),
                      ("spec_min_p", "--spec-min-p"), ("max_workers", "--max-workers")):
        if request.get(key) is not None:
            cmd += [flag, str(request[key])]
    for name in PINABLE:
        value = request.get("pin", {}).get(name)
        if value is not None:
            cmd += [f"--{name}", str(value)]
    if request.get("resume"):
        cmd.append("--load")
    if request.get("replay"):
        cmd += ["--replay", str(request["replay"])]
    if request.get("benchmark"):
        cmd.append("--benchmark")
    if request.get("dry_run"):
        cmd.append("--dry-run")
    cmd += ["--deliverable-dir", str(ROOT / "config")]
    return cmd


class TuneJob:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"          # idle | running | done | failed
        self.proc: subprocess.Popen | None = None
        self.trials: list[dict[str, Any]] = []
        self.best: dict[str, Any] | None = None
        self.log: list[str] = []
        self.files: dict[str, str] = {}
        self.command: list[str] = []
        self.started: float | None = None
        self.total: int | None = None

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "command": " ".join(self.command),
            "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
            "trials_done": len(self.trials),
            "trials_total": self.total,
            "best": self.best,
            "rows": self.trials[-200:],
            "files": self.files,
            "log": self.log[-300:],
        }

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.state == "running":
                return {"error": "a sweep is already running"}
            if not (TUNE_DIR / "bayesian_tune.py").is_file():
                return {"error": "optuna_tune/bayesian_tune.py is not present"}
            self.command = build_command(request)
            self.state = "running"
            self.trials = []
            self.best = None
            self.log = []
            self.files = {}
            self.total = int(request.get("trials") or 0) or None
            self.started = time.time()
        self.proc = subprocess.Popen(
            self.command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        return self.status()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.rstrip()
                self.log.append(line)
                m = TRIAL_RE.search(line)
                if m:
                    row = {
                        "trial": int(m.group(1)), "layer": m.group(2), "vram": m.group(3),
                        "pcie": m.group(4), "ple_io": m.group(5), "kv": m.group(6),
                        "kvgrow": m.group(7), "hostcore": m.group(8), "affinity": m.group(9),
                        "worker": m.group(10), "tail": m.group(11), "draft_floor": m.group(12),
                        "status": m.group(13), "gen_tok_s": float(m.group(14)),
                        "prefill_tok_s": float(m.group(15)), "latency_ms": float(m.group(16)),
                        "off_card_pct": float(m.group(17)) if m.group(17) else None,
                    }
                    self.trials.append(row)
                    if row["status"] == "OK":
                        objective = "gen" if "gen" in " ".join(self.command) else "balance"
                        value = {"gen": row["gen_tok_s"], "prefill": row["prefill_tok_s"],
                                 "latency": -row["latency_ms"]}.get(objective, row["gen_tok_s"])
                        if self.best is None or value > self.best["value"]:
                            self.best = {"value": value, "row": row}
                for key, path in FILE_RE.findall(line):
                    self.files[key] = path
            self.proc.wait()
            self.state = "done" if self.proc.returncode == 0 else "failed"
        except Exception as exc:  # noqa: BLE001
            self.state = "failed"
            self.log.append(f"error: {exc}")

    def stop(self) -> dict[str, Any]:
        if self.proc and self.state == "running":
            self.proc.terminate()
            return {"stopped": True}
        return {"stopped": False}


JOB = TuneJob()


def records(limit: int = 20) -> list[dict[str, Any]]:
    """The most recent sweep records, newest first."""
    if not RESULTS.is_dir():
        return []
    files = sorted(RESULTS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for path in files:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            out.append({"path": str(path), "objective": rec.get("objective"),
                        "context": rec.get("context"), "trials": (rec.get("history") or {}).get("trials"),
                        "winner": (rec.get("best") or {}).get("cfg"), "date": rec.get("date")})
        except (OSError, ValueError):
            continue
    return out
