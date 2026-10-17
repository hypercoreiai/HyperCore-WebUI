#!/usr/bin/env python3
"""optuna_tune/winner_bench.py - benchmark the winning configuration the way the project runs it.

    ./optuna_tune/max_tune.sh --benchmark --model q2_0
    ./optuna_tune/winner_bench.py --config max_run_q2_0.json --repeat 3

Same shape as `~/Strata/setup.sh --setup` / `./bench.sh`: one command, no arguments to
remember. It reads the run config the sweep wrote (max_run_<model>.json), checks the machine
and the card, boots the engine with that config's own `args`, and walks a ladder of prompt
sizes from 512 tokens up to the largest the winner's context allows (512, 1024, 2048, 4096,
8192, 16384 - the same idea as bench.sh's 1K/4K/16K rungs). For every size it reports
medians over `--repeat` boots:

  prefill tok/s, generation tok/s, request latency, and the off-card share of the decode's
  routed experts (DONE position 15 against the cache lookups, the number the engine prints
  beside its hit rate: src/program/generate.cpp:10435-10439).

The winner's recorded numbers are read from the tuning record beside the config, so the report
ends with the delta between what the sweep measured and what this run measures. Nothing here
changes the deliverables; a benchmark that fails to boot is reported as a failed boot, not as
a crash.

Standard library plus the modules in this folder.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_runner import Metrics, build_prompt, run_config
from hardware import Hardware, fingerprint, hardware_from
from prompt_transfer import off_card_share

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA = "hypercore.winner_bench/1"

# The prompt sizes the benchmark walks, in tokens. Anything above the winner's --max-context
# is dropped by ladder_sizes(), so the ladder is safe on a 4K context as well as a 262K one.
# --prompt-ladder overrides it; --no-ladder benches the sweep prompt only.
PROMPT_LADDER: tuple[int, ...] = (512, 1024, 2048, 4096, 8192, 16384)


def utc_stamp() -> str:
    """The stamp every report and log file is named with."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def median(values: list[float]) -> float:
    """Median of a non-empty list; 0.0 for an empty one, so a report never divides on None."""
    return statistics.median(values) if values else 0.0


def load_winner(config: Path) -> dict[str, Any]:
    """The winner run config. Raises SystemExit with the reason when the file is not one."""
    if not config.is_file():
        raise SystemExit(
            f"winner_bench: no run config at {config}. Run a sweep first "
            f"(./optuna_tune/max_tune.sh --model q2_0 --gen) or pass --config <file>."
        )
    try:
        cfg = json.loads(config.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"winner_bench: cannot read {config}: {exc}") from exc
    if not isinstance(cfg, dict) or not isinstance(cfg.get("args"), list):
        raise SystemExit(f"winner_bench: {config} is not a run config (no 'args' list)")
    return cfg


def engine_command(
    cfg: dict[str, Any],
    max_new: int | None = None,
    context: int | None = None,
) -> list[str]:
    """The winner's arguments, put back into the --serve protocol the runner speaks.

    The deliverable drops --serve and --max-new (they belong to the HTTP server, not to a
    direct engine run), so they are added back here; everything else is the config verbatim.
    """
    args = list(cfg.get("args") or [])
    if "--serve" not in args:
        args.insert(0, "--serve")
    if max_new is not None and "--max-new" not in args:
        args += ["--max-new", str(max_new)]
    if context is not None and "--max-context" not in args:
        args += ["--max-context", str(context)]
    return args


def free_gpu_mib() -> float | None:
    """MiB free on the first card, or None when nvidia-smi is not available."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        return float(out[0])
    except ValueError:
        return None


def summarize(runs: list[Metrics], max_new: int = 0) -> dict[str, Any]:
    """Medians over the boots that produced tokens, plus the off-card share of those boots.

    `generated` is recorded because a decode rate is only comparable to another decode rate
    if both ran for the same number of tokens. A request that the model ended after one
    token (finish=stop) reports a tok/s that is a function of the prompt, not of the decode.
    """
    ok = [m for m in runs if m.ok()]
    off = [v for v in (off_card_share(m.transfer) for m in ok) if v is not None]
    generated = [m.generated for m in ok]
    gen_median = median(generated) if generated else 0
    return {
        "boots": len(runs),
        "measured": len(ok),
        "failed": len(runs) - len(ok),
        "gen_tok_s": round(median([m.gen_tok_s for m in ok]), 2),
        "prefill_tok_s": round(median([m.prefill_tok_s for m in ok]), 2),
        "latency_ms": round(median([m.latency_ms for m in ok]), 1),
        "prompt_ms": round(median([m.prompt_ms for m in ok]), 1),
        "decode_ms": round(median([m.decode_ms for m in ok]), 1),
        "off_card_pct": round(median(off), 1) if off else None,
        "off_card_samples": len(off),
        "generated_median": round(gen_median, 1),
        # True when the model stopped on its own well before --max-new: the gen rate on
        # this rung is the answer length, not the decode speed.
        "short_decode": bool(ok) and max_new > 0 and gen_median < 0.5 * max_new,
        "statuses": [m.status for m in runs],
        "notes": [m.note for m in runs if m.note],
    }


def delta(bench: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    """How far this run is from the numbers the sweep recorded for the winner."""
    out: dict[str, Any] = {}
    for key in ("gen_tok_s", "prefill_tok_s", "latency_ms"):
        was = float(recorded.get(key) or 0.0)
        now = float(bench.get(key) or 0.0)
        if was > 0.0 and now > 0.0:
            out[key] = {"recorded": was, "measured": now,
                       "pct": round(100.0 * (now - was) / was, 1)}
    return out


def report_lines(result: dict[str, Any]) -> list[str]:
    """The terminal report. Printed by run_benchmark and reused by the README writer."""
    b = result["bench"]
    rungs = result.get("ladder") or [result]
    sizes = ", ".join(str(r["prompt_tokens"]) for r in rungs)
    lines = [
        f"winner benchmark {result['generated_utc']}  schema {SCHEMA}",
        f"  model    {result['model']}  engine {result['engine']}",
        f"  config   {result['config']}",
        f"  machine  {result['hardware_slug']}",
        (
            f"  context  {result['context']} tokens, prompt ladder {sizes} tokens, "
            f"{b['boots']} boot(s) per size"
        ),
    ]
    for i, run in enumerate(result["runs"], 1):
        off = off_card_share(run.get("transfer", {}))
        off_txt = f"off-card {off:.1f}%" if off is not None else "off-card -"
        lines.append(
            f"  boot {i}: {run['status']} gen {run['gen_tok_s']:.2f} tok/s "
            f"prefill {run['prefill_tok_s']:.2f} tok/s latency {run['latency_ms']:.0f} ms "
            f"{off_txt}" + (f"  [{run['note']}]" if run.get("note") else "")
        )
    if b["measured"]:
        off_txt = f"{b['off_card_pct']:.1f}%" if b["off_card_pct"] is not None else "not reported"
        lines.append(
            f"  median   gen {b['gen_tok_s']:.2f} tok/s prefill {b['prefill_tok_s']:.2f} tok/s "
            f"latency {b['latency_ms']:.0f} ms off-card {off_txt} "
            f"decoded {b.get('generated_median', 0)} tok "
            f"({b['off_card_samples']} of {b['measured']} boots reported decode counters)"
        )
        if b.get("short_decode"):
            lines.append(
                f"  note: the model stopped after {b['generated_median']} tokens of the "
                f"{result['max_new']} asked for, so this gen rate is the answer length, "
                "not the decode speed"
            )
    else:
        lines.append(f"  no boot produced tokens ({', '.join(b['statuses']) or 'no boots'})")
    for key, d in result.get("delta", {}).items():
        lines.append(f"  vs sweep: {key} {d['recorded']:.2f} -> {d['measured']:.2f} "
                    f"({d['pct']:+.1f}%)")
    if len(rungs) > 1:
        lines.append(f"  prompt ladder ({len(rungs)} sizes, the winner's own arguments):")
        for rung in rungs:
            rb = rung["bench"]
            mark = ""
            if rung["prompt_tokens"] == result["prompt_tokens"] and result.get("mode") != "http":
                mark = "  <- the prompt the sweep scored"
            target = rung.get("target_tokens")
            if target and target != rung["prompt_tokens"]:
                mark += f"  (target {target} tokens)"
            if rb["measured"]:
                off = (f"{rb['off_card_pct']:.1f}%" if rb["off_card_pct"] is not None
                       else "not reported")
                lines.append(
                    f"    {rung['prompt_tokens']:>6} tok  prefill {rb['prefill_tok_s']:>9.2f} tok/s "
                    f"gen {rb['gen_tok_s']:>7.2f} tok/s  latency {rb['latency_ms']:>9.0f} ms "
                    f"off-card {off}  decoded {rb.get('generated_median', 0)} tok "
                    f"({rb['measured']}/{rb['boots']} boots measured){mark}"
                )
                if rb.get("short_decode"):
                    lines.append(
                        f"           the model answered in {rb.get('generated_median', 0)} tokens "
                        f"and stopped: this rung's gen rate is not comparable with the others"
                    )
            else:
                lines.append(
                    f"    {rung['prompt_tokens']:>6} tok  no boot produced tokens "
                    f"({', '.join(rb['statuses']) or 'no boots'}){mark}"
                )
    if result.get("ladder_skipped"):
        lines.append(
            "  ladder sizes not run: "
            f"{', '.join(str(s) for s in result['ladder_skipped'])} "
            "(no prompt token file to build them from)"
        )
    return lines


def write_report(result: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """bench-<machine>-<model>-<stamp>.json and its README.md, next to the sweep CSVs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = result["generated_utc"][:19].replace(":", "").replace("-", "")
    base = out_dir / f"bench-{result['hardware_slug']}-{result['model']}-{stamp}"
    js = base.with_suffix(".json")
    js.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    md = base.with_suffix(".md")
    md.write_text(
        "# Winner benchmark\n\n"
        f"Generated {result['generated_utc']} by optuna_tune/winner_bench.py.\n\n"
        "```\n" + "\n".join(report_lines(result)) + "\n```\n\n"
        f"Engine logs: {', '.join(r['log'] for r in result['runs'])}\n",
        encoding="utf-8",
    )
    return js, md


def ladder_sizes(requested: Sequence[int], context: int) -> list[int]:
    """The prompt sizes to bench, clipped to the context the winner runs with.

    A rung larger than --max-context is refused by the engine, so it is dropped rather than
    run and failed. Sizes are de-duplicated and sorted; a non-positive or unparseable entry
    is ignored.
    """
    sizes: list[int] = []
    for raw in requested:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0 and (context <= 0 or n <= context):
            sizes.append(n)
    return sorted(set(sizes))


def run_boots(
    cfg: dict[str, Any],
    engine: str,
    prompt_ids: list[int],
    max_new: int,
    repeat: int,
    log_dir: Path,
    model: str,
    machine_rate: float | None,
    boot_timeout: int,
    run_timeout: int,
) -> dict[str, Any]:
    """One rung of the ladder: boot the winner `repeat` times at one prompt size."""
    args = engine_command(cfg, max_new=max_new)
    runs: list[Metrics] = []
    for i in range(max(1, repeat)):
        log = log_dir / f"bench-{utc_stamp()}-{model}-{len(prompt_ids)}-{i}.log"
        runs.append(run_config(
            engine=engine, args=args, prompt_ids=prompt_ids, max_new=max_new,
            log_path=log, boot_timeout=boot_timeout, run_timeout=run_timeout,
            machine_rate=machine_rate,
        ))
    bench = summarize(runs, max_new=max_new)
    recorded = (cfg.get("tuned_by") or {}).get("metrics", {})
    return {
        "prompt_tokens": len(prompt_ids),
        "args": args,
        "bench": bench,
        "runs": [
            {
                "status": m.status,
                "gen_tok_s": m.gen_tok_s,
                "prefill_tok_s": m.prefill_tok_s,
                "latency_ms": m.latency_ms,
                "prompt_ms": m.prompt_ms,
                "decode_ms": m.decode_ms,
                "generated": m.generated,
                "prompt_tokens": m.prompt_tokens,
                "transfer": m.transfer,
                "note": m.note,
                "log": m.log_path,
            }
            for m in runs
        ],
        "delta": delta(bench, recorded),
    }


def run_benchmark(
    config: Path,
    engine: str,
    prompt_ids: list[int],
    max_new: int,
    repeat: int,
    log_dir: Path,
    model: str,
    hw: Hardware,
    machine_rate: float | None = None,
    boot_timeout: int = 300,
    run_timeout: int = 180,
    prompt_file: str | None = None,
    ladder: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Boot the winner at the sweep prompt and at every prompt size the ladder allows.

    `result["bench"]` is always the rung the sweep scored (the prompt it used), so the
    comparison against the sweep record stays meaningful; `result["ladder"]` holds one block
    per prompt size, in ascending order. Without a prompt file the ladder cannot be built and
    only the sweep prompt is run.
    """
    cfg = load_winner(config)
    args = engine_command(cfg, max_new=max_new)
    context_raw = next((args[i + 1] for i, a in enumerate(args) if a == "--max-context"), "0")
    try:
        context = int(context_raw)
    except ValueError:
        context = 0

    rungs: list[dict[str, Any]] = []
    sizes = sorted({len(prompt_ids), *ladder_sizes(ladder or (), context)})
    skipped: list[int] = []
    for size in sizes:
        ids = prompt_ids if size == len(prompt_ids) else None
        if ids is None:
            if not prompt_file:
                skipped.append(size)
                continue
            ids = build_prompt(prompt_file, size)
        rungs.append(run_boots(cfg, engine, ids, max_new, repeat, log_dir, model,
                              machine_rate, boot_timeout, run_timeout))
    primary = next(r for r in rungs if r["prompt_tokens"] == len(prompt_ids))
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "engine": engine,
        "config": str(config),
        "hardware": hw.to_dict(),
        "hardware_slug": hw.slug(),
        "args": args,
        "context": context_raw,
        "prompt_tokens": len(prompt_ids),
        "max_new": max_new,
        "repeat": max(1, repeat),
        "ladder": rungs,
        "ladder_skipped": skipped,
        "bench": primary["bench"],
        "runs": primary["runs"],
        "recorded": (cfg.get("tuned_by") or {}).get("metrics", {}),
    }
    result["delta"] = primary["delta"]
    return result


# A prompt the HTTP ladder can send: the engine takes text over the API, not token ids, so
# the size is reached by repeating this line and reading back the token count the engine
# actually counted. Filler rather than a real question on purpose: the ladder is about the
# prompt path, and a fixed text makes the rungs comparable.
FILLER_SENTENCE: str = (
    "The benchmark measures the prompt path: read the pack, fill the expert cache, and "
    "answer the question. "
)


def server_base(cfg: dict[str, Any], override: str | None = None) -> str:
    """The URL of the server the winner config is already running, empty if it has no port."""
    if override:
        return override.rstrip("/")
    port = cfg.get("port")
    if not port:
        return ""
    host = cfg.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def http_request(base: str, key: str, model: str, text: str, max_new: int,
                 timeout: int = 180) -> dict[str, Any]:
    """One request to a running server, returning the engine's own timings.

    The API carries prompt_ms/prompt_per_second and predicted_ms/predicted_per_second but no
    decode expert counters, so a run measured this way has no off-card share: it is reported
    as not measured rather than as 0%.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": max_new,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"status": "FAIL_HTTP", "note": f"{base}: {exc}", "transfer": {}}
    timings = payload.get("timings") or {}
    usage = payload.get("usage") or {}
    if not timings:
        return {"status": "NO_TIMINGS", "note": "the answer carried no timings block",
                "transfer": {}}
    prompt_ms = float(timings.get("prompt_ms") or 0.0)
    decode_ms = float(timings.get("predicted_ms") or 0.0)
    return {
        "status": "PASS",
        "prompt_tokens": int(usage.get("prompt_tokens") or timings.get("prompt_n") or 0),
        "prompt_ms": prompt_ms,
        "decode_ms": decode_ms,
        "prefill_tok_s": float(timings.get("prompt_per_second") or 0.0),
        "gen_tok_s": float(timings.get("predicted_per_second") or 0.0),
        "generated": int(usage.get("completion_tokens") or timings.get("predicted_n") or 0),
        "latency_ms": prompt_ms + decode_ms,
        "transfer": {},
        "note": "",
        "log": "",
    }


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Medians over the requests that answered. Same shape as summarize(), no decode counters."""
    ok = [r for r in rows if r.get("status") == "PASS"]
    return {
        "boots": len(rows),
        "measured": len(ok),
        "failed": len(rows) - len(ok),
        "gen_tok_s": round(median([r["gen_tok_s"] for r in ok]), 2),
        "prefill_tok_s": round(median([r["prefill_tok_s"] for r in ok]), 2),
        "latency_ms": round(median([r["latency_ms"] for r in ok]), 1),
        "prompt_ms": round(median([r["prompt_ms"] for r in ok]), 1),
        "decode_ms": round(median([r["decode_ms"] for r in ok]), 1),
        "off_card_pct": None,
        "off_card_samples": 0,
        "statuses": [r.get("status", "UNKNOWN") for r in rows],
        "notes": [r.get("note", "") for r in rows if r.get("note")],
    }


def run_http_benchmark(base: str, key: str, model_name: str, sizes: Sequence[int], repeat: int,
                       max_new: int, hw: Hardware, config: Path, model_slug: str = "",
                       context: str = "", timeout: int = 180) -> dict[str, Any]:
    """The prompt ladder against a server that is already up, instead of booting a second one.

    Two probe requests calibrate what the filler is worth - one alone and three - so the chat
    template's own tokens are subtracted and each rung lands near the size asked for. The
    report prints the token count the engine actually counted.
    """
    one = http_request(base, key, model_name, FILLER_SENTENCE, 8, timeout)
    three = http_request(base, key, model_name, FILLER_SENTENCE * 3, 8, timeout)
    per_filler = 0.0
    overhead = 0.0
    if one.get("status") == "PASS" and three.get("status") == "PASS":
        per_filler = (three["prompt_tokens"] - one["prompt_tokens"]) / 2.0
        overhead = one["prompt_tokens"] - per_filler
    rungs: list[dict[str, Any]] = []
    for size in sorted({int(s) for s in sizes if int(s) > 0}):
        if per_filler > 0:
            reps = max(1, round((size - overhead) / per_filler))
        else:
            reps = max(1, size // 20)   # uncalibrated: a rough words-to-tokens guess
        text = FILLER_SENTENCE * reps
        rows = [http_request(base, key, model_name, text, max_new, timeout)
                for _ in range(max(1, repeat))]
        bench = summarize_rows(rows)
        rungs.append({
            "prompt_tokens": max((r["prompt_tokens"] for r in rows if r.get("status") == "PASS"),
                                 default=size),
            "target_tokens": size,
            "args": ["--http", base],
            "bench": bench,
            "runs": rows,
            "delta": {},
        })
    primary = rungs[0] if rungs else {"bench": summarize_rows([]), "runs": [], "delta": {}}
    return {
        "schema": SCHEMA,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "model": model_slug or model_name,
        "model_name": model_name,
        "engine": base,
        "config": str(config),
        "hardware": hw.to_dict(),
        "hardware_slug": hw.slug(),
        "args": ["--http", base],
        "context": context,
        "prompt_tokens": primary["prompt_tokens"],
        "max_new": max_new,
        "repeat": max(1, repeat),
        "mode": "http",
        "ladder": rungs,
        "ladder_skipped": [],
        "bench": primary["bench"],
        "runs": primary["runs"],
        "recorded": {},
        "delta": {},
    }


def parse_ladder(text: str) -> list[int]:
    """Split `512,1024,2048` into prompt sizes; anything unparseable is dropped."""
    out: list[int] = []
    for part in str(text).replace(" ", "").split(","):
        if part.isdigit() and int(part) > 0:
            out.append(int(part))
    return out


def context_of(args: list[str]) -> int:
    """The --max-context the winner config carries, 0 when it does not say."""
    raw = next((args[i + 1] for i, a in enumerate(args) if a == "--max-context"), "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The benchmark's own flags; anything a sweep would pass is ignored, not fatal."""
    p = argparse.ArgumentParser(
        description="Benchmark the winning configuration written by the Optuna sweep.")
    p.add_argument("--model", default="q2_0", help="quant the deliverable is named after")
    p.add_argument("--config", default=None,
                   help="the run config to benchmark; default max_run_<model>.json at the root")
    p.add_argument("--engine", default=None,
                   help="the engine binary; default the one the config recorded")
    p.add_argument("--repeat", type=int, default=3, help="boots to measure; medians reported")
    p.add_argument("--max-new", type=int, default=512, help="tokens generated per request")
    p.add_argument("--prompt-tokens", type=int, default=10240,
                   help="prompt length; must be >8192 for the tail arm to differ")
    p.add_argument("--prompt-tokens-file", default=None, help="file of token ids to cycle")
    p.add_argument("--prompt-ladder", default=",".join(str(s) for s in PROMPT_LADDER),
                   help="comma-separated prompt sizes to bench after the sweep prompt; "
                        "anything above the winner's --max-context is skipped")
    p.add_argument("--no-ladder", action="store_true",
                   help="bench only the prompt the sweep scored, no prompt ladder")
    p.add_argument("--http", nargs="?", const="auto", default=None,
                   help="benchmark a server that is already up instead of booting a second "
                        "one: bare --http uses the host and port in the run config, or pass "
                        "a URL. The API carries no decode expert counters, so the off-card "
                        "share is reported as not measured on this path.")
    p.add_argument("--results-dir", default=str(HERE / "results"),
                   help="where the bench report and logs go")
    p.add_argument("--hardware", default=None, help="pin the machine fingerprint (JSON or file)")
    p.add_argument("--no-report", action="store_true", help="print the report, write no files")
    p.add_argument("--dry-run", action="store_true",
                   help="show the command the benchmark would run, boot nothing")
    args, unknown = p.parse_known_args(argv)
    unknown = [u for u in unknown if u != "--benchmark"]  # the flag that routed here from max_tune.sh
    if unknown:
        # max_tune.sh passes its own flags through, so a sweep flag reaching here is ignored
        # rather than fatal: the benchmark needs only the config it was given.
        print(f"note: benchmark ignores {unknown}", file=sys.stderr)
    args.root = ROOT
    if args.repeat < 1:
        p.error("--repeat must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Read the winner config, boot it, report medians. Exit 0 only when a boot measured."""
    args = parse_args(argv)
    config = Path(args.config) if args.config else args.root / f"max_run_{args.model}.json"
    cfg = load_winner(config)
    engine = args.engine or cfg.get("exe") or str(ROOT / "engine" / "strata")
    hw = fingerprint() if not args.hardware else hardware_from(args.hardware)
    if hw is None:
        raise SystemExit("winner_bench: could not probe this machine")

    prompt_file = args.prompt_tokens_file
    if not prompt_file:
        from search_space import resolve_model_paths

        prompt_file = resolve_model_paths(args.root, args.model, args.root / "data").prompt_tokens
    if not prompt_file:
        raise SystemExit("winner_bench: no prompt token file found; pass --prompt-tokens-file")

    args_list = engine_command(cfg, max_new=args.max_new)
    base = "" if args.http is None else server_base(cfg, None if args.http == "auto" else args.http)
    if args.dry_run:
        print(f"config:  {config}")
        print(f"engine:  {base or engine}")
        if base:
            print(f"ladder:  {', '.join(str(s) for s in parse_ladder(args.prompt_ladder)) or 'none'} "
                  f"against {base}")
        else:
            print(f"command: {engine} " + " ".join(args_list))
            ladder = () if args.no_ladder else parse_ladder(args.prompt_ladder)
            print(f"ladder:  {', '.join(str(s) for s in ladder_sizes(ladder, context_of(args_list))) or 'none'}")
        return 0

    if base:
        ladder = () if args.no_ladder else parse_ladder(args.prompt_ladder)
        sizes = ladder_sizes(ladder, 0) or [args.prompt_tokens]
        print(f"[*] benchmarking the running server: {base}")
        print("[*] the API reports timings only, so the off-card share is not measured here")
        result = run_http_benchmark(base, str(cfg.get("api_key", "")),
                                   str(cfg.get("model_name", args.model)), sizes,
                                   args.repeat, args.max_new, hw, config, model_slug=args.model,
                                   context=str(context_of(args_list)))
        print("\n".join(report_lines(result)))
        if not args.no_report:
            js, md = write_report(result, Path(args.results_dir))
            print(f"report:  {js}")
            print(f"         {md}")
        return 0 if result["bench"]["measured"] else 1

    free = free_gpu_mib()
    if free is not None and free < 12000:
        print(f"winner_bench: the GPU has {free:.0f} MiB free. Stop the running server first "
              "(./restart.sh stop); the benchmark will fail to boot.", file=sys.stderr)

    print(f"[*] benchmarking the winner: {config}")
    print(f"[*] engine {engine} on {hw.slug()}")
    result = run_benchmark(
        config=config, engine=engine, prompt_ids=build_prompt(prompt_file, args.prompt_tokens),
        max_new=args.max_new, repeat=args.repeat, log_dir=Path(args.results_dir),
        model=args.model, hw=hw, prompt_file=str(prompt_file),
        ladder=() if args.no_ladder else parse_ladder(args.prompt_ladder),
    )
    print("\n".join(report_lines(result)))
    if not args.no_report:
        js, md = write_report(result, Path(args.results_dir))
        print(f"report:  {js}")
        print(f"         {md}")
    return 0 if result["bench"]["measured"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
