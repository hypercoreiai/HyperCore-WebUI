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
import shutil
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

# The dimension the tab pins, and the flag the search space uses for it. The names
# in PINABLE are the aliases the CLI accepts, so the form and the CLI agree.
PIN_FLAGS = {
    "ple-io": "--ple-io",
    "kv": "--kv",
    "kvgrow": "--kv-grow",
    "hostcore": "--host-core",
    "affinity": "--pool-affinity",
    "worker": "--pool-workers",
    "tail": "--prompt-cache-tail",
    "prefill-cap": "--prefill",
}


def pin_options() -> dict[str, Any]:
    """What each pinable dimension accepts, read from the search space itself.

    The tab renders a box per dimension; without the accepted values a typo in one
    of them is an argparse error deep inside a subprocess. This comes from
    optuna_tune/search_space.py, so a release that adds a kv mode adds it here too.
    """
    try:
        from optuna_tune.search_space import build_space
    except Exception:  # noqa: BLE001 - optuna is optional; the tab still shows the form
        return {}
    out: dict[str, Any] = {}
    for param in build_space():
        name = next((n for n, flag in PIN_FLAGS.items() if flag == param.flag), None)
        if name is None:
            continue
        if param.kind == "choice":
            out[name] = {"kind": "choice", "choices": list(param.choices)}
        else:
            out[name] = {"kind": param.kind, "low": param.low, "high": param.high,
                        "step": param.step}
    return out


def available_models() -> list[str]:
    """Quant names the tuner can be pointed at: every run config in config/."""
    out = []
    for path in (ROOT / "config").glob("run-*.json"):
        out.append(path.stem.replace("run-", ""))
    return sorted(set(out)) or ["q2_0"]


def run_config(model: str) -> dict[str, Any]:
    """The run config a model name points at, or an empty dict if there is none."""
    path = ROOT / "config" / f"run-{model}.json"
    if not path.is_file():
        return {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def engine_paths(model: str) -> dict[str, str]:
    """Where the sweep should get its engine and its weights.

    There is no download logic here: the paths are the ones the run config already
    carries, and the Tune tab's text boxes can override them. The engine defaults to
    engine/strata, which is the only native thing this project needs.
    """
    cfg = run_config(model)
    args = cfg.get("args") or []
    out: dict[str, str] = {}
    for flag, key in (("--pack", "pack"), ("--native", "native"),
                      ("--ple-gguf", "ple_gguf"), ("--mtp", "mtp"),
                      ("--expert-profile", "expert_profile")):
        if flag in args and args.index(flag) + 1 < len(args):
            out[key] = str(args[args.index(flag) + 1])
    exe = cfg.get("exe") or str(ROOT / "engine" / ("strata.exe" if IS_WIN else "strata"))
    out["engine"] = exe
    prompt = cfg.get("prompt_tokens_file") or str(TUNE_DIR / "fixtures" / "prompt.json")
    out["prompt_tokens_file"] = prompt
    return out


def build_command(request: dict[str, Any]) -> list[str]:
    """max_tune.sh arguments from the tab's form. Unknown keys are ignored."""
    py = sys.executable
    cmd = [py, str(TUNE_DIR / "bayesian_tune.py")]
    model = str(request.get("model") or "")
    if model:
        cmd += ["--model", model]
    paths = engine_paths(model)
    # only the path flags the tuner actually has: engine, pack, native, ple-gguf,
    # mtp and the prompt token file. The expert profile is not a CLI flag - the
    # tuner takes it from the run config it reads, so the tab must not invent one.
    for key, flag in (("engine", "--engine"), ("pack", "--pack"), ("native", "--native"),
                      ("ple_gguf", "--ple-gguf"), ("mtp", "--mtp"),
                      ("prompt_tokens_file", "--prompt-tokens-file")):
        value = request.get(key) or paths.get(key)
        if value:
            cmd += [flag, str(value)]
    for objective in OBJECTIVES:
        if request.get("objective") == objective:
            cmd.append(f"--{objective}")
            break
    if request.get("context"):
        cmd += ["--cntx", str(int(request["context"]))]
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
    if request.get("fresh"):
        cmd.append("--no-load")   # ignore previous runs; the tuner loads them by default
    if request.get("cross_hardware"):
        cmd.append("--allow-hardware-mismatch")
    if request.get("cross_objective"):
        cmd.append("--cross-objective")
    if request.get("replay"):
        cmd += ["--replay", str(request["replay"])]
    # the sweep benchmarks the winner unless the tab's box is unchecked; the CLI
    # has no --benchmark, only --no-benchmark
    if not request.get("benchmark"):
        cmd.append("--no-benchmark")
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


def deliverables(model: str) -> dict[str, str]:
    """The files a finished sweep wrote for a model: the run pair and the record."""
    out = {}
    for name, path in (("run_json", ROOT / "config" / f"max_run_{model}.json"),
                       ("run_sh", ROOT / "config" / f"max_run_{model}.sh"),
                       ("record", RESULTS / f"max_run_{model}.record.json")):
        if path.is_file():
            out[name] = str(path)
    if RESULTS.is_dir():
        csvs = sorted(RESULTS.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if csvs:
            out["csv"] = str(csvs[0])
    return out


def apply(model: str, target: Path) -> dict[str, Any]:
    """Write the winner into the run config the server is using.

    Only "args" is replaced; the keys the config carries that the sweep does not own
    (host, api_key, tokenizer, log, mcp_servers) stay as they are, so applying a sweep
    is a drop-in change rather than a rewrite of the file.
    """
    source = ROOT / "config" / f"max_run_{model}.json"
    if not source.is_file():
        return {"error": "no finished sweep for this model: run one first"}
    if not target.is_file():
        return {"error": f"the active run config {target.name} does not exist"}
    try:
        winner = json.loads(source.read_text(encoding="utf-8"))
        active = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"could not read the configs: {exc}"}
    if not isinstance(winner.get("args"), list):
        return {"error": "the sweep record has no engine arguments"}
    backup = target.with_suffix(target.suffix + ".bak")
    try:
        shutil.copy2(target, backup)
        active["args"] = winner["args"]
        target.write_text(json.dumps(active, indent=1) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"error": f"could not write the run config: {exc}"}
    return {"ok": True, "args": winner["args"], "backup": str(backup),
           "note": "the engine restarts with these arguments"}


def priors() -> list[dict[str, Any]]:
    """The run data a sweep could load, and whether it matches this machine.

    optuna_tune/history holds one file per machine/model/objective/context. Files
    named seed-* were imported from another install by tools/import_tune_history.py:
    they are real measurements, but of another box, so the tuner only uses them with
    --allow-hardware-mismatch. The tab shows this so a cold start is a choice rather
    than a surprise.
    """
    if not HISTORY.is_dir():
        return []
    try:
        if str(TUNE_DIR) not in sys.path:
            sys.path.insert(0, str(TUNE_DIR))
        from hardware import detect, differences, hardware_from
        here = detect()
    except Exception:                      # a box with no GPU to probe still gets the list
        here, differences, detect = None, None, None
    out: list[dict[str, Any]] = []
    for path in sorted(HISTORY.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("trials"), list):
            continue
        diffs: list[str] = []
        if here is not None:
            try:
                diffs = differences(here, hardware_from(rec))
            except Exception:
                diffs = ["unreadable hardware record"]
        out.append({"file": path.name, "model": rec.get("model"),
                    "objective": rec.get("objective"), "context": rec.get("context"),
                    "trials": len(rec["trials"]), "hardware": rec.get("hardware_slug"),
                    "matches": not diffs, "differs": diffs,
                    "seed": bool(rec.get("seed")), "updated": rec.get("updated_utc")})
    return out


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
