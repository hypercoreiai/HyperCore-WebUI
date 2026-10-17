#!/usr/bin/env python3
"""serve/direct_engine.py - drive a Strata engine over its stdin/stdout protocol.

This is the path that makes the promise "the project only needs the Strata engine"
true. No shared library, no worker binary: just the engine and the text protocol it
already speaks (src/program/generate.cpp):

    stdin :  GEN <max_new> <id,id,...>   STOP   QUIT   VRAM [reserve_mib]
    stdout:  READY <max_context> stop
             INFO ... engine=<version>
             T <id>                       one token, as its verify window finishes
             PP <done> <total> <ms> <tok/s>
             RESUME <n> / REUSED <n>     prompt tokens not read again
             DONE <generated> <prompt_tokens> <prompt_ms> <decode_ms> <finish> ...
             VRAM expert_slots=... vram_free_mib=...
             ERR <message>

The third field of READY is the literal word "stop", not a token id: it says this
engine honours a STOP line in the middle of a request. Reading it as an integer is
what made the engine refuse to start.

The worker path (serve/engine_supervisor.py) is preferred when hypercore-worker is
present, because an engine abort then takes down the worker rather than this process.
When it is not built, this is the fallback, and the Monitor says so.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Iterator

# The engine's own words for a verifier that cannot go on (#267): the process may
# stay alive and even print DONE, but it must not take another request.
FATAL_PREFIXES = ("ERR verify: timed out at layer ", "ERR verify batch: timed out at layer ",
                  "ERR verify: an earlier window never finished",
                  "ERR verify batch: an earlier window never finished")

READY_S = float(os.environ.get("STRATA_ENGINE_READY_S", "900.0"))


class DirectEngineError(RuntimeError):
    pass


class DirectEngine:
    """A resident Strata engine, spoken to line by line."""

    def __init__(self, exe: str, args: list[str], startup_timeout_s: float | None = None,
                 env: dict | None = None, log_path: str | None = None):
        self.exe = exe
        self.args = list(args) + ["--serve"]
        self.startup_timeout_s = READY_S if startup_timeout_s is None else float(startup_timeout_s)
        self.env = env or {}
        self.log_path = log_path
        self._temp_log: str | None = None
        self.proc: subprocess.Popen | None = None
        self.max_context = 0
        self.can_stop = False          # READY <ctx> stop: a STOP line ends a request mid-flight
        self.stop_token = 0            # the engine has no stop-token id; STOP is the stop
        self.version = None
        self.info: dict[str, object] = {}
        self.last: dict[str, object] | None = None
        self.progress: tuple[int, int] | None = None
        self.progress_ms = 0.0
        self.prefill_tok_s_mean: float | None = None
        self.reused = 0
        self.last_err = ""
        self.ended = True              # no engine process until start() says READY
        self.unloaded = False
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- the pipe
    def _write(self, line: str) -> None:
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise DirectEngineError(self._exit_reason()) from None

    def _read_line(self) -> str:
        line = self.proc.stdout.readline()
        if not line:
            raise DirectEngineError(self._exit_reason())
        return line.rstrip("\n")

    def _exit_reason(self) -> str:
        """Why the engine is gone, in its own words.

        "the engine closed its stdout" tells a user nothing; the engine's last lines do.
        They are read from the log the config names, or from the temporary one this class
        made, so the reason is reported rather than guessed at.
        """
        try:
            code = self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - the process is already gone; the code is a nicety
            code = None
        tail: list[str] = []
        for path in (self.log_path, self._temp_log):
            if not path:
                continue
            try:
                with open(path, "r", errors="replace") as f:
                    tail = [l.rstrip("\n") for l in f if l.strip()]
                break
            except OSError:
                continue
        tail = tail[-8:]
        reason = "\n  ".join(tail) if tail else "it said nothing"
        hint = ""
        joined = " ".join(tail).lower()
        if "memory guard" in joined or "out of memory" in joined:
            hint = ("\n  the machine has no room for a second engine: stop the other one "
                   "(or the tune) and start this again.")
        return f"the engine exited{'' if code is None else ' with code ' + str(code)}: {reason}{hint}"

    @staticmethod
    def _kv(line: str) -> dict[str, object]:
        out: dict[str, object] = {}
        for token in line.split()[1:]:
            key, _, value = token.partition("=")
            if key:
                out[key] = int(value) if value.lstrip("-").isdigit() else value
        return out

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        with self.lock:
            if self.proc and self.proc.poll() is None and not self.ended:
                return True
            log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
            if not self.log_path:                      # no log named by the config: keep the engine's
                import tempfile                        # own words somewhere we can read back
                fd, self._temp_log = tempfile.mkstemp(prefix="strata-", suffix=".log")
                os.close(fd)
                log = open(self._temp_log, "ab")
            try:
                self.proc = subprocess.Popen(
                    [self.exe, *self.args],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, errors="replace", bufsize=1, env=self.env or None,
                )
            except OSError as exc:
                raise DirectEngineError(f"could not start {self.exe}: {exc}") from None
            self.ended = False
            self.unloaded = False
            self.progress = None
            self.reused = 0

            deadline = time.time() + self.startup_timeout_s
            while time.time() < deadline:
                line = self._read_line()
                # The engine describes itself before it says READY; capture that first so
                # the About tab can show the version and the Monitor can show what the
                # engine actually applied, whichever order the lines arrive in.
                if line.startswith(("INFO", "VRAM")):
                    self.version = self.version or _field(line, "engine")
                    self.info.update(self._kv(line))
                elif line.startswith("READY"):
                    parts = line.split()
                    self.max_context = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
                    # "stop" is not a token id: it is the engine saying it honours STOP.
                    self.can_stop = "stop" in parts[2:]
                    if self.max_context <= 0:
                        raise DirectEngineError(f"the engine reported a context of {self.max_context}")
                    return True
                elif line.startswith("ERR"):
                    self.last_err = line[4:].strip()
                    raise DirectEngineError(self.last_err or "the engine reported an error")
            raise DirectEngineError(
                f"the engine did not reach READY within {self.startup_timeout_s:.0f} s")

    def alive(self) -> bool:
        proc = self.proc
        return proc is not None and not self.ended and proc.poll() is None

    def exit_code(self) -> int | None:
        if self.proc is None:
            return None
        try:
            return self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return None

    def death_note(self) -> str:
        return self.last_err or self._exit_reason()

    def stop(self) -> None:
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                return
            try:
                self.proc.stdin.write("QUIT\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
            self.ended = True
            if self._temp_log:
                try:
                    os.remove(self._temp_log)
                except OSError:
                    pass
                self._temp_log = None

    def unload(self) -> None:
        """Release the GPU between requests; the next request starts the engine again."""
        self.stop()
        self.unloaded = True

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def vram(self, reserve_mib: int | None, timeout: float = 120.0) -> dict:
        """`VRAM <reserve_mib>` between requests: the engine resizes its expert cache to
        leave that much VRAM free. Returns the engine's figures."""
        if not self.alive():
            raise DirectEngineError("the engine is not running")
        with self.lock:
            self._write("VRAM" + ("" if reserve_mib is None else f" {int(reserve_mib)}"))
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self._read_line().strip()
                if line.startswith("ERR"):
                    raise ValueError(line[4:].strip() or "the engine refused the VRAM command")
                if line.startswith("VRAM "):
                    out = self._kv(line)
                    self.info.update(out)
                    self.info["vram"] = out
                    return out
        raise DirectEngineError("the engine did not answer the VRAM command")

    def slots_view(self) -> list[dict[str, object]]:
        return []

    def session_file(self, action: str, path: str) -> dict:
        return {"status": "unsupported", "note": "the direct engine has no session files"}

    # ---------------------------------------------------------------- requests
    def generate(self, ids: list[int], max_new: int, sampling: dict | None = None,
                 cancel: threading.Event | None = None, embeddings=None) -> Iterator[int]:
        """One request on the control lines: GEN ... in, T <id> out, DONE at the end.

        Yields token ids, and None as a heartbeat while the engine is quiet (the
        server turns those into keep-alives). When the consumer stops early - a stop
        token, a reasoning budget, the Stop button - the request is STOPped and drained
        to its DONE line, so the next request does not read this one's leftovers.
        """
        if cancel is None:
            cancel = threading.Event()
        with self.lock:
            if not self.alive():
                raise DirectEngineError("the engine is not running")
            self.progress, self.progress_ms, self.reused = None, 0.0, 0
            head = (f"GENI {int(max_new)}{_sampling_keys(sampling or {})} {embeddings}"
                    if embeddings else f"GEN {int(max_new)}{_sampling_keys(sampling or {})}")
            try:
                self._write(f"{head} {','.join(str(int(t)) for t in ids)}")
            except DirectEngineError:
                raise DirectEngineError(
                    f"the engine stopped unexpectedly (exit code {self.exit_code()})") from None

            done = False
            try:
                while True:
                    line = self._read_line()
                    if line.startswith(FATAL_PREFIXES):        # #267: the verifier's GPU waits were released
                        self.ended = True
                        self.last_err = line[4:].strip()
                        try:
                            self.proc.terminate()
                        except OSError:
                            pass
                        raise DirectEngineError("Unrecoverable native verification failure: "
                                              + line[4:].strip())
                    if line.startswith("T "):
                        if cancel.is_set():
                            return
                        f = line.split()
                        if f[1].lstrip("-").isdigit():
                            yield int(f[1])
                    elif line.startswith("PP "):
                        f = line.split()
                        if len(f) >= 3 and f[1].isdigit() and f[2].isdigit():
                            self.progress = (int(f[1]), int(f[2]))
                            self.progress_ms = float(f[3]) if len(f) > 3 else 0.0
                            self.prefill_tok_s_mean = float(f[4]) if len(f) > 4 else None
                        if cancel.is_set():                    # a long prompt would otherwise send no keep-alives
                            return
                        yield None
                    elif line.startswith("RESUME "):           # prompt tokens not read again
                        try:
                            self.reused = int(line.split()[1])
                        except (IndexError, ValueError):
                            pass
                    elif line.startswith("VRAM "):
                        self.info.update(self._kv(line))
                    elif line.startswith("DONE"):
                        self._parse_done(line)
                        done = True
                        return
                    elif line.startswith("ERR"):
                        done = True
                        self.last_err = line[4:].strip()
                        raise DirectEngineError(self.last_err or "the engine reported an error")
            finally:
                if not done:                                   # the consumer left early: stop it and drain
                    if self.can_stop:
                        try:
                            self._write("STOP")
                        except DirectEngineError:
                            pass
                    while True:
                        try:
                            line = self._read_line()
                        except DirectEngineError:
                            break
                        if line.startswith("DONE"):
                            self._parse_done(line)
                            break
                        if line.startswith("ERR"):
                            self.last_err = line[4:].strip()
                            break

    def _parse_done(self, line: str) -> None:
        """The DONE line's fields, in the names the Monitor and the history rows use."""
        f = line.split()
        if len(f) < 6:
            return
        self.last = {"generated": int(f[1]), "prompt_tokens": int(f[2]),
                    "prompt_ms": float(f[3]), "decode_ms": float(f[4]), "finish": f[5]}
        if len(f) >= 9:
            self.last.update(drafts_accepted=int(f[6]), drafts_offered=int(f[7]), reused=int(f[8]))
        if len(f) >= 11:
            self.last.update(hits=int(f[9]), lookups=int(f[10]))
        if len(f) >= 14:
            self.last.update(ram_blobs=int(f[11]), file_blobs=int(f[12]), file_mb=float(f[13]))
        if len(f) >= 15:
            self.last.update(prompt_read=int(f[14]))
        if len(f) >= 16:
            self.last.update(offloaded=int(f[15]))
        for tok in f[16:]:
            key, eq, value = tok.partition("=")
            if eq and key in ("chunk", "experts_streamed"):
                try:
                    self.last[key] = int(value)
                except ValueError:
                    pass


def _sampling_keys(sampling: dict) -> str:
    """The engine's per-request sampling keys, exactly as the Strata server sends them."""
    keys = ""
    t = sampling.get("temperature")
    if isinstance(t, (int, float)) and float(t) > 0.0:
        keys += f" temperature={float(t)!r}"
    tp = sampling.get("top_p")
    if isinstance(tp, (int, float)) and float(tp) < 1.0:
        keys += f" top_p={float(tp)!r}"
    tk = sampling.get("top_k")
    if isinstance(tk, int) and not isinstance(tk, bool) and tk >= 0:
        keys += f" top_k={tk if 1 <= tk <= 64 else 64}"
    mp = sampling.get("min_p")
    if isinstance(mp, (int, float)) and 0.0 < float(mp) <= 1.0:
        keys += f" min_p={float(mp)!r}"
    rp = sampling.get("repetition_penalty")
    rp_on = isinstance(rp, (int, float)) and float(rp) != 1.0
    pf = sampling.get("frequency_penalty")
    pf_on = isinstance(pf, (int, float)) and float(pf) != 0.0
    pp = sampling.get("presence_penalty")
    pp_on = isinstance(pp, (int, float)) and float(pp) != 0.0
    if rp_on:
        keys += f" penalty_repeat={float(rp)!r}"
    if pf_on:
        keys += f" penalty_freq={float(pf)!r}"
    if pp_on:
        keys += f" penalty_present={float(pp)!r}"
    if rp_on or pf_on or pp_on:
        pln = sampling.get("penalty_last_n")
        keys += f" penalty_last_n={pln}" if isinstance(pln, int) and not isinstance(pln, bool) and pln > 0 \
            else " penalty_last_n=64"
    seed = sampling.get("seed")
    if isinstance(seed, int) and seed > 0:
        keys += f" seed={seed}"
    tune = sampling.get("strata_tune")
    if isinstance(tune, dict):
        for k in ("pcie_frac", "spec_min_p"):
            v = tune.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0:
                keys += f" {k}={float(v)!r}"
    return keys


def _field(line: str, key: str) -> str | None:
    for token in line.split():
        if token.startswith(key + "="):
            return token.split("=", 1)[1]
    return None
