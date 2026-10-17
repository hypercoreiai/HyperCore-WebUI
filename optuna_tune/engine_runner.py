"""optuna_tune/engine_runner.py - boot the engine once, measure one request.

The engine is not an HTTP server: with --serve it reads "GEN <max_new> <token ids>" on
stdin and answers on stdout (src/program/generate.cpp:5128-5140). The telemetry we need is
in two lines it prints:

    INFO context=... kv=... pool_workers=... pcie_frac=...      what it actually applied
    DONE <generated> <prompt> <prompt_ms> <decode_ms> <finish> <drafts accepted>
         <drafts offered> <reused> <hits> <lookups> <ram blobs> <file blobs> <file MB>
         <prompt tokens read> <offloaded>                        src/program/generate.cpp:10192

Everything is parsed with an explicit field table rather than a loose regex, so a change in
the DONE line raises instead of silently returning a wrong number.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_transfer import PromptTransfer, parse_machine_rate

try:  # psutil is in requirements.txt; without it the ambient cross-check is simply absent.
    import psutil
except ImportError:  # pragma: no cover - the fallback keeps this module importable anywhere
    psutil = None  # type: ignore[assignment]

DONE_MIN_FIELDS = 6

# index -> name, for the fields we read. Positions past 15 are the --lookup-chain tail.
# 9-13 and 15 are the transfer counters the balance objective needs; a run that stops
# before them still parses, because parse_done only reads indices present in the line.
DONE_FIELDS: dict[int, str] = {
    1: "generated", 2: "prompt_tokens", 3: "prompt_ms", 4: "decode_ms", 5: "finish",
    6: "drafts_accepted", 7: "drafts_offered", 8: "reused",
    9: "cache_hits", 10: "cache_lookups", 11: "ram_blobs", 12: "file_blobs",
    13: "file_mb", 14: "prompt_read", 15: "offloaded",
}


def ambient_snapshot() -> dict[str, float]:
    """Free RAM, swap in use, and cumulative block-device reads at this instant.

    The engine pins a RAM tier for the expert pool; the q2_0 pack is 35.47 GB on disk on a
    31 GB host, so swap in use is the clearest sign that a measured prefill is set by
    page-cache pressure rather than by the swept knobs. A probe that cannot answer returns
    an empty dict rather than raising: the sweep needs a value for every trial.
    """
    if psutil is None:
        return {}
    try:
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_io_counters()
    except (RuntimeError, OSError):
        return {}
    return {
        "free_ram_mb": round(mem.available / 1_000_000.0, 1),
        "total_ram_mb": round(mem.total / 1_000_000.0, 1),
        "swap_used_mb": round(swap.used / 1_000_000.0, 1),
        "disk_read_bytes": float(disk.read_bytes) if disk else 0.0,
    }


class EngineError(RuntimeError):
    """The engine could not be started, or produced nothing we can measure."""


@dataclass
class Metrics:
    """One measured configuration. Rates are tok/s; latency is the request wall time."""

    status: str = "FAIL"
    gen_tok_s: float = 0.0
    prefill_tok_s: float = 0.0
    latency_ms: float = 0.0
    prompt_ms: float = 0.0
    decode_ms: float = 0.0
    prompt_tokens: int = 0
    prompt_read: int = 0
    generated: int = 0
    finish: str = ""
    effective: dict[str, str] = field(default_factory=dict)
    transfer: dict[str, float] = field(default_factory=dict)
    ambient: dict[str, float] = field(default_factory=dict)
    log_path: str = ""
    wall_ms: float = 0.0
    boot_ms: float = 0.0
    disk_read_mb: float = 0.0
    note: str = ""

    def ok(self) -> bool:
        return self.status == "PASS"


def parse_done(line: str) -> dict[str, Any]:
    """Split a DONE line into named fields. Raises EngineError on a malformed line."""
    parts = line.split()
    if len(parts) < DONE_MIN_FIELDS:
        raise EngineError(f"malformed DONE line ({len(parts)} fields): {line.strip()}")
    out: dict[str, Any] = {}
    for idx, name in DONE_FIELDS.items():
        if idx < len(parts):
            out[name] = parts[idx]
    try:
        out["generated"] = int(out["generated"])
        out["prompt_tokens"] = int(out["prompt_tokens"])
        out["prompt_ms"] = float(out["prompt_ms"])
        out["decode_ms"] = float(out["decode_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EngineError(f"unreadable DONE line: {line.strip()}") from exc
    return out


def parse_info(line: str) -> dict[str, str]:
    """key=value pairs from the INFO line, so a degraded arm is visible in the record."""
    return {k.split("=")[0]: k.split("=", 1)[1] for k in line.split()[1:] if "=" in k}


def failure_reason(log_text: str) -> str:
    """The engine's own last complaint, used to label a failed row."""
    for needle, label in (
        ("does not fit", "NO_VRAM_FOR_MTP: the draft head does not fit at this --vram-reserve-mib"),
        ("out of memory", "NO_VRAM: a device allocation failed"),
        ("mlock failed", "PLE table could not be pinned; the ram arm degrades to mmap"),
    ):
        if needle in log_text:
            return label
    for line in reversed(log_text.splitlines()):
        if line.startswith(("hypercore ", "error")):
            return line.strip()
    return "the engine exited with no message"


def build_prompt(prompt_file: str, tokens: int) -> list[int]:
    """A deterministic prompt of `tokens` ids, cycled from the bench token file."""
    ids = json.loads(Path(prompt_file).read_text(encoding="utf-8"))["input"]
    if not ids:
        raise EngineError(f"prompt file {prompt_file} carries no token ids")
    out: list[int] = []
    while len(out) < tokens:
        out.extend(ids)
    return out[:tokens]


def attach_transfer(metrics: Metrics, done: dict[str, Any], lines: Iterable[str],
                    machine_rate: float | None = None) -> None:
    """Record the prompt-path traffic on a measured run.

    `machine_rate` overrides the engine's own boot measurement so a whole sweep is scored
    against one anchor; without it the run's own boot line is used, and with neither the
    transfer stays inert.
    """
    rate = machine_rate or parse_machine_rate(lines)
    metrics.transfer = PromptTransfer.from_done(done, rate).as_dict()


def run_config(
    engine: str,
    args: list[str],
    prompt_ids: list[int],
    max_new: int,
    log_path: Path,
    boot_timeout: int = 300,
    run_timeout: int = 180,
    extra_env: dict[str, str] | None = None,
    machine_rate: float | None = None,
    ambient_sampler: Callable[[], dict[str, float]] = ambient_snapshot,
) -> Metrics:
    """Boot the engine with `args`, send one GEN request, return its metrics.

    A boot that never reaches "session is up", a process that exits, a request that produces
    no tokens, and a timeout are each recorded as their own status rather than as a crash:
    the optimizer needs a value for every point it tried.
    """
    metrics = Metrics(log_path=str(log_path))
    t_start = time.monotonic()
    ambient_before = ambient_sampler()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")

    env = dict(extra_env or {})
    try:
        # Binary pipes on purpose. bufsize=0 gives an unbuffered raw handle for stdin, so
        # there is no TextIOWrapper whose flush-at-GC can raise BrokenPipeError after the
        # engine has exited - the "Exception ignored while finalizing file" noise.
        proc = subprocess.Popen(
            [engine] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env or None,
        )
    except OSError as exc:
        metrics.status = "FAIL_EXEC"
        metrics.note = f"could not start {engine}: {exc}"
        return metrics

    raw_out, raw_in = proc.stdout, proc.stdin
    if raw_out is None:
        metrics.status = "FAIL_EXEC"
        metrics.note = "the engine has no stdout to read"
        return metrics

    def send(line: str) -> bool:
        """Write one request line to the engine. False if its stdin is already gone."""
        if raw_in is None:
            return False
        try:
            raw_in.write(line.encode("utf-8"))
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    fd = raw_out.fileno()
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    pending = b""
    lines: list[str] = []
    done: dict[str, Any] | None = None
    boot_deadline = t_start + boot_timeout
    run_deadline = boot_deadline + run_timeout
    sent = False
    eof = False

    def handle(line: str) -> None:
        nonlocal sent, done
        lines.append(line)
        if line.startswith("INFO "):
            metrics.effective = parse_info(line)
        if not sent and "session is up" in line:
            metrics.boot_ms = (time.monotonic() - t_start) * 1000.0
            sent = send(f"GEN {max_new} {','.join(map(str, prompt_ids))}\n")
        if line.startswith("DONE "):
            done = parse_done(line)

    try:
        while time.monotonic() < run_deadline and not eof:
            # Read the raw fd, never a buffered wrapper: a buffered reader can hold data
            # that select() cannot see, and the two together deadlock.
            if not sel.select(timeout=0.5):
                if not sent and time.monotonic() > boot_deadline:
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                eof = True
                break
            if not chunk:
                eof = True
                break
            pending += chunk
            while b"\n" in pending:
                raw_line, pending = pending.split(b"\n", 1)
                handle(raw_line.decode("utf-8", "replace"))
                if done is not None:
                    break
            if done is not None:
                break
    except (BrokenPipeError, ValueError) as exc:
        metrics.note = f"the engine closed its output: {exc}"
    finally:
        sel.close()
        # QUIT only while the engine is alive; a dead engine's stdin raises EPIPE.
        if proc.poll() is None:
            send("QUIT\n")
        for pipe in (raw_out, raw_in):
            if pipe is not None:
                try:
                    pipe.close()
                except (BrokenPipeError, ValueError, OSError):
                    pass
        if proc.poll() is None:
            proc.terminate()
            deadline = time.monotonic() + 15
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.5)
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    log_text = "\n".join(lines)
    log_path.write_text(log_text, encoding="utf-8")
    metrics.wall_ms = (time.monotonic() - t_start) * 1000.0
    ambient_after = ambient_sampler()
    if ambient_before:  # a probe that cannot answer leaves the ambient block empty
        metrics.ambient = {
            "free_ram_mb": float(ambient_before.get("free_ram_mb", 0.0)),
            "total_ram_mb": float(ambient_before.get("total_ram_mb", 0.0)),
            "swap_used_mb": float(ambient_before.get("swap_used_mb", 0.0)),
        }
    before_bytes = ambient_before.get("disk_read_bytes")
    after_bytes = ambient_after.get("disk_read_bytes")
    if before_bytes is not None and after_bytes is not None and after_bytes >= before_bytes:
        metrics.disk_read_mb = (after_bytes - before_bytes) / 1_000_000.0

    if done is None:
        metrics.status = "FAIL_BOOT" if not sent else "TIMEOUT"
        metrics.note = failure_reason(log_text)
        return metrics

    prompt_ms = float(done["prompt_ms"])
    decode_ms = float(done["decode_ms"])
    generated = int(done["generated"])
    prompt_tokens = int(done["prompt_tokens"])
    read_n = int(done.get("prompt_read") or 0)
    if read_n <= 0:
        read_n = max(0, prompt_tokens - int(done.get("reused") or 0))

    if generated <= 0:
        metrics.status = "NO_TOKENS"
        metrics.note = failure_reason(log_text)
        metrics.finish = str(done.get("finish", ""))
        return metrics

    metrics.status = "PASS"
    metrics.generated = generated
    metrics.prompt_tokens = prompt_tokens
    metrics.prompt_read = read_n
    metrics.prompt_ms = prompt_ms
    metrics.decode_ms = decode_ms
    metrics.finish = str(done.get("finish", ""))
    metrics.gen_tok_s = 1000.0 * generated / decode_ms if decode_ms > 0 else 0.0
    metrics.prefill_tok_s = 1000.0 * read_n / prompt_ms if prompt_ms > 0 else 0.0
    metrics.latency_ms = prompt_ms + decode_ms
    # The engine measured this machine's pack read rate during boot; without that line the
    # transfer stays inert and the balance objective falls back to the plain rate.
    attach_transfer(metrics, done, lines, machine_rate)
    return metrics
