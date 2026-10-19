"""serve/server.py - plan v0.3 P8: OpenAI and Anthropic endpoints over any engine that maps token ids to tokens.

    python -m serve.server --engine mock --port 8095            (a scripted engine, for clients and tests)
    python -m serve.server --engine hypercore --config hypercore.json --port 8081   (the real engine, resident)

Endpoints: POST /v1/chat/completions (OpenAI, stream and non-stream), POST /v1/messages (Anthropic, stream and
non-stream), GET /v1/models, GET /models, GET /props, GET /slots, GET /health, GET /mcp. One sequence at a time behind a FIFO (plan: one resident sequence).
Tools from MCP servers (serve/mcp/, `mcp.yaml` beside the server or `config/mcp.yaml`,
`"mcp_servers"` in the config, or --mcp-config) are
offered only to requests that ask for them with `"hypercore_mcp": true` - the web app does; other clients see exactly
the API they always saw.
Images (optional, when the config has a "vision" entry): OpenAI image_url parts and Anthropic image blocks (base64
data, http(s) URLs or local file paths) go through `hypercore-vision` (the model's mmproj file) and reach the engine as
embeddings (`GENI`).  JPEG/PNG/BMP/GIF go straight in; WebP, TIFF, AVIF, ... (agents like omp send WebP) are
converted to PNG first with Pillow.
Requests whose prompt plus max tokens exceed the engine's context are REJECTED with 400, never truncated.
An unset (or 0, or -1) max tokens means "unlimited": whatever the prompt leaves of the context.

The engine boundary is `Engine.generate(prompt_ids, max_new, sampling, cancel) -> iterator of token ids`.
`HyperCoreEngine` keeps one `hypercore --serve` process resident (weights, expert arena and VRAM tier load once) and
talks to it over stdin/stdout; `MockEngine` is a scripted stand-in that makes every API path testable without a GPU.
"""
from __future__ import annotations

import argparse
import base64
import codecs
import collections
import contextlib
import ctypes
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterator
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))   # run as a script (run-<model>.bat) as well as a module
import logging

from serve import (
    assist,
    runconfig,
)
from serve import prompts as agent_prompts
from serve import responses as responses_api
from serve import build_api, chat_archive, quant_store, strata_hook, tune_api
from serve.offline_engine import (DEFAULT_RETRY_S, DeferredEngine, EngineRetry, EngineUnavailable,
                                 OfflineEngine, UNLOADED_CODE)
from serve.frontend import (
    ChatTemplate,
    Event,
    OutputParser,
    anthropic_to_messages,
    forced_call,
    images_of,
    literal_tags,
    mark_think_literals,
    openai_to_messages,
    tool_choice_of,
    unmark_think_literals,
)
from serve.http_auth import auth_error, key_matches
from serve.http_origin import (  # the Host/Origin policy: one definition, shared with serve/flood_stub.py
    LOOPBACK_NAMES,
    host_allowed,
    host_name,
    host_names_for,
    lan_addresses,
    origin_allowed,
)
from serve.engine_supervisor import (
    HyperCoreProcessSupervisor,
    StreamStalled,
    WorkerBusy,
    WorkerRejected,
    WorkerUnreachable,
    model_startup_s,
    unique_socket_path,
)
from serve.ipc_parser import (
    STREAM_TERMINAL,
    STREAM_TOKEN,
    derive_monitor_field,
)

logger = logging.getLogger("hypercore.server")
if not logger.handlers:
    _stream_handler = logging.StreamHandler(sys.stderr)
    _stream_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_stream_handler)
    logger.setLevel(logging.INFO)

from serve.mcp import (
    COMPACT_PROMPT,
    McpCancelled,
    deterministic_summary,
    hub_from_config,
    plan_compaction,
    shrink_messages,
)
from serve.responses import ResponsesError
from serve.responses import error_body as responses_error_body
from serve.structured import (
    StructuredOutputError,
    prepare_format,
    validated_json,
)
from serve.winjob import contain

IM_END = "<|im_end|>"
IMAGE_PAD = "<|image_pad|>"
IMAGE_URL_MAX = 32 << 20  # an image URL is read up to this (the web app attaches pictures of up to 20 MB)


def popen(what: str, args: list, **kw):
    """#735: subprocess.Popen, but Windows' refusal to start a program (WinError 4551, or 1260 when a policy blocks it:
    Smart App Control on a clean Windows 11 blocks an exe that is not signed) is said in words instead of a bare
    OSError."""
    try:
        return subprocess.Popen(args, **kw)
    except OSError as e:
        if getattr(e, "winerror", None) in (4551, 1260):
            raise OSError(f"Windows blocked {what} ({args[0]}): Smart App Control, or an application-control policy, does "
                          "not let a program that is not signed run. Turn Smart App Control off (Windows Security > App "
                          "& browser control > Smart App Control settings), or - for the image encoder only - run setup "
                          "with --vision no (see docs/TROUBLESHOOTING.md)") from e
        raise


def listen_problem(host: str, port: int, e: OSError) -> str:
    """#769: why the server cannot listen.  Only an address that is taken says "already in use"; anything else (a port
    Windows keeps reserved, an address this PC does not have, no permission) says what the OS said."""
    import errno
    code = getattr(e, "winerror", None) or e.errno
    if code in (errno.EADDRINUSE, 10048):
        return (f"port {port} is already in use - is HyperCore (or another server) already running? "
                f"Close it, or start this one with a different --port")
    what = e.strerror or str(e)
    if code in (10013, errno.EACCES):
        hint = ("Windows may keep this port for itself (see `netsh int ipv4 show excludedportrange protocol=tcp`) "
                "or a firewall rule forbids it: start this one with a different --port")
    elif code in (10049, errno.EADDRNOTAVAIL):
        hint = f"{host} is not an address of this PC: check \"host\" in the model's hypercore-<model>.json or --host"
    else:
        hint = "check --host and --port"
    return f"cannot listen on {host}:{port}: {what} [{code}]. {hint}"


def combined_embeddings_path(vision_dir: Path, nbytes: int) -> Path:
    """#874: where one request's combined image-embeddings file goes.  Every request with images rewrites it (it is
    deleted after the request), which on a disk is steady heavy writing: on Linux it goes to /dev/shm when that has
    room (the engine reads it back at once), on Windows the file is opened short-lived (the cache is not forced out
    to the drive).  Anywhere else, or when /dev/shm is too small (Docker's default is 64 MB), it stays in the encoder's
    own directory, as before."""
    name = f"req-{uuid.uuid4().hex[:12]}.sve"
    shm = Path("/dev/shm")
    if os.name != "nt" and shm.is_dir() and os.access(shm, os.W_OK):
        try:
            if shutil.disk_usage(shm).free > 2 * nbytes + (16 << 20):
                return shm / f"hypercore-vision-{os.getpid()}-{name}"
        except OSError:
            pass
    return Path(vision_dir) / name


def write_temporary(path: Path, parts: list) -> None:
    """#874: write `parts` (files) one after the other into `path`; on Windows with FILE_ATTRIBUTE_TEMPORARY
    (os.O_SHORT_LIVED), so the file normally never reaches the drive before it is deleted."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0) | getattr(os, "O_SHORT_LIVED", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as f:
        for part in parts:
            f.write(Path(part).read_bytes())
# #458: the trailing effort turn ("effort_position": "end"); low is the template's own sentence, medium (which the
# template says nothing for: no xhigh sentence is medium there) says it, since the xhigh one stays at the top
EFFORT_TURN = "<|im_start|>system\n{}<|im_end|>\n"
EFFORT_TEXT = {"low": "Reasoning effort is set to low. Keep your thinking brief and focused, moving directly to the "
                      "conclusion without unnecessary elaboration.",
               "medium": "Reasoning effort is set to medium. Think as much as the task needs, without unnecessary "
                         "elaboration.",
               # the template's own xhigh sentence (chat_template.jinja), which #728's recovery swaps for the low one
               "xhigh": "Reasoning effort is set to xhigh. Please think carefully through the task, validate key "
                        "assumptions, consider plausible alternatives, and prioritize correctness, consistency, and "
                        "clarity in the final answer."}
VISION_START = "<|vision_start|>"
# #606: a reply that repeats one token this many times in a row is ended there ("length"): a model in a loop, or a
# broken state that answers one token forever (an issue saw 36,689 tokens of "!"). The config's "repeat_stop_tokens"
# sets it; 0 turns it off.
REPEAT_STOP_TOKENS = 256
# #123: what closes the thinking when it reaches reasoning_budget_tokens (the model's own end-of-thinking tag after it)
REASONING_WRAP_UP = "\n\nI have thought about this long enough; time to give my answer.\n</think>\n\n"
# #728: opt-in handling of reasoning that repeats whole passages (which the single-token guard above cannot see).
# "reasoning_loop_recovery": "stop" ends the reply there; "recover" (or true) goes on from the same output with the
# low-effort instruction in place of the xhigh one.  Both off by default.
HIGH_EFFORT = EFFORT_TEXT["xhigh"]
LOW_EFFORT = EFFORT_TEXT["low"]
LOOP_CHECK_EVERY = 512           # output tokens between two looks at the reasoning (at a clean parser boundary)
LOOP_COVERAGE = 0.25             # the share of the last 2,000 words inside 12-word passages seen three times
LOOP_HISTORY_WORDS = 30000       # how far back the passages are counted (bounds the cost of a look)


def reasoning_repeat_coverage(text):
    """Coverage of recent words by 12-word passages seen at least three times.

    Only reasoning is supplied. The history (the last LOOP_HISTORY_WORDS words) detects repeated verification passes
    separated by long code drafts; the recent window excludes old repetitions.
    """
    words = re.findall(r"\w+|[^\w\s]", text.lower())[-LOOP_HISTORY_WORDS:]
    width, window = 12, 2000
    if len(words) < window:
        return 0.0
    grams = [tuple(words[i:i + width]) for i in range(len(words) - width + 1)]
    counts = collections.Counter(grams)
    start = max(0, len(words) - window)
    covered = set()
    for i in range(start, len(grams)):
        if counts[grams[i]] >= 3:
            covered.update(range(i, i + width))
    return len(covered) / (len(words) - start)


def focused_recovery_prompt(tok, ids, generated):
    """The prompt's ids with the xhigh effort sentence of the first system message replaced by the low one, then
    every token generated so far.  A splice of token ids: the prompt is never decoded and encoded again, so a literal
    `</think>` or vision marker in it (#537, #554) stays what it was.  None when the sentence is not there as whole
    tokens in the first system message (a user's text never counts).  No answer or end-of-thinking is inserted."""
    end = tok.encode(IM_END, parse_special=True)
    if len(end) != 1 or end[0] not in ids:
        return None
    head = ids[:ids.index(end[0])]
    if not tok.decode(head).startswith("<|im_start|>system\n"):
        return None
    old, new = tok.encode(HIGH_EFFORT, parse_special=False), tok.encode(LOW_EFFORT, parse_special=False)
    for i in range(len(head) - len(old) + 1):
        if head[i:i + len(old)] == old:
            return ids[:i] + new + ids[i + len(old):] + list(generated)
    return None


CTX_SLACK = 8               # `hypercore --serve` rejects prompt + max_new + 8 > context: keep the same margin here
# The live tok/s is a rate over a window, not a mean since the first token: a mean reads ~1/elapsed at the first
# token (the Monitor showed five-digit numbers) and then undershoots for the first second of every answer.
RATE_WINDOW_S = 2.0
RATE_MIN_SPAN_S = 0.25      # younger than this there is no rate yet: the mean so far, with the span floored here
# #481: a running request whose engine prints nothing (no T, PP or any other line) for this long has lost step with the
# server (the engine's main thread waits, untimed, for its next command): the engine is ended and the request fails;
# the next request starts it again.  The config's "engine_silence_s" sets it (0: wait forever, as before).
ENGINE_SILENCE_S = 300.0
# Disk sessions: the longest allowance an engine may announce (`SWAIT <phase> <s>`) for one step that blocks in a
# single call (a file flush, the host->device transfer); the engine's own bound is the same (session_phase_limit_s).
SESSION_WAIT_MAX_S = 3600
# ... except while a prompt is read: a PP line comes once per chunk (up to 32768 tokens with --prefill auto, issue
# #282), and the slowest PCs read ~100 tok/s, so a first chunk can take minutes before the first line.  Until the first
# PP the wait adds the chunk's tokens at PP_FLOOR_TOK_S; after one, a chunk may take PP_SLACK x the last one's time.
PP_CHUNK_MAX = 32768
PP_FLOOR_TOK_S = 50.0
PP_SLACK = 3.0


# ------------------------------------------------------------------------------------------------ engines
class Engine(Protocol):
    max_context: int
    def generate(self, ids: list[int], max_new: int, sampling: dict, cancel: threading.Event) -> Iterator[int]: ...


class MockEngine:
    """Replays a scripted completion (text) as token ids, one per step, then the end-of-turn token.  Given a list of
    scripts, each request gets the next one and the last one repeats (a tool call, then the answer after it)."""

    def __init__(self, tokenizer, script: str | list[str], max_context: int = 32768, delay_s: float = 0.0):
        self.tok, self.max_context, self.delay = tokenizer, max_context, delay_s
        end = tokenizer.encode(IM_END, parse_special=True)
        self.scripts = [tokenizer.encode(x, parse_special=True) + end for x in ([script] if isinstance(script, str)
                                                                                 else script)]
        self.script, self.turns = self.scripts[0], 0
        self.last_prompt: list[int] = []

    def generate(self, ids, max_new, sampling, cancel, embeddings=None):
        self.last_prompt = list(ids)
        self.last_embeddings = embeddings
        if len(self.scripts) > 1:
            self.script = self.scripts[min(self.turns, len(self.scripts) - 1)]
            self.turns += 1
        for t in self.script[:max_new]:
            if cancel.is_set():
                return
            if self.delay:
                time.sleep(self.delay)
            yield t


class EngineDied(RuntimeError):
    """The engine process ended in the middle of a request (issue #27: on Linux, the out-of-memory killer)."""


class EngineStarting(RuntimeError):
    """The engine is (re)starting and has not said READY yet (#344): no context size to plan a request with - a 503,
    not a 400 about the prompt."""


class ModelBusy(RuntimeError):
    """Explicit model controls must not interrupt active or queued requests."""


class EngineSilent(EngineDied):
    """#481: the engine said nothing for too long during a request (or never acknowledged a STOP): the two sides lost
    step - the engine waiting for its next command, the server for this request's end - and the server ended it.  An
    EngineDied, so the request ends with an error and the next one starts the engine again."""


class SessionRefused(ValueError):
    """Disk sessions: the engine refused or failed a SAVE / RESTORE and is still in step (`SERR <kind> <published>
    <reason>`).  `kind` is the engine's category - invalid (400), storage (507), memory (503), io (500); `published`:
    a save whose new file already replaced the old one before a later step (the folder flush) failed."""

    STATUS = {"invalid": 400, "storage": 507, "memory": 503, "io": 500}

    def __init__(self, kind: str, message: str, published: bool = False):
        super().__init__(message)
        self.kind = kind if kind in self.STATUS else "io"
        self.published = published

    @property
    def status(self) -> int:
        return self.STATUS[self.kind]


class EngineStuck(RuntimeError):
    """The engine process did not end after QUIT, terminate and kill: the server keeps it (and says so) rather than
    reporting its GPU and RAM as given back."""


class EngineStopped(RuntimeError):
    """The engine was stopped with POST /v1/stop and has not been started again. A request that arrives while it is
    stopped is refused (409) instead of quietly loading the model behind the button and answering with nothing: an
    empty reply reaches the Monitor as a turn that ran out of tokens."""


class GpuBusy(RuntimeError):
    """The model is unloaded and the GPU has less free VRAM than min_free_vram_mib: something else (a game, another
    model server) is using it, so the engine is not started into the little that is left.  `retry_after`: seconds for
    the Retry-After header of the 503."""

    def __init__(self, message="", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class SlotRefused(RuntimeError):
    """#1994: the engine would not take a batch slot after several tries (its view of the slots and the server's
    differ, or it is full).  Not the request's fault: a retryable 503, never a 400 about the conversation."""

    def __init__(self, message="", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def unload_wait(value, default: float) -> float | None:
    """#1997: `?wait=` on POST /unload: no value means do not wait, a bare `wait` waits the default, `wait=60` that
    many seconds.  A client that wants the GPU back now (another model server unloading) gets a real wait instead of a
    409 busy on the first try."""
    if value is None:
        return None
    try:
        return float(value) if value.strip() else default
    except ValueError:
        return default


ENGINE_REQUEST = re.compile(
    # "+ 12288 of 98179 read": a request cancelled while its prompt was read (#471)
    r"prompt (?P<prompt>\d+) tokens = (?P<reused>\d+) reused \+ \d+(?: of \d+)? read in (?P<read>[\d.]+) ms "
    r"\((?P<pp>[\d.]+) tok/s\), "
    r"(?P<gen>\d+) generated in (?P<gen_ms>[\d.]+) ms \((?P<tg>[\d.]+) tok/s\)")


_echoing: set[str] = set()      # the logs echo_requests already follows (restart() runs HyperCoreEngine.__init__ again)

DRAFT_HEAD_FAIL = "the draft head does not fit"
DRAFT_HEAD_HINT = ("a smaller draft vocabulary needs less VRAM: --draft-vocab cyrillic (English, code and the Cyrillic "
                   "script) or --draft-vocab en (English and code, ~215 MiB less than the default). Start once with "
                   "it - START-HERE.bat --draft-vocab en (Windows) or ./setup.sh --draft-vocab en - and the model "
                   "keeps it; or a smaller --context in setup.")


def start_failure_hint(log: str | None, offset: int) -> str:
    """#474: what to change when the engine stopped at the start because the MTP draft head did not fit the VRAM
    left: the engine's own `hypercore mtp:` lines after that failure (0.1.36+: what it needs, what is free, the smaller
    subsets), else the same advice in words for an older engine.  "" for any other failure: the log says why."""
    if not log:
        return ""
    try:
        with open(log, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    if DRAFT_HEAD_FAIL not in text:
        return ""
    said = [x.strip()[len("hypercore mtp: "):] for x in text.splitlines()
            if x.strip().startswith("hypercore mtp: ") and ("draft head over" in x or "hint:" in x)]
    return ". mtp: " + DRAFT_HEAD_FAIL + ". " + (" ".join(said) if said else "Hint: " + DRAFT_HEAD_HINT)


def start_log_tail(log: str | None, offset: int, n: int = 20) -> str:
    """#496: the last n lines this start wrote to the engine log, for the error when the engine ended before READY -
    whatever the failure, the engine's own reason is in them (people posted the traceback without the log).  "" when
    there is no log or nothing in it from this start."""
    if not log:
        return ""
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            start = max(offset, f.tell() - 64 * 1024)   # enough for 20 lines, never an earlier start's
            f.seek(start)
            text = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    lines = text.splitlines()[1 if start > offset else 0:]   # not a line cut in half
    lines = [x.rstrip() for x in lines if x.strip()][-n:]
    if not lines:
        return ""
    return "\nthe engine log's last lines:\n" + "\n".join("  " + x for x in lines)


def echo_requests(log_path: str, offset: int) -> None:
    """HYPERCORE_REQUEST_LINES=1: one stdout line per finished request, from the engine's own summary in its log.

    The engine's stderr goes to the log file (the start narrator reads it), so a supervisor that only sees this
    process's output - a tray, llama-swap - has no per-request numbers. This re-states the engine's line with the
    total the two times make: `request prompt P cached C output O prompt_read R ms total S ms prefill X tok/s decode Y
    tok/s` (prompt_read: the time the engine spent reading the prompt's new tokens, not a time to first token).
    """
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            m = ENGINE_REQUEST.search(line)
            if m:
                read_ms, gen_ms = float(m["read"]), float(m["gen_ms"])
                print("[hypercore] request prompt %s cached %s output %s prompt_read %.0f ms total %.0f ms prefill %s "
                      "tok/s decode %s tok/s" % (m["prompt"], m["reused"], m["gen"], read_ms, read_ms + gen_ms, m["pp"],
                                                 m["tg"]), flush=True)


def experts_loading_words(args: list, size: str) -> str:
    """#505: what the start does with the experts, by the engine's flags (generate.cpp's option parsing): a RAM budget
    copies the hottest N GiB into RAM (--resident-budget-gib), the resident low-RAM mode the ones the GPU does not hold
    (--resident-experts), plain --mmap-experts reads them from the model files through the OS file cache (nothing is
    loaded into RAM up front); otherwise all of them go into RAM."""
    if "--resident-budget-gib" in args:
        try:
            n = f"up to {float(args[args.index('--resident-budget-gib') + 1]):g} GiB"
        except (IndexError, ValueError):
            n = "a RAM budget"
        return (f"loading the most-used experts into RAM ({n}; the rest are read from the model files as needed) "
                "and locking part of them for the GPU.")
    if "--resident-experts" in args:
        return (f"loading the experts the GPU does not hold into RAM (of {size}) and locking part of them for the "
                "GPU.")
    if "--mmap-experts" in args:
        return (f"mapping the experts from the model files ({size}, --mmap-experts): they are not loaded into RAM - "
                "the OS file cache reads them as the GPU's expert cache fills and as requests need them.")
    return f"loading the experts into RAM ({size}) and locking part of them for the GPU."


def narrate_start(log_path: str, offset: int, args: list, done: threading.Event, heartbeat=20.0) -> None:
    """While the engine starts, say in the server window what it is doing, from its log: the start reads tens of GB
    into RAM and locks part of it for the GPU, and on many PCs everything is slow or frozen for a minute or more -
    people closed the window thinking it had hung.  The warning comes at that step, not after it."""
    gb = 0.0
    if "--native" in args:                              # about the size of the experts it will read
        try:
            gb = os.path.getsize(args[args.index("--native") + 1]) / 1e9
        except (OSError, IndexError):
            pass
    size = f"about {gb:.0f} GB" if gb >= 1 else "tens of GB"
    loading = experts_loading_words(args, size)
    t0 = last = time.time()
    said = set()

    def say(key, text):
        nonlocal last
        if key not in said:
            said.add(key)
            last = time.time()
            print(text, flush=True)

    say("weights", "[hypercore] starting the engine: reading the model's weights ...")
    pos = offset
    while not done.wait(0.5):
        try:
            with open(log_path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            chunk = b""
        if chunk.count(b"\n"):
            cut = chunk.rfind(b"\n") + 1
            pos += cut
            for line in chunk[:cut].decode("utf-8", "replace").splitlines():
                if "PLE on" in line or "expert arena:" in line or "experts via mmap" in line:   # #505: mapped
                    say("arena", f"[hypercore] {loading}\n"
                                 "         YOUR PC CAN BE SLOW OR STOP RESPONDING FOR 1-3 MINUTES NOW - this is normal.\n"
                                 "         Please wait and don't close this window; the browser opens when it is ready.")
                elif " loaded " in line and "GiB at" in line:
                    say("loaded", "[hypercore] experts loaded: " + line.split(" loaded ", 1)[1].strip() +
                        f" ({time.time() - t0:.0f} s so far)")
                elif "expert cache " in line and " slots, " in line and "auto" not in line:
                    n = line.split("expert cache ", 1)[1].split(";")[0].replace(" slots,", " experts,").strip()
                    say("cache", f"[hypercore] filling the GPU's expert cache ({n}) ...")
                elif "session is up" in line:
                    say("up", "[hypercore] almost ready ...")
        if time.time() - last > heartbeat:
            last = time.time()
            print(f"[hypercore] still starting ({time.time() - t0:.0f} s) - please wait ...", flush=True)


def worker_start_words(supervisor) -> tuple[str, list[str]] | None:
    """What the narrator needs from a supervisor: the file its worker's stderr goes to, and the
    engine flags that worker was started with (its own config).  None without a log: there is
    nothing to read, and the window then says nothing about the minutes of loading (#505)."""
    log = getattr(supervisor, "stderr_log", None)
    if not log:
        return None
    try:
        cfg = json.loads(getattr(supervisor, "config", "") or "{}")
    except (TypeError, ValueError):
        cfg = {}
    return str(log), list(cfg.get("engine_args") or [])


def narrate_worker_start(supervisor, ready: threading.Event) -> None:
    """Say in the server window what the worker is doing while it starts, from its log (#505).
    Daemon thread, stopped by `ready`: the start reads tens of GB into RAM and locks part of it
    for the GPU, and on many PCs everything freezes for a minute or more - people closed the
    window thinking it had hung."""
    words = worker_start_words(supervisor)
    if not words:
        return
    log, args = words
    threading.Thread(target=narrate_start, args=(log, 0, args, ready), daemon=True).start()


EXPERT_SOURCE_NAMES = {
    0: "no expert tier",
    1: "mapped read-only from the pack (--mmap-experts)",
    2: "the arena copy (--expert-cache)",
    3: "resident in page-locked RAM (--resident-experts)",
}


def expert_config_line(info: dict) -> str:
    """The expert configuration the worker resolved, in one line, when setup finishes.

    The engine prints all of this at startup, but to its own log file. In the split
    architecture the worker's stderr goes to that log and not to the terminal, so the person
    starting the server sees a model name and a port and nothing about the tier that decides
    whether the run is fast. This is that line, from the numbers the worker already reports.
    """
    slots = int(info.get("expert_slots") or 0)
    full = int(info.get("expert_slots_full") or 0)
    mib = int(info.get("expert_cache_mib") or 0)
    full_mib = int(info.get("expert_cache_full_mib") or 0)
    source = info.get("expert_source")
    free = info.get("vram_free_mib")
    if source is None:
        return ("[hypercore] expert configuration: the worker did not report one. "
               "Its log holds the detail: " + str(info.get("log") or "(no log configured)"))
    parts = [f"{slots} of {full} VRAM expert slots" if full else f"{slots} VRAM expert slots",
            f"{mib / 1024:.2f} GiB" + (f" of {full_mib / 1024:.2f} GiB" if full_mib > mib else "")]
    line = "[hypercore] expert configuration: " + ", ".join(parts) + " held by " + EXPERT_SOURCE_NAMES.get(int(source), f"source {source}")
    if free:
        line += f" ({free} MiB of VRAM free when it was sized)"
    if info.get("prompt_chunk"):
        line += f", prompt chunk {info['prompt_chunk']} tokens"
    if info.get("pcie_frac") is not None and info.get("pcie_frac") >= 0:
        line += f", pcie_frac {info['pcie_frac']:.2f}"
    return line


class ConvCacheLog:
    """#596: the engine's conversation cache as its log tells it (the engine writes "hypercore serve: conversation
    cache: parked N tokens ...; parked=P bytes=B evictions=E" and "restored N tokens ...; parked=P bytes=B" to stderr,
    which is the log): read on from where it was last read, from the start of the engine's current run."""
    EVENT = re.compile(r"conversation cache: (parked|skipped|restored) (\d+) tokens.*?parked=(\d+) bytes=(\d+)"
                       r"(?: evictions=(\d+))?")
    DROPPED = re.compile(r"conversation cache: dropped \d+ superseded .*?parked=(\d+)")
    READ_MAX = 1 << 20                                  # at most the last MiB of new lines per read

    def __init__(self):
        self.key, self.pos = None, 0
        self.reset()

    def reset(self):
        self.state = {"parked": 0, "bytes": 0, "evictions": 0, "parks": 0, "restores": 0, "last_event": None,
                      "last_tokens": None, "last_at": None}

    def poll(self, path, start) -> dict:
        """The state after the log's new lines; `start` is where the engine's current run began in it."""
        if not path or start is None:
            return dict(self.state)
        if self.key != (path, start):                   # another start of the engine: its cache starts empty
            self.key, self.pos = (path, start), start
            self.reset()
        try:
            size = os.path.getsize(path)
            if size < self.pos:                         # the log was emptied or replaced
                self.pos = 0
            if size - self.pos > self.READ_MAX:
                self.pos = size - self.READ_MAX
            with open(path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return dict(self.state)
        end = data.rfind(b"\n") + 1                     # whole lines only: the rest is read next time
        self.pos += end
        now = time.time()
        for line in data[:end].decode("utf-8", "replace").splitlines():
            if "conversation cache:" not in line:
                continue
            m = self.EVENT.search(line)
            if m:
                st = self.state
                st["parked"], st["bytes"] = int(m.group(3)), int(m.group(4))
                if m.group(5) is not None:
                    st["evictions"] = int(m.group(5))
                if m.group(1) != "skipped":
                    st["parks" if m.group(1) == "parked" else "restores"] += 1
                    st["last_event"], st["last_tokens"], st["last_at"] = m.group(1), int(m.group(2)), now
                continue
            m = self.DROPPED.search(line)
            if m:
                self.state["parked"] = int(m.group(1))
        return dict(self.state)


def conversation_cache_view(info: dict, hist: list, totals: dict, parked: dict) -> dict:
    """#596: the Monitor's Conversation cache card: the parked conversations (the engine's opt-in
    --conversation-cache-mib: budget, slots, what its log says) and how much of the prompts the cache gave back."""
    mib = info.get("conversation_cache_mib")
    last = hist[-1] if hist else None
    return {"enabled": isinstance(mib, int) and mib > 0, "budget_mib": mib if isinstance(mib, int) else None,
            "slots": info.get("conversation_cache_slots"), **parked,
            "requests": len(hist), "requests_reused": sum(1 for r in hist if (r.get("reused") or 0) > 0),
            "reused_tokens": totals.get("reused", 0), "prompt_tokens": totals.get("prompt_tokens", 0),
            "last_reused": last.get("reused") if last else None,
            "last_prompt": last.get("prompt_tokens") if last else None}


_BTRACE = bool(os.environ.get("HYPERCORE_BATCH_TRACE"))


def btrace(*a):
    if _BTRACE:
        print("[batch-trace]", threading.get_ident() % 10000, *a, file=sys.stderr, flush=True)


EOS_IDS = {248044, 248046}   # <|endoftext|>, <|im_end|>: the engine's default --eos-ids


# The engine's own words for a verifier that cannot go on (its GPU waits were released, or an earlier window never
# finished, #267): the process may stay alive and even print DONE, but it must not take another request.
FATAL_PREFIXES = ("ERR verify: timed out at layer ", "ERR verify batch: timed out at layer ",
                  "ERR verify: an earlier window never finished", "ERR verify batch: an earlier window never finished")


# HyperCoreEngine, the text-pipe engine, used to live here. It is no longer
# constructed by production code; it is a test fixture and lives in
# serve/fixtures/legacy_engine.py. Import it from there if a suite needs it.

def network_path(path: str) -> bool:
    r"""A path to another computer: \\host\share or //host/share (either slash), \\?\UNC\host\..., \\.\UNC\host\...
    or the NT form \??\UNC\host\...  Windows connects to that host at the first look at the path (os.path.isfile is
    enough) and signs in with the user's NTLM credentials.  \\?\ and \??\ name local files too; no client needs them."""
    p = path.replace("/", "\\")
    return p.startswith("\\\\") or p.startswith("\\??\\")


VISION_ALLOC_MARKS = ("cudaMalloc", "alloc_tensor_range", "GGML_ASSERT", "out of memory")
ENGINE_LINES = ("hypercore serve:", "hypercore generate:", "hypercore_worker:", "hypercore mtp:",
                "hypercore verify:", "hypercore hc:", "FileExpertSource:")
VISION_VRAM_MIB = 1400          # the GPU encoder's share: 865 MiB of BF16 mmproj tensors + its CUDA context + work buffers
VISION_VRAM_HINT = (" - the image encoder needs its own share of VRAM (about 1.4 GB with --vision gpu): run setup "
                    "with --vision cpu, give it a card of its own (vision.cuda_device in the config), or free the "
                    "one it is on (docs/TROUBLESHOOTING.md)")


def vision_start_reason(log: str | None, offset: int) -> str:
    """What the image encoder said before it died: after `offset` in the log its stderr went to - the model's log is
    shared with the engine, so the engine's lines are skipped - the line naming an allocation (the most actionable
    mark first), else its last line.  "" with no log or nothing new in it."""
    if not log:
        return ""
    try:
        with open(log, "rb") as f:
            f.seek(offset)
            lines = [x.strip() for x in f.read().decode("utf-8", "replace").splitlines()
                     if x.strip() and not x.strip().startswith(ENGINE_LINES)]
    except OSError:
        return ""
    for mark in VISION_ALLOC_MARKS:
        for line in lines:
            if mark in line:
                return line
    return lines[-1] if lines else ""


class Vision:
    """The resident image encoder: `hypercore-vision` (llama.cpp mtmd + the mmproj file) reads `ENC <image> <out>`
    lines and writes each image's embeddings; results are cached by the image's hash, so a conversation that
    sends the same picture again (every turn, with most clients) encodes it once."""

    @staticmethod
    def work_dir() -> Path:
        """#480: the directory for the encoder's files.  The engine reads the embeddings file named in a `GENI` line
        and the encoder the output named in an `ENC <image> <out>` line, both split at spaces, so a space in the path
        (a %TEMP% under a user name with a space) broke every image.  The system temp directory is used while its path
        has no space, then the working directory, then the system drive's root; the plain temp directory is the last
        resort (the encoder's own files are then named relative to it, see `_start`)."""
        for parent in (None, Path.cwd(), Path(os.environ.get("SystemDrive", "C:") + os.sep)):
            try:
                d = Path(tempfile.mkdtemp(prefix="hypercore-vision-", dir=parent))
            except OSError:
                continue                                       # not writable, or no such drive
            if " " not in str(d):
                return d
            try:
                d.rmdir()                                      # just created, so still empty
            except OSError:
                pass
        return Path(tempfile.mkdtemp(prefix="hypercore-vision-"))

    def __init__(self, cfg: dict, log=None, env: dict | None = None):
        absolute = lambda p: os.path.abspath(p) if os.path.dirname(str(p)) else p   # the encoder runs in its own dir (#480)
        args = [absolute(cfg["exe"]), "--mmproj", absolute(cfg["mmproj"]), "--model", absolute(cfg["model"])]
        if cfg.get("gpu"):
            args.append("--gpu")
        if cfg.get("threads"):
            args += ["--threads", str(cfg["threads"])]
        if cfg.get("max_tokens"):
            args += ["--max-tokens", str(cfg["max_tokens"])]
        if cfg.get("min_tokens"):                       # #767: mtmd's image_min_tokens (a hand-edited key)
            args += ["--min-tokens", str(cfg["min_tokens"])]
        self.dir = self.work_dir()
        self.spawn = (args, log, env)                   # to start it again after an unload
        self.cpu_only = False                           # set when the card had no room for the encoder
        self.stopped = False
        self._start()
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[Path, int]] = {}

    def _start(self, args: list | None = None):
        args = list(self.spawn[0]) if args is None else list(args)
        log, env = self.spawn[1], self.spawn[2]
        # where this start's lines begin in the log: an append-mode handle reports 0 until it is written to, and the
        # lines of an earlier start would then be read as the encoder's own
        log_path = getattr(log, "name", None)
        log_start = os.path.getsize(log_path) if log_path else 0
        # cwd is the encoder's directory: the ENC lines name the files relative to it (#480: no space in the line)
        self.proc = popen("the image encoder", args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=log or subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=env,
                          cwd=self.dir)
        contain(self.proc)
        for line in iter(self.proc.stdout.readline, ""):
            if line.startswith("READY"):
                self.stopped = False
                return
            if line.startswith("ERR"):                       # the encoder's own "cannot do this"
                raise RuntimeError("the vision encoder did not start: " + line.strip())
            # anything else on stdout before READY is noise (a crash reporter's stack trace, a loader warning):
            # the reason is on stderr, which went to the log
        code = self.proc.wait()
        why = vision_start_reason(log_path, log_start)
        if "--gpu" in args and not self.cpu_only and any(m in why for m in VISION_ALLOC_MARKS):
            # the card has no room for the encoder: a chat that answers is better than one that will not start
            print(f"[hypercore] the image encoder does not fit the VRAM left on the card ({why}). It runs on the "
                  "CPU instead - pictures are slow (about 3-13 s) but they work. To keep it on the GPU, run setup "
                  f"again with --vram-reserve-mib {VISION_VRAM_MIB} or --vision cpu (docs/TROUBLESHOOTING.md).",
                  flush=True)
            self.cpu_only = True
            self.spawn = ([a for a in args if a != "--gpu"], log, env)   # a restart stays on the CPU
            return self._start([a for a in args if a != "--gpu"])
        hint = VISION_VRAM_HINT if any(m in why for m in VISION_ALLOC_MARKS) else ""
        raise RuntimeError(f"the vision encoder did not start (it exited with {code})" +
                          (": " + why if why else "") + hint)

    def alive(self) -> bool:
        return not self.stopped and self.proc.poll() is None

    def unload(self):
        """Stop the encoder process (its VRAM or RAM goes back); the encoded images stay cached on disk."""
        self.close()
        self.stopped = True

    def restart(self):
        """Start the encoder again after an unload (or if it died); the cache of encoded images is kept."""
        try:
            self.proc.kill()
        except OSError:
            pass
        self._start()

    @staticmethod
    def load(source: str) -> bytes:
        if source.startswith("data:"):
            try:
                return base64.b64decode(source.split(",", 1)[1])
            except (IndexError, ValueError) as e:                # no comma, or not base64
                raise ValueError(f"the image's data: URL could not be read ({e})") from None
        if source.startswith(("http://", "https://")):
            return Vision.download(source)
        path = source.removeprefix("file://")
        # refused before anything looks at the path: file://host/share/x.png is \\host\share\x.png ("file://C:/x.png",
        # a drive, is a client's spelling of file:///C:/x.png)
        host = re.split(r"[/\\]", path, maxsplit=1)[0] if source.startswith("file://") else ""
        if network_path(path) or host and not re.fullmatch(r"[A-Za-z]:|localhost", host, re.IGNORECASE):
            raise ValueError("an image file must be on this computer: network paths (\\\\host\\share, //host/share, "
                             "file://host/...) are not read")
        if path and os.path.isfile(path):
            try:
                return Path(path).read_bytes()
            except OSError as e:
                raise ValueError(f"the image could not be read from {path} ({e})") from None
        raise ValueError("an image must be a data: URL, an http(s) URL or a local file path")

    @staticmethod
    def download(url: str) -> bytes:
        """An image URL's bytes, at most IMAGE_URL_MAX of them: the whole response was read, so a huge or endless one
        filled the memory.  One that cannot be read is a ValueError (a 400 that says so), not a dropped connection."""
        too_big = f"the image URL's file is over {IMAGE_URL_MAX >> 20} MiB"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hypercore"}), timeout=60) as r:
                size = r.headers.get("Content-Length") or ""
                if size.isdigit() and int(size) > IMAGE_URL_MAX:
                    raise ValueError(too_big)
                data = r.read(IMAGE_URL_MAX + 1)
        except (OSError, HTTPException) as e:
            raise ValueError(f"the image URL could not be read: {e}") from None
        if len(data) > IMAGE_URL_MAX:
            raise ValueError(too_big)
        return data

    @staticmethod
    def normalize(data: bytes) -> bytes:
        """The formats hypercore-vision's decoder (stb_image) reads pass through; anything else is converted to PNG."""
        if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"BM" or \
                data[:6] in (b"GIF87a", b"GIF89a"):
            return data
        try:
            import io

            from PIL import Image
        except ImportError:
            raise ValueError("this image format needs Pillow (python -m pip install pillow); JPEG, PNG, BMP and "
                             "GIF work without it") from None
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as e:
            raise ValueError(f"the image could not be read ({e})") from None
        if im.mode in ("RGBA", "LA", "P") and "transparency" in im.info or im.mode in ("RGBA", "LA"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))   # transparent areas become white, not black
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()

    def encode(self, source: str | bytes) -> tuple[Path, int]:
        """-> (embeddings file, number of image tokens).  `source`: what load() reads, or the image's bytes."""
        data = self.normalize(source if isinstance(source, bytes) else self.load(source))
        key = hashlib.sha256(data).hexdigest()[:32]
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            img, out = self.dir / f"{key}.img", self.dir / f"{key}.sve"
            img.write_bytes(data)
            try:
                self.proc.stdin.write(f"ENC {img.name} {out.name}\n")      # relative to the encoder's cwd (#480)
                self.proc.stdin.flush()
                line = self.proc.stdout.readline().strip()
            finally:                                                   # #352: also when the encoder's pipe is gone
                img.unlink(missing_ok=True)
            if not line.startswith("OK"):
                raise ValueError("the image could not be read: " + (line[4:] if line.startswith("ERR") else
                                                                     "the vision encoder stopped"))
            self.cache[key] = (out, int(line.split()[1]))
            if len(self.cache) > 64:                                   # oldest first
                old = next(iter(self.cache))
                self.cache.pop(old)[0].unlink(missing_ok=True)
            return self.cache[key]

    def close(self):
        try:
            self.proc.stdin.write("QUIT\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def shutdown(self):
        """#914: close() for good: the server ends, so the encoder's directory (one ~10 MB .sve per image, on a tmpfs
        /tmp that is RAM) goes with it instead of piling up with every start.  unload() keeps it for the restart."""
        self.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def gpu_list(cfg: dict) -> list[int]:
    """The config's "gpu": one card (2), or several for a layer split ([0, 2] or "0,2"), numbered as nvidia-smi
    numbers them; [] when it names none."""
    g = cfg.get("gpu")
    if g is None or g == "":
        return []
    items = g if isinstance(g, (list, tuple)) else str(g).split(",")
    return [int(str(x).strip()) for x in items if str(x).strip() != ""]


def effort_end_args(cfg: dict, exe: str, tok) -> list[str] | None:
    """#458 (opt-in): the engine arguments for "effort_position": "end" - the id of "system" as --tail-role-token, so
    the engine checkpoints in front of the trailing effort turn - or None when the config leaves it at the top (the
    default) or the engine is too old for it (said once; the prompt stays the default one).  ValueError for a value
    other than "start" / "end"."""
    pos = cfg.get("effort_position", "start")
    if pos not in ("start", "end"):
        raise ValueError(f'"effort_position" must be "start" (the default) or "end", not {pos!r}')
    if pos == "start":
        return None
    try:
        with open(exe, "rb") as f:
            known = b"--tail-role-token" in f.read()
    except OSError:
        known = False
    role = tok.encode("system", parse_special=True)
    if not known or len(role) != 1:
        print("[hypercore] effort_position \"end\" needs engine 0.1.39 or newer (--tail-role-token): the reasoning "
              "effort stays at the top of the prompt", flush=True)
        return None
    print("[hypercore] effort_position end: a request's non-default reasoning effort goes right before the answer, so "
          "changing it keeps the cached conversation", flush=True)
    return ["--tail-role-token", str(role[0])]


def engine_silence_s(cfg: dict) -> float:
    """#481: the config's "engine_silence_s" - seconds an engine may print nothing during a request before the server
    ends it (default ENGINE_SILENCE_S; 0 = wait forever).  ValueError for anything but a number >= 0."""
    v = cfg.get("engine_silence_s")
    if v is None:
        return ENGINE_SILENCE_S
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError(f'"engine_silence_s" must be a number of seconds >= 0 (0 = no limit), not {v!r}')
    return float(v)


def layer_split_value(cfg: dict) -> str:
    """#644: the config's "layer_split" as the engine's --layer-split value: "auto" (the default), or the FIRST LAYER of
    each later GPU's share - one rising number per GPU after the first, e.g. "24,36,42" for 4 GPUs (layers 0-23 on the
    first card, 24-35, 36-41, 42 to the end) - as a string or a JSON list ([24, 36, 42]).  ValueError with the format
    and an example for anything else, such as per-card layer counts ("24,16,12,12")."""
    v = cfg.get("layer_split")
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "auto")):
        return "auto"
    n = len(gpu_list(cfg))
    items = v if isinstance(v, (list, tuple)) else str(v).split(",") if isinstance(v, (str, int)) else None
    vals = []
    for x in items or []:
        try:
            if isinstance(x, bool) or isinstance(x, float):
                raise ValueError
            vals.append(int(str(x).strip()))
        except ValueError:
            vals = None
            break
    want = max(n - 1, 1)
    example = ",".join(str(round(48 * (i + 1) / (want + 1))) for i in range(want))
    hint = (f'"layer_split" is the first layer of each later GPU, one rising number per GPU after the first '
            f'({want} for {n} GPUs), not a count of layers per card - e.g. "{example}" (or [{example.replace(",", ", ")}])'
            f' for an even share of 48 layers, or "auto" (the default) to place them by each card\'s free VRAM')
    if not vals:
        raise ValueError(f"{hint}; got {v!r}")
    if n > 1 and len(vals) != n - 1:
        raise ValueError(f"{hint}; got {len(vals)} number(s) ({v!r}) for {n} GPUs")
    if vals[0] < 2 or any(b <= a for a, b in zip(vals, vals[1:])):
        raise ValueError(f"{hint}; got {v!r}, which does not rise from 2 or more")
    return ",".join(str(x) for x in vals)


def engine_args(cfg: dict) -> list[str]:
    """The engine's arguments: the config's, and with several GPUs the layer split across them ("layer_split" in the
    config: "auto" by default, or the first layer of each later GPU's share, e.g. "18" or "16,32"; see
    layer_split_value)."""
    args = list(cfg["args"])
    if len(gpu_list(cfg)) > 1 and "--layer-split" not in args:
        args += ["--layer-split", layer_split_value(cfg)]
    # opt-in: an auto split runs on the first card alone when it holds every profiled expert and the KV
    if len(gpu_list(cfg)) > 1 and cfg.get("split_skip_if_fits") and "--split-skip-if-fits" not in args:
        args.append("--split-skip-if-fits")
    # #533 (opt-in): "vram_elastic": true - the expert cache in segments, so POST /v1/vram can give VRAM back to other
    # programs and take it back; "vram_segment_mib" sets the segment size (the engine's default: 512)
    if cfg.get("vram_elastic") is True and "--vram-elastic" not in args:
        args.append("--vram-elastic")
        seg = cfg.get("vram_segment_mib")
        if isinstance(seg, int) and not isinstance(seg, bool) and seg > 0 and "--vram-segment-mib" not in args:
            args += ["--vram-segment-mib", str(seg)]
    args += parallel_args(cfg, args)
    return learned_profile_args(cfg, args)


PARALLEL_MAX = 8      # the engine's batch window holds at most 8 rows (kVerifyMaxT); --batch-mtp waves more through it


def parallel_args(cfg: dict, args: list[str]) -> list[str]:
    """#465 (opt-in): "parallel": N in the config - up to N requests decode together in the engine's batch slots
    (--batch N); more wait their turn.  1 or absent: one request at a time, as before.  A value the engine cannot use
    is said and passed on: the engine warns and adjusts (recommend, never force)."""
    if "--batch" in args or "--slots" in args:
        return []
    n = cfg.get("parallel")
    if n is None or n is False:
        return []
    if isinstance(n, bool) or not isinstance(n, int):
        print(f'[hypercore] "parallel" must be a whole number of requests (2..{PARALLEL_MAX}), not {n!r}: ignored',
              flush=True)
        return []
    if n <= 1:
        return []
    if n > PARALLEL_MAX and "--batch-mtp" not in args and os.environ.get("HYPERCORE_BATCH_MTP", "0") in ("", "0"):
        print(f'[hypercore] "parallel": {n} - the engine runs at most {PARALLEL_MAX} requests together; it will use '
              f"{PARALLEL_MAX}", flush=True)
    return ["--batch", str(n)]


def profile_shape(path: str) -> tuple[int, int] | None:
    """An expert profile's (layers, experts per layer), from its header (tools/make_profile.py's format), or None
    when the file is missing, is not one or is shorter than the pairs its header promises."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return None
    if len(head) < 24 or head[:4] != b"STRP":
        return None
    _, nl, ne, _, n = struct.unpack("<5I", head[4:])
    return (nl, ne) if size >= 24 + 4 * n else None


def learned_profile_args(cfg: dict, args: list[str]) -> list[str]:
    """#477 (opt-in): "expert_profile_save": "<path>" in the config has the engine (0.1.36+) save what its adaptive
    tier learned there - on QUIT and every "expert_profile_save_every" minutes (10 by default, 0 = at QUIT only) -
    and the next start begins from it instead of the config's --expert-profile, when it is a profile of the same
    model (its header's layers and experts match); otherwise from the config's own, as before.  A relative path is
    the engine's (the config's "cwd").  Without the key, the arguments are the config's, unchanged."""
    save = cfg.get("expert_profile_save")
    if not isinstance(save, str) or not save.strip() or "--expert-profile-save" in args:
        return args
    args = args + ["--expert-profile-save", save]
    every = cfg.get("expert_profile_save_every")
    if isinstance(every, (int, float)) and not isinstance(every, bool) and every >= 0:
        args += ["--expert-profile-save-every", str(every)]
    if "--expert-profile" in args[:-1]:
        i = args.index("--expert-profile") + 1
        here = cfg.get("cwd") or "."
        learned = profile_shape(save if os.path.isabs(save) else os.path.join(here, save))
        base = profile_shape(args[i] if os.path.isabs(args[i]) else os.path.join(here, args[i]))
        if learned is not None and learned == base:
            args[i] = save
    return args


def hip_visible(cfg: dict) -> list[int]:
    """AMD: the devices the engine should see, as the HIP runtime numbers them (HIP_VISIBLE_DEVICES).

    On Linux setup's KFD order is HIP's order, so the config's "gpu" is it.  On Windows setup finds the cards in the
    display-adapter order, and an integrated Radeon that HIP also enumerates takes ordinal 0 and pushes the discrete
    card to 1 (#325): setup records the ordinal `hypercore-device --list-devices` gave the card as "hip_ordinal", which
    wins for a one-card config.  Without it (a config from before), the config's "gpu"."""
    ordinal = cfg.get("hip_ordinal")
    if ordinal is not None and str(ordinal).strip() != "" and len(gpu_list(cfg)) <= 1:
        try:
            return [int(str(ordinal).strip())]
        except ValueError:
            pass
    return gpu_list(cfg)


def child_env(cfg: dict) -> dict:
    """The engine's environment: the CUDA libraries setup installed (pip's nvidia packages, or the toolkit that
    compiled it) first on the library search path."""
    env = dict(os.environ)
    if hip_visible(cfg) and cfg.get("backend") == "hip":   # AMD: numbered as HIP numbers them (hip_visible)
        env["HIP_VISIBLE_DEVICES"] = ",".join(str(i) for i in hip_visible(cfg))
    elif gpu_list(cfg):                              # issue #51: the GPU(s) to run on, numbered as nvidia-smi does; CUDA's
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"      # own default order (fastest first) can number the cards otherwise
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_list(cfg))
    for k, v in (cfg.get("env") or {}).items():      # engine settings the config carries (AMD: the GEMM tuning table)
        env[str(k)] = str(v)
    dirs = [d for d in cfg.get("lib_dirs") or [] if Path(d).is_dir()]
    if dirs:
        var = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
        env[var] = os.pathsep.join(dirs + ([env[var]] if env.get(var) else []))
    return env


def vision_env(cfg: dict, env: dict) -> dict:
    """The image encoder's environment: the engine's, unless the config's vision section names its own "cuda_device"
    (numbered like nvidia-smi) - then the encoder runs on that card alone, so a spare GPU can hold it while the engine
    keeps all of its own cards' VRAM (#408, Efs-O).  Without it, nothing changes.

    GGML_NO_BACKTRACE: llama.cpp's crash reporter forks gdb and writes its stack trace on stdout, which is the pipe
    _start() reads - so a failed start is reported as "[New LWP 12345]" instead of the reason.  The encoder's stderr
    (the assert, the cudaMalloc that did not fit) still goes to the log."""
    env = dict(env)
    env.setdefault("GGML_NO_BACKTRACE", "1")
    dev = (cfg.get("vision") or {}).get("cuda_device")
    if dev is None:
        return env
    if cfg.get("backend") == "hip":
        env["HIP_VISIBLE_DEVICES"] = str(dev)
    else:
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["CUDA_VISIBLE_DEVICES"] = str(dev)
    return env


class ByteTokenizer:
    """Tiny stand-in tokenizer for tests without the pack: one id per UTF-8 byte, specials as ids >= 256."""
    SPECIALS = ["<|im_start|>", "<|im_end|>", "<|endoftext|>", "<|vision_start|>", "<|image_pad|>", "<|vision_end|>"]

    ALWAYS = ()                                     # specials matched without parse_special (type 4, as <think>)

    @property
    def control_tokens(self):
        return [s for s in self.SPECIALS if s not in self.ALWAYS]

    def encode(self, text, parse_special=False, plain=()):
        out, i = [], 0
        while i < len(text):
            for k, s in enumerate(self.SPECIALS):
                if (parse_special or s in self.ALWAYS) and text.startswith(s, i) and not any(
                        a <= i < b for a, b in plain):
                    out.append(256 + k)
                    i += len(s)
                    break
            else:
                out.extend(text[i].encode("utf-8"))
                i += 1
        return out

    def decode(self, ids, errors="replace"):
        raw = bytearray()
        for t in ids:
            raw += self.SPECIALS[t - 256].encode() if t >= 256 else bytes([t])
        return raw.decode("utf-8", errors=errors)


# ------------------------------------------------------------------------------------------------ core
class Detokenizer:
    """Incremental decode: each token's bytes go through an incremental UTF-8 decoder, which emits the complete
    characters and holds a multi-byte character split across tokens until it is complete (invalid bytes become
    U+FFFD, as a whole decode with errors="replace" makes them).  Constant time per token - the old re-decode of
    every generated id cost 2 ms per token after 8K tokens and 4 ms after 16K (perf-review F-1).  A tokenizer
    without `token_bytes` (the tests' byte tokenizer) keeps the re-decode."""

    def __init__(self, tok):
        self.tok, self.ids, self.sent = tok, [], 0
        self.inc = codecs.getincrementaldecoder("utf-8")(errors="replace") if hasattr(tok, "token_bytes") else None

    def pending(self) -> bool:
        """A character is split across the tokens so far: its first bytes are held."""
        if self.inc is not None:
            return bool(self.inc.getstate()[0])
        return self.tok.decode(self.ids).endswith("\ufffd")

    def push(self, t: int) -> str:
        if self.inc is not None:
            return self.inc.decode(self.tok.token_bytes(t))
        self.ids.append(t)
        text = self.tok.decode(self.ids)
        if text.endswith("�"):
            return ""
        delta, self.sent = text[self.sent:], len(text)
        return delta


OPENAI_MAX_STOP = 4                                  # OpenAI's limit on `stop`


def stop_strings(req: dict) -> list[str]:
    """The request's stop strings: OpenAI's `stop` (a string or a list of up to 4) or Anthropic's `stop_sequences`
    (a list).  Empty strings are left out.  A bad value is a ValueError, so a 400 before anything is generated."""
    req = req or {}
    if req.get("stop") is not None:
        stop = req["stop"]
        stop = [stop] if isinstance(stop, str) else stop
        if not isinstance(stop, list) or not all(isinstance(x, str) for x in stop):
            raise ValueError("stop must be a string or a list of strings")
        if len(stop) > OPENAI_MAX_STOP:
            raise ValueError(f"stop takes at most {OPENAI_MAX_STOP} strings")
    elif req.get("stop_sequences") is not None:
        stop = req["stop_sequences"]
        if not isinstance(stop, list) or not all(isinstance(x, str) for x in stop):
            raise ValueError("stop_sequences must be a list of strings")
    else:
        return []
    return [x for x in stop if x]


class StopMatcher:
    """Cuts the answer's text at the first stop string.  Text that could still be the start of a stop string is held
    back (only that tail, never more), so a stop string split across tokens or chunks is still found and never sent."""

    def __init__(self, stops: list[str]):
        self.stops, self.held, self.hit = stops, "", None

    def push(self, text: str) -> str:
        """-> the text that is safe to send now; after a match, self.hit is the stop string and nothing more comes."""
        if self.hit is not None:
            return ""
        buf = self.held + text
        first = None
        for s in self.stops:
            i = buf.find(s)
            if i >= 0 and (first is None or i < first[0]):
                first = (i, s)
        if first is not None:
            self.held, self.hit = "", first[1]
            return buf[:first[0]]
        keep = 0                                      # the longest end of buf that a stop string starts with
        for s in self.stops:
            for k in range(min(len(s) - 1, len(buf)), keep, -1):
                if buf.endswith(s[:k]):
                    keep = k
                    break
        self.held = buf[len(buf) - keep:] if keep else ""
        return buf[:len(buf) - keep]

    def flush(self) -> str:
        held, self.held = self.held, ""
        return held



# Windows names that open a device whatever their extension or folder (Microsoft, "Naming Files, Paths, and Namespaces")
_WIN_DEVICES = {"con", "prn", "aux", "nul", "conin$", "conout$", *(f"com{i}" for i in "123456789\u00b9\u00b2\u00b3"),
                *(f"lpt{i}" for i in "123456789\u00b9\u00b2\u00b3")}


def slot_filename_problem(name) -> str | None:
    """/slots/0 {"filename": NAME}: why NAME is not one plain file name inside the slot save path, on Linux and on
    Windows alike (None when it is).  No separator or drive (`D:x`), no stream (`a:b`), no device (`NUL.txt`), no
    trailing dot or space (Windows drops them), no control character, no leading dot (temporary files use it)."""
    if not isinstance(name, str) or not name:
        return "must be a non-empty string"
    if len(name.encode("utf-8", "surrogatepass")) > 200:
        return "is too long (at most 200 bytes)"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "must not contain control characters"
    if any(c in name for c in '/\\:*?"<>|'):
        return "must be a plain file name (no / \\ : * ? \" < > |)"
    if name.startswith("."):
        return "must not start with a dot"
    if name.endswith((".", " ")):
        return "must not end with a dot or a space"
    if name.split(".")[0].rstrip(" ").lower() in _WIN_DEVICES:
        return "names a Windows device"
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return "is not valid Unicode"
    return None


def slot_save_dir(value, base: str | None = None) -> str:
    """The slot save path as one absolute, normalized directory (relative to `base`, else the server's working
    directory), created private to this user when missing.  ValueError for anything else."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("slot_save_path must be a directory path (a string)")
    if any(ord(c) < 32 for c in value):
        raise ValueError("slot_save_path must not contain control characters")
    path = os.path.abspath(os.path.join(base, value) if base and not os.path.isabs(value) else value)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if not os.path.isdir(path):
        raise ValueError(f"slot_save_path {path} is not a directory")
    return path

class HyperCoreRequestHandler:
    """Engine adapter over HyperCoreProcessSupervisor.

    Service keeps its old Engine surface: max_context, alive, exit_code, restart,
    unload, slots_view, generate, and a request summary in last. The actual inference
    travels through the binary IPC worker; no stdout lines are parsed.
    """

    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.log_path = None
        self.log_start = None
        self.spawn = (None, None)
        self.silent_note = ""
        self.progress = None
        self.last_err = ""
        self.reused = 0
        self.can_stop = False
        self.waiting = 0
        self.last = None

    @property
    def max_context(self) -> int:
        return int(getattr(self.supervisor, "max_context_len", 0) or 0)

    @property
    def batch(self) -> int:
        return int(getattr(self.supervisor, "max_batch_slots", 0) or 0)

    @property
    def info(self) -> dict[str, object]:
        """What this instance is and what it is configured to do.

        The three tuning keys are the instance baselines the worker reported in its CAPS
        reply. tools/calibrate.py reads them as the defaults its measurements must beat;
        without them it falls back to the product defaults and tunes against the wrong
        starting point.

        The expert-tier keys are the same reply's appended fields (#Monitor). The Monitor tab
        reads expert_slots/expert_cache_mib for "Experts in VRAM"; without them it shows "-"
        on a machine that has a cache, which looks like a missing feature and is not.
        """
        sup = self.supervisor
        caps = getattr(sup, "caps", {}) or {}
        return {
            "version": "hypercore-worker",
            "cvec": 1 if self.alive() else 0,
            "max_context": self.max_context,
            "max_batch_slots": self.batch,
            "pcie_frac": getattr(sup, "instance_pcie_frac", -1.0),
            "spec_min_p": getattr(sup, "instance_spec_min_p", -1.0),
            "pool_workers": int(caps.get("active_pool_workers", 0) or 0),
            "expert_slots": int(caps.get("expert_slots") or 0),
            "expert_slots_full": int(caps.get("expert_slots_full") or 0),
            "expert_cache_mib": int(caps.get("expert_cache_mib") or 0),
            "expert_cache_full_mib": int(caps.get("expert_cache_full_mib") or 0),
            "vram_free_mib": caps.get("vram_free_mib"),
            "expert_source": caps.get("expert_source"),
            "prompt_chunk": caps.get("prompt_chunk"),
        }

    def alive(self) -> bool:
        return bool(self.supervisor.alive())

    def exit_code(self) -> int | None:
        proc = getattr(self.supervisor, "_proc", None)
        return proc.returncode if proc is not None else None

    def restart(self, tries: int = 3) -> bool:
        ready = threading.Event()
        narrate_worker_start(self.supervisor, ready)     # the words the fixture path always had (#505)
        ok = self.supervisor.restart() if hasattr(self.supervisor, "restart") else self.supervisor.start_worker()
        ready.set()                                     # the narrator stops when the worker is up (or failed)
        if ok:
            self.last = None
            self.last_err = ""
        return ok

    def unload(self) -> None:
        self.supervisor.stop()

    def in_flight(self) -> int:
        return int(self.supervisor.in_flight())

    def slots_view(self) -> list[dict[str, object]]:
        return list(getattr(self.supervisor, "slots_view", list)())

    def monitor_snapshot(self) -> dict[str, object]:
        return self.supervisor.monitor_snapshot()

    @property
    def prefill_tok_s_mean(self) -> float | None:
        """Tokens per second the worker spent reading the last request's prompt.

        The terminal summary carries both halves of it - `prompt_read` and `prefill_ms` - so
        the rate is one division, not a second measurement. None until a request has finished
        with both fields; the worker's prompt-progress lines are not on the wire, so there is
        no live figure while a prompt is being read.
        """
        return self.monitor_snapshot().get("prefill_tok_s_mean")

    def death_note(self) -> str:
        err = getattr(self.supervisor, "last_stderr", lambda: "")()
        return err.strip() if err else ""

    def _request_last(self, summary: dict[str, object]) -> dict[str, object]:
        """The last request's figures, in the names the Monitor and the history rows use.

        A counter the worker did not report stays None. The Recent requests table renders None
        as "-"; rendering it as 0 would tell a reader the expert cache had no hits, which is a
        different fact and a more alarming one.
        """
        return {
            "generated": int(summary.get("produced_tokens", 0) or 0),
            "prompt_ms": float(summary.get("prefill_ms", 0) or 0),
            "decode_ms": float(summary.get("decode_ms", 0) or 0),
            "finish": summary.get("stop_reason", "unknown"),
            "reused": int(summary.get("prompt_reused", 0) or 0),
            "prompt_read": summary.get("prompt_read"),
            # the same division the Monitor's Prefill card uses, from the same table row
            "prefill_tok_s_mean": derive_monitor_field("prefill_tok_s_mean", summary, {}),
            "drafts_offered": int(summary.get("drafts_offered", 0) or 0),
            "drafts_accepted": int(summary.get("drafts_accepted", 0) or 0),
            "hits": summary.get("cache_hits"),
            "lookups": summary.get("cache_lookups"),
            "offloaded": summary.get("offloaded"),
            "ram_blobs": summary.get("ram_blobs"),
            "file_blobs": summary.get("file_blobs"),
            "file_mb": (int(summary["file_kb"]) / 1000.0) if summary.get("file_kb") is not None else None,
        }

    def generate(self, ids, max_new, sampling=None, cancel=None, embeddings=None):
        if not self.alive():
            raise ModelBusy("the HyperCore worker is not running")
        # The worker is the authoritative gate: it answers the ack frame with
        # HYPERCORE_STATUS_BUSY and the supervisor raises WorkerBusy. A host-side
        # count here would be a snapshot that can drift from the running config.
        self.last = None
        if cancel is None:
            cancel = threading.Event()
        try:
            yield from self.supervisor.generate(ids, max_new, sampling or {}, cancel, embeddings=embeddings)
        except Exception as exc:
            self.last_err = str(exc)
            raise
        finally:
            # A turn that ended on the end-of-turn token closes this generator at that token,
            # so the summary has to be taken on the way out, not after the loop.
            summary = getattr(self.supervisor, "last_summary", None)
            if summary:
                self.last = self._request_last(summary)

# ---------------------------------------------------------------- raw inference lane
# POST /infer/stream is the raw-token route: it speaks the frame protocol directly and
# streams the frames out. The API routes build OpenAI/Anthropic-shaped bodies from the
# same generator; this one is the wire, for embedders and for the load tests.
#
# The loop below is deliberately thin. Everything that is stream *state* - the ack,
# the token/terminal transitions, the timeout walls, the CANCEL frame on early exit -
# lives in HyperCoreProcessSupervisor.stream(). This function only serialises what it
# is handed and notices that the client has gone.

# The wall for the worker's ack to a submit. It is not the stream's silence limit:
# that is the supervisor's own (the config's "engine_silence_s"), and a prompt of six
# figures takes longer to read than this.
STREAM_ACK_S = 30.0


def stream_sse(
    handler,
    supervisor_instance: HyperCoreProcessSupervisor,
    request_id: int,
    prompt_ids: list[int],
    temperature: float,
    top_p: float,
    top_k: int,
    max_new: int,
    pcie_frac: float = -1.0,
    spec_min_p: float = -1.0,
    stall_s: float | None = None,
    timeout_s: float = STREAM_ACK_S,
) -> str:
    """Drive the supervisor's generator and write SSE. Returns an outcome token.

    "complete", "client_gone", "stalled", "worker_gone", or "busy"/"rejected" when
    the worker refused the request before a single frame was written - those two are
    returned rather than raised so the caller can still choose a status code.
    """
    started = False

    def emit(obj: dict) -> bool:
        nonlocal started
        if not started:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-store")
            handler._cors()
            handler.end_headers()
            started = True
        try:
            handler.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    try:
        for msg_type, token_id, metrics in supervisor_instance.stream(
            prompt_ids,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_new_tokens=max_new,
            pcie_frac=pcie_frac,
            spec_min_p=spec_min_p,
            timeout_s=timeout_s,
            silence_s=stall_s,
            request_id=request_id,
        ):
            if msg_type == STREAM_TOKEN:
                if not emit({"token_id": token_id,
                            "prompt_reused": int(metrics.get("prompt_reused") or 0)}):
                    return "client_gone"
            elif msg_type == STREAM_TERMINAL:
                logger.info(
                    "[MONITOR] request_id=%d status=success prompt_ms=%s decode_ms=%s "
                    "tokens=%s ttft_ms=%s stop_reason=%s",
                    request_id,
                    metrics.get("prefill_ms", 0),
                    metrics.get("decode_ms", 0),
                    metrics.get("produced_tokens", 0),
                    metrics.get("time_to_first_token_ms", 0),
                    metrics.get("stop_reason", "unknown"),
                )
                if not emit({"done": True,
                             "prompt_reused": int(metrics.get("prompt_reused") or 0),
                             "metrics": metrics}):
                    return "client_gone"
                return "complete"
    except StreamStalled as e:
        logger.error("[MONITOR] request_id=%d status=stall %s", request_id, e)
        emit({"done": True, "error": "stream stalled", "stop_reason": "cancelled"})
        return "stalled"
    except WorkerBusy as e:
        if not started:
            return "busy"
        logger.error("[MONITOR] request_id=%d status=busy %s", request_id, e)
        emit({"done": True, "error": str(e), "stop_reason": "cancelled"})
        return "busy"
    except WorkerRejected as e:
        if not started:
            return "rejected"
        emit({"done": True, "error": str(e), "stop_reason": "cancelled"})
        return "rejected"
    except WorkerUnreachable as e:
        logger.error("[MONITOR] request_id=%d status=worker_gone %s", request_id, e)
        if not started:
            return "worker_gone"
        emit({"done": True, "error": str(e), "stop_reason": "cancelled"})
        return "worker_gone"
    return "complete"


class Service:
    def __init__(self, engine: Engine, tokenizer, template: ChatTemplate, model_name: str = "qwen3.8-flash-next",
                 vision: Vision | None = None, sampling_defaults: dict | None = None,
                 fit_max_tokens: bool = False, trim_context: bool = False):
        self.engine, self.tok, self.template, self.model, self.vision = engine, tokenizer, template, model_name, vision
        self.literals = literal_tags(getattr(tokenizer, "control_tokens", ()))   # texts that stay text inside a message
        self.fit_max_tokens = fit_max_tokens          # --fit-max-tokens: clamp the output cap instead of 400
        # --trim-context: fit an oversized prompt by dropping the oldest turns instead of answering 400.
        # Off by default: "requests are never truncated" is the contract (module docstring, #545).
        self.trim_context = trim_context
        self.last_trim: dict | None = None           # what the last trimmed request lost, for the tests and the log
        self._prefix_cache: dict[str, dict] = {}     # tool surface -> the tokens the template adds around a prompt
        self.aliases: list[str] = []                  # #297: other names of the model (the config's `aliases`)
        self.sampling_defaults = dict(sampling_defaults or {})   # the run config's `sampling` block
        self.shared = {}                              # the web app's Chat settings for every client (POST /settings)
        self.shared_path = None                       # where they are kept between starts (next to the config)
        self.fifo = threading.Lock()
        self.slot_save_path = None                    # --slot-save-path: /slots/0?action=save|restore (off when None)
        self.embeddings = threading.local()           # the current request's image embeddings file (GENI)
        self.api_key = ""                              # when set, /v1/* needs it (Bearer or x-api-key)
        # #458 (opt-in, the config's "effort_position": "end"): a non-default reasoning effort goes in a short system
        # turn before the answer instead of the top of the prompt, so switching it keeps the cached conversation
        self.effort_end = False
        self.conv_log = ConvCacheLog()                  # #596: the parked conversations, from the engine's log
        self.config_path = None                         # #564: the run config the web page's Settings view edits
        self.config_lock = threading.Lock()
        # #321: browser pages of these origins may call /v1/* (CORS; "*" = any page - only with an api_key that
        # matters); empty = no CORS headers at all, as before
        self.cors_origins: list[str] = []
        # #321: origins that count as HyperCore's own page for /settings and MCP tools, e.g. the web app reached through a
        # reverse proxy or tunnel whose Host differs ("https://hypercore.example.com"); never a wildcard
        self.trusted_origins: list[str] = []
        # DNS rebinding: extra Host names this server answers to (the config's allowed_hosts, $HYPERCORE_ALLOWED_HOSTS;
        # "*" = any), and every name it answers to, which serve() works out from the address it listens on
        self.allowed_hosts: list[str] = []
        self.host_names: set[str] = set(LOOPBACK_NAMES)
        self.status = {"busy": False, "queued": 0}      # GET /status: what the model is doing right now
        self.rate = collections.deque(maxlen=32)        # (time, generated) samples for the live tok/s window
        # "parallel" (the engine's batch slots): every running request's own status and rate window; self.status
        # then says busy while any runs and shows the newest one
        self.live_reqs: dict[int, tuple[dict, collections.deque]] = {}
        # #332: the API request monitor (/api-monitor) keeps the last 100 requests' prompts and answers in memory,
        # so it is off unless the config's "api_monitor" (or --api-monitor) turns it on
        self.api_monitor = False
        self.api_requests = collections.deque(maxlen=100)  # bounded I/O in memory; no headers or API keys
        self.request_trace = threading.local()
        self.history = collections.deque(maxlen=500)    # the last finished requests, newest last (GET /metrics)
        # since the server started (the Monitor's totals, issue #35)
        self.totals = {"since": time.time(), "requests": 0, "prompt_tokens": 0, "reused": 0, "output_tokens": 0,
                       "prompt_ms": 0.0, "decode_ms": 0.0,
                       "drafts_offered": 0, "drafts_accepted": 0}   # #457: the MTP drafts, summed where reported
        self.last_timings = None                         # the last finished request's, llama.cpp's names (/v1/status)
        self.last_request_at = None                      # when a request last started or finished
        self.started_at = time.time()
        self.status_lock = threading.Lock()
        self.engine_stopped = False                   # POST /v1/stop set it: the engine is down until /v1/start
        self.engine_retry = None                      # serve.offline_engine.EngineRetry while no engine is running
        self.mcp = None                                  # serve/mcp/'s McpHub when MCP servers are configured
        # GET /prompts: the agent profiles the web app's dropdown is built from. prompt.json beside the
        # server, or the one in the prompts/ folder beside it, is re-read when it changes, so a new profile
        # reaches the menu with no code change and no restart.
        self.prompts_path = agent_prompts.find_prompt_file(ROOT)
        # sharing the GPU with other programs (all off by default): unload the engine after this many idle seconds,
        # only start it again when this much VRAM is free, and run this command first (e.g. to unload another
        # server's model); the next request after an unload starts the engine again
        self.idle_unload_s = 0
        self.min_free_vram_mib = 0
        self.vram_free_wait_s = 15.0                     # how long a full card is waited on (memory given back)
        self.before_load = None
        self.before_load_timeout_s = 120.0               # #1997: how long the before_load command may run
        self.before_load_ignore_failure = False          # #1997: load even when it fails, without a VRAM check
        self.before_load_strict = False                  # #1997: refuse the request when it fails
        self.vram_reserve = None                         # #533: the last POST /v1/vram reserve (None: the start's)
        self.reasoning_loop_recovery = False
        self.tool_call_recovery = False                  # #2037: opt-in, read a tool call written as JSON
        self.reasoning_budget_tokens = 0                 # #123: the config's default thinking budget (0: none)
        self.repeat_stop_tokens = REPEAT_STOP_TOKENS     # #606: one token this many times in a row ends a reply (0: off)
        # the turn-tail check (#...): off until the config or --turn-tail-check turns it on
        self.turn_tail_check = False
        self.turn_tail_checks = TAIL_CHECK_MAX
        self.turn_tail_prompt = TAIL_CHECK_PROMPT
        self.anthropic_think_unasked = True               # #278: "anthropic_thinking": "on_request" -> False
        # #641: the banner's Shut Down button. The request only sets this; main()'s wait loop
        # sees it and runs the same closers Ctrl+C and SIGTERM run, so a shutdown from the
        # page and a shutdown from the terminal end up identical on Windows and Linux.
        self.stop_requested = threading.Event()
        self.stop_ids = set(tokenizer.encode(IM_END, parse_special=True) +
                            tokenizer.encode("<|endoftext|>", parse_special=True))

    def loaded(self) -> bool:
        return not hasattr(self.engine, "alive") or self.engine.alive()

    def slot_action(self, slot: str, action: str, filename) -> tuple[int, dict]:
        """llama-server's POST /slots/{id}?action=save|restore {"filename": ...}: the conversation the engine holds,
        to or from a file in slot_save_path.  -> (HTTP status, body)."""
        def error(code, message):
            return code, {"error": {"code": code, "message": message,
                                    "type": "invalid_request_error" if code < 500 else "server_error"}}
        if not self.slot_save_path or not hasattr(self.engine, "session_file"):
            return error(501, "slot save/restore is disabled (start the server with --slot-save-path DIR)")
        if getattr(self.engine, "batch", 0):
            # #465 parallel requests do not hold the FIFO and share the engine's control lines: a session file
            # could interleave with a request being admitted, so the two are not combined (the engine refuses too)
            return error(501, "slot save/restore is not available with parallel requests (\"parallel\" / --batch)")
        if slot != "0":
            return error(400, "this server has one slot: 0")
        if action not in ("save", "restore"):
            return error(400, "action must be save or restore")
        why = slot_filename_problem(filename)
        if why:
            return error(400, f"filename {why}")
        path = os.path.join(self.slot_save_path, filename)
        if action == "restore":
            try:
                os.lstat(path)                              # the engine opens it without following a link
            except OSError:
                return error(404, f"no saved session named {filename}")
        self.last_request_at = time.time()                  # the idle unload counts this as activity
        with self.status_lock:
            self.status["queued"] += 1
        waiting = True
        try:
            with self.fifo:
                with self.status_lock:
                    self.status["queued"] -= 1
                waiting = False
                if not self.loaded():
                    return error(503, "the model is not loaded")
                with self.status_lock:
                    self.status.update(busy=True, phase="saving the session" if action == "save"
                                       else "restoring a session", started=time.time(), first_token=None,
                                       prompt_tokens=None, generated=None, max_tokens=None)
                try:
                    r = self.engine.session_file(action, path)
                except SessionRefused as e:
                    body = error(e.status, str(e))
                    body[1]["error"]["kind"] = e.kind
                    if e.published:
                        body[1]["error"]["published"] = True
                    return body
                except ValueError as e:                     # the request itself (an invalid command)
                    return error(400, str(e))
                finally:
                    with self.status_lock:
                        self.status["busy"] = False
                    self.last_request_at = time.time()
        finally:
            if waiting:                                     # never reached the FIFO (an exception while waiting)
                with self.status_lock:
                    self.status["queued"] -= 1
        if action == "save":
            return 200, {"id_slot": 0, "filename": filename, "n_saved": r["tokens"], "n_written": r["bytes"],
                         "timings": {"save_ms": r["ms"]}}
        return 200, {"id_slot": 0, "filename": filename, "n_restored": r["tokens"], "n_read": r["bytes"],
                     "timings": {"restore_ms": r["ms"]}}

    def set_aliases(self, aliases) -> None:
        """#297: the config's `aliases` - a list of names (or one comma-separated string), like llama-server's --alias.
        ValueError for anything else."""
        if aliases is None:
            aliases = []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        if not isinstance(aliases, list) or not all(isinstance(x, str) for x in aliases):
            raise ValueError(f"aliases={aliases!r}: expected a list of model names")
        names = []
        for x in (x.strip() for x in aliases):
            if x and x != self.model and x not in names:
                names.append(x)
        self.aliases = names

    def model_names(self) -> list[str]:
        return [self.model, *self.aliases]

    def model_for(self, req) -> str:
        """The name to answer with: the request's own when it is the model's name or an alias (#297), else the model's.
        Other names are still served, as before."""
        asked = req.get("model") if isinstance(req, dict) else None
        return asked if isinstance(asked, str) and asked in self.aliases else self.model

    def reasoning_budget(self, req) -> int | None:
        """#123: the most tokens this request may think, or None: the request's `reasoning_budget_tokens`, else the
        config's.  0 (or less) means no budget, so a request can turn a configured one off.  ValueError (a 400) for
        anything that is not a whole number."""
        value = (req or {}).get("reasoning_budget_tokens") if isinstance(req, dict) else None
        if value is None:
            value = self.reasoning_budget_tokens
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"reasoning_budget_tokens={value!r}: expected a whole number of tokens (0: no budget)")
        return value if value > 0 else None

    def _vision_down(self) -> bool:
        return self.vision is not None and hasattr(self.vision, "alive") and not self.vision.alive()

    def free_vram_mib(self) -> int | None:
        """Free VRAM on the engine's (first) GPU, from NVML (AMD backend: amdgpu's sysfs files, #301); None when it can't
        be read (then nothing is refused)."""
        try:
            if getattr(self, "backend", None) == "hip":
                from serve.telemetry import free_vram_mib
                return free_vram_mib(int(getattr(self, "gpu_index", 0) or 0), amd=True)
            from serve.telemetry import _Nvml
            nv = _Nvml(int(getattr(self, "gpu_index", 0) or 0))
            if not nv.ok():
                return None
            m = nv.Mem()
            if nv.lib.nvmlDeviceGetMemoryInfo(nv.dev, ctypes.byref(m)) != 0:
                return None
            return int(m.free >> 20)
        except Exception:
            return None

    def start_engine(self) -> bool:
        """POST /v1/start: bring the engine back after /v1/stop.

        A Start the user asked for is not a crash, so it is not charged against the supervisor's restart budget:
        stopping and starting a working engine several times must not exhaust it and leave every later request
        running against a worker that is no longer there.

        Whether it worked is read from the engine, not from the call's return value: the process engines' restart()
        returns None on success and raises when the process exits before READY. Returns False when the engine did
        not come back, and self.engine_stopped stays True in that case so the page can try again.
        """
        supervisor = getattr(self.engine, "supervisor", None)
        allow = getattr(supervisor, "allow_deliberate_restart", None)
        if allow is not None:
            allow()
        try:
            if hasattr(self.engine, "restart"):
                self.engine.restart()
            elif hasattr(self.engine, "start_worker"):
                self.engine.start_worker()
            else:                                   # an engine with no restart path: load it the plain way
                self.engine_stopped = False
                self.ensure_loaded()
        except (EngineDied, EngineStuck, RuntimeError, OSError) as error:
            print(f"[hypercore] the engine did not come back: {error}", flush=True)
            self.engine_stopped = True
            return False
        self.engine_stopped = not self.loaded()
        return not self.engine_stopped

    def ensure_loaded(self):
        """Start the engine if it is not running (unloaded, or it died - issue #27), after the before_load hook and
        the free-VRAM check.  The caller holds self.fifo.  A deliberate Stop is not an unload: while the engine is
        stopped this refuses rather than loading it behind the button."""
        if self.engine_stopped:
            raise EngineStopped("the engine is stopped: start it again (POST /v1/start) before sending a request")
        if isinstance(self.engine, OfflineEngine) and self.engine_retry:
            # No engine is running and the UI asked for one: try once, now, rather than wait
            # for the next scheduled attempt.
            state = self.engine_retry.try_now()
            if self.loaded():
                return
            raise EngineUnavailable(state["reason"], state["interval_s"])
        if self.loaded() and not self._vision_down():
            return
        hook_failed = None
        if self.before_load:
            cmd = self.before_load
            print(f"[hypercore] before loading: {cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))}", flush=True)
            why = None
            try:
                r = subprocess.run(cmd, shell=isinstance(cmd, str), timeout=self.before_load_timeout_s,
                                   stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    why = f"it exited with code {r.returncode}"
            except subprocess.TimeoutExpired:
                why = f"it did not finish in {self.before_load_timeout_s:g} s (before_load_timeout_s)"
            except (OSError, subprocess.SubprocessError) as e:
                why = f"it could not run ({e})"
            if why is not None:
                # #1997: a failing hook (e.g. the other server's /unload that is still busy) may mean the GPU was not
                # given back.  A peer that is simply not running also makes `curl` fail (exit 7), so the exit code
                # alone does not refuse: the card's free VRAM decides (min_free_vram_mib, checked just below);
                # "strict" always refuses, "ignore" always loads.
                if self.before_load_strict and not self.before_load_ignore_failure:
                    print(f"[hypercore] the before_load command failed ({why}); the model stays unloaded and this "
                          "request is answered 503 - try again (before_load_strict)", flush=True)
                    raise GpuBusy(f"the before_load command failed ({why}): the GPU was not given back, so the "
                                  "model was not loaded; try again in a moment", retry_after=15)
                hook_failed = why
                if self.before_load_ignore_failure or not self.min_free_vram_mib:
                    print(f"[hypercore] the before_load command failed ({why}); loading anyway"
                          + ("" if self.before_load_ignore_failure else
                             " (set min_free_vram_mib to have the free VRAM checked, or before_load_strict to refuse)"),
                          flush=True)
                else:
                    print(f"[hypercore] the before_load command failed ({why}); checking the free VRAM", flush=True)
        if self.min_free_vram_mib and not (hook_failed and self.before_load_ignore_failure):
            free = self.free_vram_mib()
            deadline = time.time() + self.vram_free_wait_s   # memory another process just gave back can take a moment
            while free is not None and free < self.min_free_vram_mib and time.time() < deadline:
                time.sleep(0.5)
                free = self.free_vram_mib()
            if free is not None and free < self.min_free_vram_mib:
                if hook_failed and not self.before_load_ignore_failure:
                    print(f"[hypercore] the before_load command failed ({hook_failed}) and only {free} MiB of VRAM "
                          f"is free (the model needs {self.min_free_vram_mib}): not loading; 503", flush=True)
                raise GpuBusy(f"the GPU is in use by another program: {free} MiB of VRAM free, the model needs "
                              f"{self.min_free_vram_mib} (min_free_vram_mib) - it stays unloaded until that is free"
                              + (f"; the before_load command failed ({hook_failed})" if hook_failed else ""),
                              retry_after=15)
            if hook_failed and not self.before_load_ignore_failure:
                print(f"[hypercore] the before_load command failed ({hook_failed}), but {free} MiB of VRAM is free: "
                      "loading", flush=True)
        if self._vision_down():                         # first, as at a start: a GPU encoder takes its VRAM before
            print("[hypercore] starting the vision encoder again ...", flush=True)   # the engine sizes its cache
            self.vision.restart()
        if self.loaded():
            return
        if getattr(self.engine, "unloaded", False):
            print("[hypercore] loading the model again (it was unloaded) ...", flush=True)
        else:
            code = self.engine.exit_code() if hasattr(self.engine, "exit_code") else None
            print(f"[hypercore] the engine had stopped (exit code {code}); starting it again "
                  "(a minute or two) ...", flush=True)
        self.engine.restart()
        print("[hypercore] the engine is running again", flush=True)
        if self.vram_reserve is not None and hasattr(self.engine, "vram"):   # #533: the reserve asked for last
            try:
                self.engine.vram(self.vram_reserve)
            except (ValueError, EngineDied) as e:
                print(f"[hypercore] the VRAM reserve ({self.vram_reserve} MiB) was not applied: {e}", flush=True)

    vram_wait_s = 300.0                                  # #533: how long POST /v1/vram waits for a running request

    def vram(self, reserve_mib: int | None) -> dict:
        """#533, POST /v1/vram: keep `reserve_mib` of VRAM free for other programs (None: the reserve the engine
        started with), applied between requests - a request that is running finishes first (up to wait_s).  Only an
        engine started with --vram-elastic (the config's "vram_elastic": true) can do it; it never resizes on its own.
        An unloaded engine applies it when it loads.  -> {"status": ..., and the engine's figures}."""
        if not hasattr(self.engine, "vram"):
            raise ValueError("this engine cannot resize its VRAM use")
        if not self.fifo.acquire(timeout=self.vram_wait_s):
            raise ModelBusy("a request is still running; try again when it has finished")
        # parallel requests do not hold the fifo: the engine's control lines (a prompt being admitted) are taken too,
        # and the engine refuses a resize while a slot decodes
        ctl = getattr(self.engine, "ctl", None) if getattr(self.engine, "batch", 0) else None
        if ctl is not None and not ctl.acquire(timeout=self.vram_wait_s):
            self.fifo.release()
            raise ModelBusy("a request is still running; try again when it has finished")
        try:
            if not self.loaded():
                self.vram_reserve = reserve_mib
                return {"status": "not loaded", "reserve_mib": reserve_mib,
                        "note": "applied when the model loads"}
            out = self.engine.vram(reserve_mib)
            self.vram_reserve = reserve_mib
            print(f"[hypercore] VRAM: {out.get('vram_free_mib')} MiB free, expert cache {out.get('expert_cache_mib')} of "
                  f"{out.get('expert_cache_full_mib')} MiB ({out.get('expert_slots')} experts)", flush=True)
            return {"status": "ok", **out}
        finally:
            if ctl is not None:
                ctl.release()
            self.fifo.release()

    def _say_died(self, e: Exception) -> None:
        """The server window's line for an engine that died (or was ended, #481) in the middle of a request."""
        note = self.engine.death_note() if hasattr(self.engine, "death_note") else ""
        log = getattr(self.engine, "log_path", None)
        print(f"[hypercore] {e}. {note} The next request starts the engine again."
              f"{' Its log: ' + log if log else ''}", flush=True)

    def load(self):
        """POST /load and every generation request: start the engine now if it is unloaded (raises GpuBusy)."""
        # a request is on its way: the idle thread must not unload between this and the request's own start
        self.last_request_at = time.time()
        if self.loaded() and not self._vision_down():
            return
        trace = getattr(self.request_trace, "record", None)
        waiting = time.perf_counter()
        with self.fifo:
            loading = time.perf_counter()
            if trace is not None:
                with self.status_lock:
                    trace["queue_s"] += round(loading - waiting, 3)
                    trace["state"] = "loading"
            try:
                self.ensure_loaded()
            finally:
                if trace is not None:
                    with self.status_lock:
                        trace["load_s"] += round(time.perf_counter() - loading, 3)
                        trace["state"] = "queued"

    UNLOAD_WAIT_S = 300.0       # #1997: POST /unload?wait=1 waits this long for the running turn (wait=<s> to choose)

    def unload_waiting(self, wait_s: float) -> str:
        """#1997: unload(), but a running request is given up to wait_s seconds to finish first.  Returns "unloaded"
        when it did, "busy" when the wait ran out."""
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            r = self.unload()
            if r != "busy" or time.monotonic() >= deadline:
                return r
            time.sleep(0.25)

    def unload(self, idle_for: float | None = None) -> str:
        """Stop the engine between requests: "unloaded", "not loaded", "busy" (a request is running or waiting, or
        with idle_for: one ran more recently than that) or "unsupported"."""
        if not hasattr(self.engine, "unload"):
            return "unsupported"
        if not self.fifo.acquire(blocking=False):
            return "busy"
        try:
            if not self.engine.alive():
                return "not loaded"
            with self.status_lock:
                if self.status.get("busy") or self.status.get("queued"):
                    return "busy"
            if idle_for is not None and time.time() - (self.last_request_at or self.started_at) < idle_for:
                return "busy"
            self.engine.unload()
            if self.vision is not None and hasattr(self.vision, "unload"):
                self.vision.unload()
            print(f"[hypercore] model unloaded{f' after {idle_for:.0f} s idle' if idle_for else ''}; "
                  "the next request loads it again", flush=True)
            return "unloaded"
        finally:
            self.fifo.release()

    def start_idle_unload(self):
        if not self.idle_unload_s or not hasattr(self.engine, "unload"):
            return
        print(f"[hypercore] the model unloads after {self.idle_unload_s} s without requests", flush=True)

        def loop():
            while True:
                time.sleep(max(1.0, min(30.0, self.idle_unload_s / 4)))
                try:
                    self.unload(idle_for=self.idle_unload_s)
                except EngineStuck as e:                # tried again at the next turn; the thread keeps running
                    print(f"[hypercore] idle unload: {e}", flush=True)
        threading.Thread(target=loop, daemon=True).start()

    def set_shared(self, defaults) -> dict:
        """The Chat settings every client gets for what it leaves out; {} / None = clients use their own again."""
        self.shared = clean_shared_defaults(defaults)
        if self.shared_path:
            try:
                if self.shared:
                    Path(self.shared_path).write_text(json.dumps(self.shared, indent=1), encoding="utf-8")
                else:
                    Path(self.shared_path).unlink(missing_ok=True)
            except OSError as e:
                print(f"[hypercore] could not save the shared settings: {e}", flush=True)
        return self.shared

    def with_shared(self, req: dict, api: str) -> dict:
        """The request with the shared thinking level and max tokens filled in where it has none of its own."""
        s = self.shared
        if not s:
            return req
        req = dict(req)
        if "max_tokens" in s and not req.get("max_tokens") and not req.get("max_completion_tokens"):
            req["max_tokens"] = s["max_tokens"]
        effort = s.get("reasoning_effort")
        if effort:
            if api == "openai":
                ctk = req.get("chat_template_kwargs") if isinstance(req.get("chat_template_kwargs"), dict) else {}
                if not req.get("reasoning_effort") and not req.get("reasoning") and \
                        "enable_thinking" not in ctk and "reasoning_effort" not in ctk:
                    req["reasoning_effort"] = effort
            elif not req.get("thinking") and not req.get("output_config"):
                if effort == "none":
                    req["thinking"] = {"type": "disabled"}
                else:
                    req["output_config"] = {"effort": effort}
        return req

    def start_telemetry(self):
        """The hardware sampler behind GET /metrics (serve/telemetry.py), recording this server's tok/s too."""
        if getattr(self, "telemetry", None) is None:
            from serve.telemetry import Telemetry
            self.telemetry = Telemetry(extra=lambda: {"tok_s": self._tok_s(), "tok_s_mean": self._tok_s_mean(),
                                                    "prefill_tok_s_mean": self._prefill_tok_s_mean()},
                                       gpu_index=int(getattr(self, "gpu_index", 0) or 0),
                                       gpu_indices=getattr(self, "gpu_indices", None),
                                       amd=getattr(self, "backend", None) == "hip")

    def _tok_s(self):
        """tok/s over the last RATE_WINDOW_S seconds.  Returns 0.0 while nothing is generating.  With parallel requests:
        the sum of theirs."""
        with self.status_lock:
            many = [(dict(st), list(rt)) for st, rt in self.live_reqs.values()]
        if many:
            return sum(self._rate_of(st, rt) for st, rt in many)
        with self.status_lock:
            s = dict(self.status)
            rate = list(self.rate)
        return self._rate_of(s, rate)

    @staticmethod
    def _rate_of(s, rate):
        if not s.get("busy") or not s.get("first_token"):
            return 0.0
        now = time.time()
        newest = rate[-1] if rate else None
        oldest = next(((t, g) for t, g in rate if now - t <= RATE_WINDOW_S), None)
        if newest and oldest and newest[0] - oldest[0] >= RATE_MIN_SPAN_S:
            return max(0.0, (newest[1] - oldest[1]) / (newest[0] - oldest[0]))
        return s["generated"] / max(RATE_MIN_SPAN_S, now - s["first_token"])

    def _tok_s_mean(self):
        """The whole-request mean since the first token (the old formula), kept so the two can be compared."""
        with self.status_lock:
            many = [dict(st) for st, _ in self.live_reqs.values()] or [dict(self.status)]
        return sum(s["generated"] / max(1e-6, time.time() - s["first_token"]) for s in many
                   if s.get("busy") and s.get("first_token"))

    def _prefill_tok_s_mean(self):
        """Tokens per second the engine spent reading a prompt, the cached prefix excluded.

        While a prompt is being read this is the worker's live prompt-progress rate. Once the
        first token has arrived the prefill is over, so the live number is 0 rather than a
        stale rate from the prompt just read. When the engine is idle, the Monitor's Prefill
        card uses the last finished request's rate; None means nothing has been measured.
        """
        with self.status_lock:
            busy, first_token = bool(self.status.get("busy")), self.status.get("first_token")
        if busy and first_token is not None:
            return 0.0
        rate = getattr(self.engine, "prefill_tok_s_mean", None)
        if rate is not None:
            return rate
        with self.status_lock:
            timings = dict(self.last_timings or {})
            last = self.history[-1] if self.history else None
        if timings.get("prompt_per_second") is not None:
            return float(timings["prompt_per_second"])
        if last and last.get("prompt_ms") and (last.get("prompt_read") or last.get("prompt_tokens")):
            read = last.get("prompt_read") or max(0, (last.get("prompt_tokens") or 0) - (last.get("reused") or 0))
            if read:
                return round(read / (float(last["prompt_ms"]) / 1000), 1)
        return None

    def begin_request(self, path, req):
        """#332: a monitor record for this request, or None when the monitor is off (nothing is kept then)."""
        if not self.api_monitor:
            return None
        raw = json.dumps(req, ensure_ascii=False, indent=2)
        record = {"id": uuid.uuid4().hex[:12], "path": path, "model": req.get("model") or self.model,
                  "started_at": time.time(), "state": "queued", "stream": bool(req.get("stream")),
                  "response_format": (req.get("response_format") or {}).get("type")
                  if isinstance(req.get("response_format"), dict) else None,
                  "input": raw[:262144], "input_truncated": len(raw) > 262144,
                  "output": "", "reasoning": "", "output_truncated": False, "reasoning_truncated": False,
                  "queue_s": 0.0, "load_s": 0.0, "first_token_s": None,
                  "_clock": time.perf_counter()}
        with self.status_lock:
            self.api_requests.append(record)
        self.request_trace.record = record
        return record

    def request_records(self, request_id=None):
        with self.status_lock:
            records = list(self.api_requests)
            if request_id:
                record = next((r for r in records if r["id"] == request_id), None)
                if record is None:
                    return None
                return {k: v for k, v in record.items() if not k.startswith("_")}
            return [{k: v for k, v in r.items()
                     if k not in ("input", "output", "reasoning", "response") and not k.startswith("_")}
                    | {"wallclock_s": r.get("wallclock_s", round(time.perf_counter() - r["_clock"], 3))}
                    for r in reversed(records)]

    def metrics(self, all_requests=False) -> dict:
        """GET /metrics: what the Monitor tab shows - the engine's facts, what it is doing, the last requests, and
        the hardware (with a minute of history per series)."""
        with self.status_lock:
            s = dict(self.status)
            if self.live_reqs:                          # parallel requests: the newest one's status
                s = {**s, **dict(list(self.live_reqs.values())[-1][0]), "queued": s.get("queued", 0)}
            hist = list(self.history)
            totals = dict(self.totals)
        now = time.time()
        progress = getattr(self.engine, "progress", None)
        if s.get("busy") and s.get("first_token") is None:
            state = "reading"
        elif s.get("busy"):
            state = "generating"
        elif not self.loaded():
            state = "unloaded"
        else:
            state = "idle"
        live = {"state": state, "queued": s.get("queued", 0), "phase": s.get("phase") if s.get("busy") else None,
                "prompt_tokens": s.get("prompt_tokens") if s.get("busy") else None,
                "prompt_read": None, "prompt_total": None, "generated": s.get("generated") if s.get("busy") else None,
                "max_tokens": s.get("max_tokens") if s.get("busy") else None,
                "elapsed_s": round(now - s["started"], 1) if s.get("busy") and s.get("started") else None,
                "tok_s": round(self._tok_s(), 1) if state == "generating" else None,
                "tok_s_mean": round(self._tok_s_mean(), 1) if state == "generating" else None,
                "prefill_tok_s_mean": self._prefill_tok_s_mean(),
                "tok_s_window_s": RATE_WINDOW_S if state == "generating" else None}
        if state == "reading" and progress:
            live["prompt_read"], live["prompt_total"] = progress
        par = int(getattr(self.engine, "batch", 0) or 0)
        if par:                                         # #465: the requests running together, slot by slot
            with self.status_lock:
                running = len(self.live_reqs)
            slots = self.engine.slots_view() if hasattr(self.engine, "slots_view") else []
            # the requests in flight that are not in a slot: the one alone on the solo path, or waiting their turn
            in_slots = sum(1 for x in slots if x.get("state", "idle") != "idle")
            live.update(parallel=par, running=running, slots=slots, outside_slots=max(0, running - in_slots),
                        waiting=int(getattr(self.engine, "waiting", 0) or 0))
            if running and state == "idle":
                live["state"] = "generating"
        engine = {"model": self.model, "max_context": self.engine.max_context, "images": self.vision is not None,
                  **dict(getattr(self.engine, "info", {}) or {})}
        tel = self.telemetry.snapshot() if getattr(self, "telemetry", None) else {"now": {}, "history": {}, "static": {}}
        parked = self.conv_log.poll(getattr(self.engine, "log_path", None), getattr(self.engine, "log_start", None))
        return {"engine": engine, "live": live, "requests": hist[::-1][:None if all_requests else 12],
                "conversation_cache": conversation_cache_view(engine, hist, totals, parked),
                "requests_kept": len(hist), "totals": totals, "hardware": tel["now"],
                "hardware_static":
                tel["static"], "history": tel["history"], "time": now}

    def v1_status(self) -> dict:
        """GET /v1/status: what this server is and does, for a client that would rather ask than guess (a front-end
        that polls its OpenAI-compatible server's status, collabosm's for one): the model and its window, images,
        the APIs, what is running, and the last request's timings in llama.cpp's names.  /metrics has the rest."""
        with self.status_lock:
            s, totals = dict(self.status), dict(self.totals)
            last_t, last_at = (dict(self.last_timings) if self.last_timings else None), self.last_request_at
        tel = self.telemetry.snapshot() if getattr(self, "telemetry", None) else {"now": {}, "static": {}}
        hw, static = tel.get("now") or {}, tel.get("static") or {}

        def scaled(v, unit, digits=0):
            return round(v / unit, digits) if isinstance(v, (int, float)) else None

        busy, ctx = bool(s.get("busy")), self.engine.max_context
        images = self.vision is not None
        return {
            "service": "hypercore", "model": self.model,
            "loaded": self.loaded(), "auto_load": hasattr(self.engine, "restart"),
            # stopped by POST /v1/stop: requests are refused (409) until POST /v1/start, not quietly reloaded
            "engine_stopped": bool(self.engine_stopped),
            "structured_output": {"formats": ["json_object", "json_schema"], "method": "prompt_and_validate",
                                  "constrained_decoding": False, "stream_buffered": True},
            "engine": (getattr(self.engine, "info", {}) or {}).get("version"),
            "started": int(self.started_at), "uptime_s": int(time.time() - self.started_at),
            "cache_max_tokens": ctx,
            "context": {"native": ctx, "max_positions": ctx},
            # one request at a time (more wait their turn), or "parallel": N batch slots (#465)
            "concurrency": {"serving": max(1, int(getattr(self.engine, "batch", 0) or 0)),
                            "requested": max(1, int(getattr(self.engine, "batch", 0) or 0))},
            "dialects": ["/v1/chat/completions", "/v1/messages", "/v1/responses"],
            # the end-of-turn "is the work done?" check: on, and how many times one turn may be asked
            "turn_tail": {"enabled": bool(self.turn_tail_check), "max_checks": int(self.turn_tail_checks),
                          "custom_prompt": bool(self.turn_tail_prompt != TAIL_CHECK_PROMPT)},

            "vision": {"enabled": images, "available": images, "error": None},
            "activity": {"requests": totals["requests"] + int(busy), "in_flight": int(busy) + int(s.get("queued") or 0),
                         "last_request_at": int(last_at) if last_at else None},
            "last_timings": last_t,
            "vram": dict((getattr(self.engine, "info", {}) or {}).get("vram") or {},
                         elastic=bool((getattr(self.engine, "info", {}) or {}).get("vram_elastic"))),
            "machine": {
                "at": int(time.time()),
                "gpu": {"name": static.get("gpu_name"), "used_mib": scaled(hw.get("gpu_mem_used"), 2 ** 20),
                        "total_mib": scaled(hw.get("gpu_mem_total"), 2 ** 20), "util_pct": hw.get("gpu_util"),
                        "temp_c": hw.get("gpu_temp"), "power_w": hw.get("gpu_power")} if static.get("gpu_name") else None,
                "ram": {"used_gib": scaled(hw.get("ram_used"), 2 ** 30, 1),
                        "total_gib": scaled(hw.get("ram_total"), 2 ** 30, 1)} if hw.get("ram_total") else None}}

    def render_prompt(self, messages, tools, kwargs) -> str:
        """The template rendered.  #458: with `effort_end`, a request with a non-default effort (low, medium or no
        thinking) is rendered as a default one up to the answer - the same prompt start, so the conversation cache
        keeps it - and its effort follows in a short system turn right before the answer (thinking off: the template's
        empty thinking block).  The engine (--tail-role-token) checkpoints in front of that turn."""
        effort = kwargs.get("reasoning_effort")
        off = kwargs.get("enable_thinking") is False
        if not self.effort_end or (not off and effort in (None, "", "xhigh", "high")):
            return self.template.render(messages, tools=tools, **kwargs)
        base = {k: v for k, v in kwargs.items() if k not in ("reasoning_effort", "enable_thinking")}
        history = self.template.render(messages, tools=tools, add_generation_prompt=False, **base)
        full = self.template.render(messages, tools=tools, add_generation_prompt=True, **kwargs)
        mine = self.template.render(messages, tools=tools, add_generation_prompt=False, **kwargs)
        if not full.startswith(mine):                  # a template this cannot take apart: rendered as asked
            return full
        tail = "" if off else EFFORT_TURN.format(EFFORT_TEXT.get(effort, EFFORT_TEXT["medium"]))
        return history + tail + full[len(mine):]

    def encode_prompt(self, messages, tools, kwargs) -> list[int]:
        """The request's prompt: the template rendered and tokenized.  #537: a <think> / </think> written inside a
        message's text is encoded as the text it is, not as the model's reasoning markers (the template's own are).
        A control token's text (<|im_start|>, <|im_end|>, <|endoftext|>, ...) inside a message is text as well: only
        the control tokens the template writes are control tokens."""
        marked, marked_tools, changed = mark_think_literals(messages, tools, self.literals)
        prompt = self.render_prompt(marked, marked_tools, kwargs)
        if not changed:
            return self.tok.encode(prompt, parse_special=True)
        prompt, plain = unmark_think_literals(prompt, self.literals)
        return self.tok.encode(prompt, parse_special=True, plain=plain)

    def tool_prefix(self, tools: list | None) -> dict:
        """What the template writes around the messages: the tool block, its instructions, the
        generation prompt. None of it is in the payload, and the browser has no tokenizer, so the
        page cannot count it; it anchors on prompt_tokens for every request after the first.

        `tokens` is the answer for a request that offers the tools, `base` for one that does not.
        Measured with an empty probe message, so the number carries that turn's own wrapper: a few
        tokens over, never under. Cached by the tool surface, so adding a server changes it and a
        steady chat does not.
        """
        tools = list(tools or [])
        key = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
        hit = self._prefix_cache.get(key)
        if hit is not None:
            return hit
        probe = [{"role": "user", "content": ""}]
        out = {"tokens": len(self.encode_prompt(probe, tools or None, {})),
               "base": len(self.encode_prompt(probe, None, {})),
               "tools": len(tools)}
        self._prefix_cache[key] = out
        return out

    def _note_unreadable_tool_images(self, messages):
        """A picture a tool returned (Claude Code's Read of an image file) that this server cannot read - it has no
        image encoder, or the encoder refuses that picture - becomes a short note in its place instead of a 400.
        Clients resend the whole history every turn, so one unreadable tool picture would fail every later request of
        the conversation; a picture the user sends keeps the 400.  A picture that encodes stays in the cache, so the
        prompt's own encode below finds it."""
        fetched = {}                     # a URL's bytes, so prepare() does not download the same picture again
        for m in messages:
            content = m.get("content")
            if m.get("role") != "tool" or not isinstance(content, list):
                continue
            for n, item in enumerate(content):
                if not isinstance(item, dict) or item.get("type") != "image":
                    continue
                why = "this server has no image encoder" if self.vision is None else None
                if why is None:
                    src = item["source"]
                    try:
                        if src.startswith(("http://", "https://")):
                            src = fetched[item["source"]] = Vision.download(src)   # outside the FIFO, as in prepare()
                        with self.fifo:          # the encoder takes its turn with the requests (see below)
                            self.vision.encode(src)
                    except (ValueError, OSError) as e:
                        why = str(e)
                if why is not None:
                    print(f"[hypercore] a picture a tool returned was left out of the prompt: {why}", flush=True)
                    content[n] = {"type": "text", "text": f"[image omitted: {why}]"}
        return fetched

    @staticmethod
    def _head_tail(text: str, keep: int) -> str:
        """Keep the beginning and the end of one turn's text, the same shape serve/web/app.js headTail uses.
        Below 80 characters there is nothing worth cutting, so the text is returned unchanged."""
        if keep < 80 or len(text) <= keep:
            return text
        head, tail = keep // 2, keep - keep // 2
        return (text[:head] + f"\n\n[... {len(text) - head - tail} characters omitted; the beginning and "
                              f"the end are kept]\n\n" + text[-tail:])

    @staticmethod
    def _text_chars(content) -> int:
        """How many characters of a message's content are text (images and other parts are not)."""
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(len(p["text"]) for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
        return 0

    def _fit_messages(self, messages: list, tools, kwargs, ctx: int, max_new: int | None) -> dict:
        """Make `messages` render inside `ctx - CTX_SLACK - max_new` tokens, in place.

        Leading system turns are the profile and stay. Then the oldest turns go one at a time; when only
        the newest turn is left and it is still too big, its text is head/tail-truncated. Only called on
        the path that would otherwise 400, so an ordinary request never pays for the extra encodes.
        Returns what was removed: turns dropped, characters clipped, and the prompt size before/after.
        """
        budget = max(1, ctx - CTX_SLACK - (max_new if max_new and max_new > 0 else 1))

        def size(ms: list) -> int:
            return len(self.encode_prompt(ms, tools, kwargs))

        head, i = [], 0
        while i < len(messages) and messages[i].get("role") == "system":
            head.append(messages[i])
            i += 1
        body = messages[i:]
        before = size(head + body)
        dropped = 0
        while len(body) > 1 and size(head + body) > budget:
            body.pop(0)
            dropped += 1
        clipped = 0
        if body and size(head + body) > budget:          # the newest turn alone does not fit
            last = body[-1]

            def fitted(keep: int) -> dict:
                content = last.get("content")
                if isinstance(content, str):
                    return {**last, "content": self._head_tail(content, keep)}
                if isinstance(content, list):
                    return {**last, "content": [({**p, "text": self._head_tail(p["text"], keep)}
                                                if isinstance(p, dict) and isinstance(p.get("text"), str) else p)
                                               for p in content]}
                return last

            text = last.get("content") or ""
            original = self._text_chars(text) if isinstance(text, (str, list)) else 0
            lo, hi = 80, original                     # the largest keep that still fits; below 80 there is
            while lo < hi:                           # nothing to cut, so headTail leaves the text alone
                mid = (lo + hi + 1) // 2
                if size(head + body[:-1] + [fitted(mid)]) <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            new = fitted(lo)
            clipped = max(0, original - self._text_chars(new.get("content")))
            body[-1] = new
        messages[:] = head + body
        return {"dropped": dropped, "clipped_chars": clipped,
                "tokens_before": before, "tokens_after": size(head + body)}

    def prepare(self, messages, tools, kwargs, max_new=None, force=None):
        """-> (ids, thinking, max_new). An unset or non-positive max_new (some clients send -1) means "unlimited":
        the rest of the context.  `force` (forced_call): without thinking the reply starts with it, so it ends the
        prompt; with thinking, Service.run writes it once the thinking is over."""
        fetched = self._note_unreadable_tool_images(messages)
        ids = self.encode_prompt(messages, tools, kwargs)
        if force and kwargs.get("enable_thinking", True) is False:
            ids = ids + self.tok.encode(force, parse_special=True)
        self.embeddings.path = None
        images = images_of(messages)
        if images:
            if self.vision is None:
                raise ValueError("this server was started without the vision encoder (run setup again and choose "
                                 "'vision'), so it cannot read images")
            pad = self.tok.encode(IMAGE_PAD, parse_special=True)[0]
            start = self.tok.encode(VISION_START, parse_special=True)[0]
            # An image URL is downloaded first, outside the FIFO: under it, a slow server held every other request
            # for as long as urlopen waited.
            images = [fetched[src] if src in fetched else Vision.download(src) if src.startswith(("http://", "https://"))
                      else src for src in images]
            # Encode only while the engine is idle: the engine and the image encoder (a separate process) must not
            # run on the GPU at the same time - an encode during a running request left that request stuck at
            # "reading the prompt" with CPU and GPU busy, for good (reproduced).  So encoding takes its turn in the
            # same FIFO as the requests.
            with self.fifo:
                encoded = [self.vision.encode(src) for src in images]
            # one <|image_pad|> per image -> one per image token.  Only the markers the template writes for an image
            # (right after <|vision_start|>) are images: the same text inside a message (an agent reading these docs,
            # #150) is kept as plain text, or it took an image's place and the counts no longer matched.
            literal = self.tok.encode(IMAGE_PAD, parse_special=False)
            out, k = [], 0
            for j, t in enumerate(ids):
                if t == pad and j > 0 and ids[j - 1] == start:
                    # more pairs than images: text parts that cut both markers apart (the literal marks keep whole ones text)
                    if k == len(encoded):
                        raise ValueError("the prompt and its images do not match")
                    out += [pad] * encoded[k][1]
                    k += 1
                elif t == pad:
                    out += literal
                else:
                    out.append(t)
            if k != len(encoded):
                raise ValueError("the prompt and its images do not match")
            ids = out
        ctx = self.engine.max_context
        if ctx <= 0:
            if getattr(self.engine, "starting", False):   # #344: (re)starting, not a prompt that is too long
                raise EngineStarting("the engine is starting (a minute or two); try again shortly")
            # a failed restart leaves max_context 0: check against the last known context, so the request reaches
            # run() - which starts the engine again - instead of failing with "exceeds the context (0)" forever
            ctx = getattr(self.engine, "known_ctx", 0)
            if ctx <= 0:
                raise EngineStarting("the engine is starting (a minute or two); try again shortly")
        room = ctx - CTX_SLACK - len(ids)
        if room < 1 and self.trim_context:
            # Nothing above can shorten a single oversized turn (a pasted file, one turn carrying many tool
            # results): the web app's pruneToContext is turn-granular and runs out of turns to drop. Fit it
            # here - oldest turns first, then the newest turn's own text - before refusing the request.
            self.last_trim = self._fit_messages(messages, tools, kwargs, ctx, max_new)
            ids = self.encode_prompt(messages, tools, kwargs)
            room = ctx - CTX_SLACK - len(ids)
            if self.last_trim["dropped"] or self.last_trim["clipped_chars"]:
                print(f"[hypercore] context trim: {self.last_trim['dropped']} oldest turn(s) dropped, "
                      f"{self.last_trim['clipped_chars']} characters clipped from the newest; prompt "
                      f"{self.last_trim['tokens_after']} of {ctx} tokens", file=sys.stderr, flush=True)
        if max_new is None or max_new <= 0 or (self.fit_max_tokens and room < 1):
            if room < 1:
                raise ValueError(f"prompt ({len(ids)} tokens) leaves no room to answer in the context "
                                 f"({ctx}); requests are never truncated")
            max_new = room
        elif max_new > room:
            if not self.fit_max_tokens:
                raise ValueError(f"prompt ({len(ids)} tokens) + max tokens ({max_new}) exceeds the context "
                                 f"({ctx}); requests are never truncated. Send a smaller "
                                 f"max_tokens (at most {max(0, room)} here), or add \"fit_max_tokens\": true to the "
                                 "model's hypercore-<model>.json to shorten it to the room left (#545)")
            max_new = max(1, room)          # --fit-max-tokens: a shorter completion beats a 400
        if images:
            # The request's images in one file for GENI (~10 MB a picture), written once nothing above refuses the
            # request: one refused after it (the engine starting, no room) left it in the vision directory for good,
            # one more for every retry of a 503.  run() deletes it; drop_embeddings() if run() never starts.
            combined = combined_embeddings_path(self.vision.dir, sum(Path(p).stat().st_size for p, _ in encoded))
            self.embeddings.path = combined             # first, so a half-written one is found as well
            write_temporary(combined, [p for p, _ in encoded])
        return ids, kwargs.get("enable_thinking", True) is not False, max_new

    def drop_embeddings(self) -> None:
        """Delete the combined image file prepare() wrote when no run() took it over (a run deletes its own as it
        ends): a request can end before its answer starts - its client gone, anything raised after prepare() - and
        then nothing else would.  The handler calls this when a request is done."""
        path, self.embeddings.path = getattr(self.embeddings, "path", None), None
        if path is not None:
            Path(path).unlink(missing_ok=True)

    def _note(self, n, evs, st=None, rate=None):
        with self.status_lock:
            s = self.status if st is None else st
            s["generated"] = n
            if s.get("first_token") is None:
                s["first_token"] = time.time()
            (self.rate if rate is None else rate).append((time.time(), n))   # the live rate's window (RATE_WINDOW_S)
            for ev in evs:
                if ev.kind == "reasoning":
                    s["phase"] = "thinking"
                elif ev.kind == "content":
                    s["phase"] = "answering"
                elif ev.kind == "tool_start":
                    s["phase"], s["tool"] = f"writing a tool call: {ev.call.name}", ev.call.name
                elif ev.kind == "tool_call":
                    s["phase"] = "tool call complete"
                s["tail"] = ((s.get("tail") or "") + (ev.text or ""))[-600:]

    def _progress(self, last_print, every=1.0, st=None):
        """A progress line in the server window every `every` seconds while a request runs."""
        now = time.time()
        if now - last_print < every:
            return last_print
        with self.status_lock:
            s = dict(self.status if st is None else st)
        el = now - s.get("started", now)
        if s.get("first_token") is None:
            pr = getattr(self.engine, "progress", None)   # (position reached, prompt tokens): a reused prefix counts
            done = f"{pr[0]:,} of {pr[1]:,}" if pr and pr[1] else f"{s.get('prompt_tokens', 0):,}"   # as read (#29)
            print(f"[hypercore] reading the prompt: {done} tokens, {el:.0f} s so far", flush=True)
        else:
            rate = s["generated"] / max(1e-6, now - s["first_token"])
            print(f"[hypercore] {s['phase']}: {s['generated']} of max {s.get('max_tokens')} tokens, {rate:.1f} tok/s, "
                  f"{el:.0f} s", flush=True)
        return now

    def run(self, ids, thinking, tools, max_new, sampling, cancel, force=None) -> Iterator[tuple[str, object]]:
        """Yields ("event", Event) as text arrives, then ("done", {"finish": .., "completion_tokens": ..}).
        `force` (forced_call): the opening of the call the reply must make - see prepare()."""
        budget = self.reasoning_budget(sampling) if thinking else None   # #123: opt-in, off by default
        defaults = {**self.sampling_defaults, **self.shared}   # the config's, then the Chat settings shared with apps
        if defaults:                   # the request's own fields win (explicit 0 stays greedy)
            req_values = {k: v for k, v in (sampling or {}).items() if v is not None}
            sampling = {**defaults, **req_values}
        parser = OutputParser(thinking=thinking, tools=tools, stream_tools=True,
                              recover=self.tool_call_recovery)
        detok, n, finish = Detokenizer(self.tok), 0, "length"
        run_tok, run_len, repeated = None, 0, False     # #606: the current run of one repeated token
        thinking_n = 0                                  # tokens written while thinking (Responses' reasoning_tokens)
        stop_list = stop_strings(sampling)
        stops = StopMatcher(stop_list) if stop_list else None

        def cut(evs):
            """The events with the answer's text cut at a stop string (OpenAI stop, Anthropic stop_sequences)."""
            if stops is None:
                return evs
            out = []
            for ev in evs:
                if stops.hit is not None:
                    break                               # nothing after the stop string is sent
                if ev.kind == "content":
                    text = stops.push(ev.text or "")
                    if text:
                        out.append(Event("content", text))
                    continue
                held = stops.flush()                    # a stop string does not run across a tool call or thinking
                if held:
                    out.append(Event("content", held))
                out.append(ev)
            return out
        opening = []                                    # without thinking the prompt ends with the forced opening:
        if force and not thinking:                      # the parser reads it as if the model had written it
            opening, force = parser.feed(force), None
        tail = ""                                       # the last characters written (the newlines before a call)
        timings, before = None, None                    # this request's timings; the engine's `last` before it
        recovery_count, reasoning_text, repeat_coverage = 0, "", 0.0
        looped, next_loop_check = False, LOOP_CHECK_EVERY       # #728: reasoning that repeats whole passages
        raw_ids = []                                    # every generated id (HYPERCORE_DEBUG: dump raw model text)
        emb, self.embeddings.path = getattr(self.embeddings, "path", None), None   # this run's to delete now
        # Identity token: only a DONE line replaces engine.last, so a request that died, errored or was
        # disconnected must not have the PREVIOUS request's decode figures recorded as its own.
        engine_last0 = getattr(self.engine, "last", None)
        trace = getattr(self.request_trace, "record", None)
        waiting = time.perf_counter()
        # #465: with "parallel" (the engine's batch slots) requests run at once: each keeps its own status and rate
        # window (self.status says busy while any runs); one at a time they are self.status / self.rate, as before
        par = bool(getattr(self.engine, "batch", 0))
        st = {} if par else self.status
        rate = collections.deque(maxlen=32) if par else self.rate
        with self.status_lock:
            self.status["queued"] += 1
        try:
            # --batch: the engine runs several requests at once (HyperCoreEngine.generate_batched orders them)
            with (contextlib.nullcontext() if getattr(self.engine, "batch", 0) else self.fifo):
                try:
                    with self.status_lock:
                        if trace is not None:
                            trace["queue_s"] += round(time.perf_counter() - waiting, 3)
                            trace["state"] = "generating"
                        self.status["queued"] -= 1
                    # issue #27: it died in an earlier request (or was unloaded) - start it again instead of failing
                    # (parallel requests do not hold the fifo: one of them starts it, the others wait for that)
                    with (self.fifo if par else contextlib.nullcontext()):
                        self.ensure_loaded()
                    with self.status_lock:
                        st.update(busy=True, phase="reading the prompt", prompt_tokens=len(ids),
                                  generated=0, started=time.time(), first_token=None, tool=None, tail="",
                                  max_tokens=max_new, reasoning_recoveries=0)
                        if par:
                            self.live_reqs[id(st)] = (st, rate)
                            self.status.update(st)
                        self.last_request_at = time.time()
                        rate.clear()                    # the previous request's samples must not leak into this one
                    before = getattr(self.engine, "last", None)
                    last_print = time.time()
                    prompt, thought = ids, 0            # thought: the reasoning tokens so far (the budget's count)
                    for ev in opening:
                        yield "event", ev
                    while True:
                        gen = self.engine.generate(prompt, max_new - n, sampling, cancel, embeddings=emb) if emb \
                            else self.engine.generate(prompt, max_new - n, sampling, cancel)
                        recover_prompt = None
                        seg, wrap, leaving = [], False, False   # this pass's tokens; the budget is reached; closed
                        opens = False                   # the thinking is over: write the forced call's opening
                        try:
                            for t in gen:
                                if t is None:               # heartbeat while the engine is quiet
                                    last_print = self._progress(last_print, st=st)
                                    yield "ping", None
                                    continue
                                n += 1
                                if trace is not None and trace["first_token_s"] is None:
                                    with self.status_lock:
                                        trace["first_token_s"] = round(time.perf_counter() - trace["_clock"], 3)
                                if t in self.stop_ids:
                                    finish = "stop"
                                    raw_ids.append(t)
                                    break
                                raw_ids.append(t)
                                seg.append(t)
                                thinking_n += parser.state in ("reasoning", "rcall")
                                run_len = run_len + 1 if t == run_tok else 1
                                run_tok = t
                                if self.repeat_stop_tokens and run_len >= self.repeat_stop_tokens:
                                    repeated = True     # #606: a degenerate output, not an answer: end it here
                                    break
                                piece = detok.push(t)
                                tail = (tail + piece)[-2:]
                                evs = cut(parser.feed(piece))
                                self._note(n, evs, st, rate)
                                last_print = self._progress(last_print, st=st)
                                for ev in evs:
                                    if self.reasoning_loop_recovery and ev.kind == "reasoning":
                                        reasoning_text += ev.text or ""
                                    yield "event", ev
                                if stops is not None and stops.hit is not None:
                                    finish = "stop"         # gen.close() below STOPs the engine, as for a stop token
                                    break
                                if (self.reasoning_loop_recovery and not recovery_count and parser.state == "reasoning"
                                        and n >= next_loop_check and not parser.buf and not parser.pending
                                        and not detok.pending()):
                                    next_loop_check = n + LOOP_CHECK_EVERY
                                    repeat_coverage = reasoning_repeat_coverage(reasoning_text)
                                    if repeat_coverage >= LOOP_COVERAGE:
                                        if self.reasoning_loop_recovery == "stop":
                                            looped = True        # #728: end the reply here, as for a repeated token
                                            break
                                        if not emb:              # "recover": not for a request with pictures
                                            recover_prompt = focused_recovery_prompt(self.tok, ids, raw_ids)
                                            if recover_prompt is not None:
                                                break
                                if budget and parser.state == "reasoning":
                                    thought += 1
                                    # at a clean point: no tag held back, no character split across tokens
                                    if thought >= budget and not parser.buf and not detok.pending():
                                        wrap = True
                                        break
                                if force:
                                    if any(ev.kind in ("tool_start", "tool_call") for ev in evs):
                                        force = None    # the model is writing a call of its own
                                    elif parser.state == "content" and not parser.buf and not detok.pending():
                                        opens = True
                                        break
                        except EngineDied as e:
                            finish = "error"
                            self._say_died(e)
                            raise
                        except ValueError as e:             # the engine's ERR line (it may have ended after it)
                            finish = "error"
                            print(f"[hypercore] the engine reported an error: {e}", flush=True)
                            raise
                        except GeneratorExit:               # the client went away: an engine that never acknowledges
                            leaving = True                  # the STOP below is ended, but no error replaces this
                            raise
                        finally:
                            try:
                                gen.close()             # STOP+drain to THIS request's DONE while still holding the
                                #                         fifo, so a stop-token break can't leave the shared engine
                                #                         queue mid-drain for the next request to read as its own DONE
                            except EngineSilent as e:   # #481: the STOP was never acknowledged: the engine is ended
                                finish = "error"
                                self._say_died(e)
                                if not leaving and not cancel.is_set():
                                    raise
                        if recover_prompt is not None and not cancel.is_set() and n < max_new:
                            recovery_count += 1
                            # Only the two settings that keep the same words coming are raised (temperature to at
                            # least 1.0, presence penalty to at least 1.5): a client's top_p and top_k are never
                            # touched, and its own request dict is copied, not changed.
                            want = (max(1.0, float((sampling or {}).get("temperature") or 0)),
                                    max(1.5, float((sampling or {}).get("presence_penalty") or 0)))
                            if want != tuple(float((sampling or {}).get(k) or 0)
                                             for k in ("temperature", "presence_penalty")):
                                sampling = {**(sampling or {}), "temperature": want[0], "presence_penalty": want[1]}
                            prompt = recover_prompt
                            with self.status_lock:
                                st["reasoning_recoveries"] = recovery_count
                            if trace is not None:
                                trace["reasoning_recoveries"] = recovery_count
                                trace["reasoning_repeat_coverage"] = round(repeat_coverage, 3)
                            print(f"[hypercore] repeated reasoning detected at {n} tokens "
                                  f"(coverage={repeat_coverage:.3f}); resuming the same output with the low-effort "
                                  "instruction (reasoning_loop_recovery)", flush=True)
                            continue
                        if not (wrap or opens) or cancel.is_set():
                            break
                        # #123: the thinking reached reasoning_budget_tokens.  Close it the way the model would (a
                        # short wrap-up and </think>) and let it answer: the next pass's prompt is this one plus what
                        # was generated plus the wrap-up, so the engine continues from the prefix it already holds.
                        # A forced call (tool_choice) is opened the same way: after the wrap-up, or where the
                        # thinking ended, after the blank line the template puts before a call.
                        if wrap:
                            budget = None
                            text = REASONING_WRAP_UP + (force or "")
                        else:
                            text = "\n" * (2 - (len(tail) - len(tail.rstrip("\n")))) + force
                        force = None
                        extra = self.tok.encode(text, parse_special=True)
                        if max_new - n - len(extra) < 1:
                            break                       # no room left to answer: "length", as without a budget
                        if wrap:
                            print(f"[hypercore] thinking budget reached ({thought} tokens): wrapping up the thinking",
                                  flush=True)
                        for t in extra:
                            n += 1
                            raw_ids.append(t)
                            thinking_n += parser.state in ("reasoning", "rcall")
                            evs = cut(parser.feed(detok.push(t)))
                            self._note(n, evs, st, rate)
                            for ev in evs:
                                yield "event", ev
                        if stops is not None and stops.hit is not None:
                            finish = "stop"
                            break
                        prompt = prompt + seg + extra
                    if cancel.is_set():
                        finish = "cancel"
                    elif looped:
                        print(f"[hypercore] the thinking repeated the same passages (coverage={repeat_coverage:.3f}) at "
                              f"{n} tokens: ended as \"length\" (reasoning_loop_recovery: \"stop\" in "
                              "hypercore-<model>.json; remove it to turn this off)", flush=True)
                    elif repeated:
                        print(f"[hypercore] the reply repeated one token ({self.tok.decode([run_tok])!r}) "
                              f"{run_len} times in a row: ended as \"length\" (repeat_stop_tokens in "
                              "hypercore-<model>.json; 0 turns this off). If a new request with a short prompt does the "
                              "same, restart the server and report it (#606)", flush=True)
                except GeneratorExit:                   # the client disconnected mid-stream
                    finish = "disconnect"
                    raise
                finally:
                    # #266: settle this request's status, history and totals while still holding the fifo: once
                    # it is released the next request sets its own status, which this must not record or clear
                    with self.status_lock:
                        if st.get("busy"):
                            # only this request's DONE counts: same object means no DONE arrived (death, error,
                            # disconnect)
                            last = dict(getattr(self.engine, "last", {}) or {}) \
                                if getattr(self.engine, "last", None) is not engine_last0 else {}
                            started = st.get("started", time.time())
                            cvec = (getattr(self.engine, "info", {}) or {}).get("cvec", 0)
                            loaded = str(cvec) not in ("0", "", "None")
                            hit_rate = round(last["hits"] / last["lookups"], 3) if last.get("lookups") else None
                            # #588: the share the GPU read over PCIe (--pcie-frac) or another GPU computed - not in
                            # the hit rate's lookups, which is the VRAM share of the rest (None: an older engine)
                            routed = (last.get("lookups") or 0) + (last.get("offloaded") or 0)
                            pcie_share = round(last["offloaded"] / routed, 3) \
                                if last.get("offloaded") is not None and routed else None
                            seen = prompt_tokens_seen(len(ids), last)   # #471: < len(ids) when cancelled mid-read
                            self.history.append({
                                "projection": (sampling or {}).get("experimental_speed_projection") is not False
                                if loaded else None,
                                "time": started, "duration_s": round(time.time() - started, 1), "finish": finish,
                                "prompt_tokens": seen, "reused": last.get("reused"), "output_tokens": n,
                                # the request's whole prompt, and the tokens read of it (None: an older engine)
                                "prompt_total": len(ids), "prompt_read": last.get("prompt_read"),
                                "prefill_tok_s_mean": last.get("prefill_tok_s_mean"),
                                "reasoning_recoveries": recovery_count,
                                "reasoning_repeat_coverage": round(repeat_coverage, 3),
                                "engine_generated": last.get("generated"),
                                "prompt_ms": last.get("prompt_ms"), "decode_ms": last.get("decode_ms"),
                                "decode_tok_s": round(last["generated"] / (last["decode_ms"] / 1000), 1)
                                if n and last.get("generated") and last.get("decode_ms") else None,
                                "hit_rate": hit_rate, "pcie_share": pcie_share, "ram_blobs": last.get("ram_blobs"),
                                "file_blobs": last.get("file_blobs"), "file_mb": last.get("file_mb"),
                                # #457: the speculative drafts from the DONE line (None: the engine did not say)
                                "drafts_offered": last.get("drafts_offered"),
                                "drafts_accepted": last.get("drafts_accepted")})
                            t = self.totals
                            t["requests"] += 1
                            t["prompt_tokens"] += seen
                            t["reused"] += last.get("reused") or 0
                            t["output_tokens"] += n
                            t["prompt_ms"] += last.get("prompt_ms") or 0.0
                            t["decode_ms"] += last.get("decode_ms") or 0.0
                            t["drafts_offered"] += last.get("drafts_offered") or 0
                            t["drafts_accepted"] += last.get("drafts_accepted") or 0
                            fresh = getattr(self.engine, "last", None)
                            if fresh is not None and fresh is not before:      # the engine's clock for THIS request
                                timings = request_timings(seen, n, last)
                                self.last_timings = dict(timings, at=int(time.time())) if timings else None
                            self.last_request_at = time.time()
                            now = time.time()
                            el = now - st.get("started", now)
                            ft = st.get("first_token")
                            rate = n / max(1e-6, now - ft) if ft else 0.0
                            hit_msg = f", expert cache {hit_rate*100:.1f}% hit" if hit_rate is not None else ""
                            if hit_msg and pcie_share:
                                hit_msg += f" (+{pcie_share*100:.1f}% of the routed experts over PCIe)"
                            print(f"[hypercore] done: {n} tokens in {el:.0f} s ({rate:.1f} tok/s) "
                                  f"({finish}, cancel={cancel.is_set()}){hit_msg}", flush=True)
                            if finish == "length" and parser.state in ("reasoning", "rcall"):   # #530
                                print("[hypercore] the reply reached max tokens while still thinking, so it has no "
                                      "answer: a thinking budget (reasoning_budget_tokens, in the request or in "
                                      "hypercore-<model>.json for every request) leaves room to answer", flush=True)
                            if os.environ.get("HYPERCORE_DEBUG") and raw_ids:
                                print(f"[hypercore] raw: {self.tok.decode(raw_ids)!r}", flush=True)
                        st["busy"] = False
                        st.pop("tail", None)                     # #212: the answer's end is not kept once it is done
                        st.pop("tool", None)
                        if par:
                            self.live_reqs.pop(id(st), None)
                            if self.live_reqs:              # the newest request still running
                                self.status.update(list(self.live_reqs.values())[-1][0])
                            else:
                                self.status.update(busy=False)
                                self.status.pop("tail", None)
                                self.status.pop("tool", None)
        finally:
            if emb:
                Path(emb).unlink(missing_ok=True)
        for ev in cut(parser.finish(finish)):
            yield "event", ev
        if parser.rescued or parser.refused:
            self.totals["tool_calls_from_reasoning"] = self.totals.get("tool_calls_from_reasoning", 0) + parser.rescued
            print(f"[hypercore] tool calls inside the thinking: {parser.rescued} read as calls, {parser.refused} kept as "
                  f"reasoning (quoted, or the turn did not end on a stop)", flush=True)
        if stops is not None and stops.hit is None and stops.held:
            yield "event", Event("content", stops.flush())     # the held tail was not a stop string after all
        done = {"finish": finish, "completion_tokens": n, "reused": (timings or {}).get("cache_n", 0),
                "timings": timings, "reasoning_tokens": thinking_n,
                "reasoning_recoveries": recovery_count}
        if stops is not None and stops.hit is not None:
            done["stop_sequence"] = stops.hit
        yield "done", done


def prompt_tokens_seen(prompt_tokens: int, last: dict) -> int:
    """#471: the prompt tokens a request got through - all of them, unless the engine's DONE line says a cancel stopped
    its prompt read part-way (then the reused ones plus those read).  /metrics' history and totals count these, so a
    cancelled read is neither recorded as the whole prompt nor given a rate from tokens it never read.  An engine
    before 0.1.36 does not say (no `prompt_read`): the whole prompt, as before."""
    read = last.get("prompt_read")
    if read is None or last.get("finish") != "cancel":
        return prompt_tokens
    return min(prompt_tokens, int(last.get("reused") or 0) + int(read))


def request_timings(prompt_tokens: int, generated: int, last: dict) -> dict | None:
    """One request's `timings` in llama.cpp's names (what its clients show as speed), from the engine's own clock
    (HyperCoreEngine.last): prompt_n is what was read, cache_n what the conversation cache already held.  None when the
    engine keeps no clock (MockEngine)."""
    if last.get("prompt_ms") is None:
        return None
    cache_n = int(last.get("reused") or 0)
    prompt_n, prompt_ms, decode_ms = max(0, prompt_tokens - cache_n), float(last["prompt_ms"]), float(last.get("decode_ms") or 0)
    decoded = int(last.get("generated") or generated)          # the engine's count gives its rate, as /metrics does
    return {"cache_n": cache_n, "prompt_n": prompt_n, "prompt_ms": round(prompt_ms, 1),
            "prompt_per_token_ms": round(prompt_ms / prompt_n, 3) if prompt_n else None,
            "prompt_per_second": round(prompt_n / (prompt_ms / 1000), 1) if prompt_n and prompt_ms > 0 else None,
            "predicted_n": generated, "predicted_ms": round(decode_ms, 1),
            "predicted_per_token_ms": round(decode_ms / decoded, 3) if decoded else None,
            "predicted_per_second": round(decoded / (decode_ms / 1000), 1) if decoded and decode_ms > 0 else None,
            # the speculative drafts, as llama.cpp names them (from PR #83, @mikicvi): only when the engine reported them
            **({"draft_n": int(last["drafts_offered"]), "draft_n_accepted": int(last["drafts_accepted"])}
               if last.get("drafts_offered") is not None else {})}


def _debug_req(api, req, messages, tools, max_new, thinking, prompt_tokens):
    """One compact line per request while diagnosing blank/empty turns. Set HYPERCORE_DEBUG=1 to enable."""
    if not os.environ.get("HYPERCORE_DEBUG"):
        return
    last = messages[-1] if messages else {}
    body = last.get("content")
    if isinstance(body, list):
        body = " ".join(p.get("text", "") for p in body if isinstance(p, dict))
    preview = (str(body or "")[:80]).replace("\n", " ")
    print(f"[hypercore] req {api}: msgs={len(messages)} tools={len(tools or [])} "
          f"max_tokens_raw={req.get('max_tokens')!r}/{req.get('max_completion_tokens')!r} "
          f"max_new={max_new} thinking={thinking} stream={bool(req.get('stream'))} "
          f"prompt_tokens={prompt_tokens} last={last.get('role')!r}:{preview!r}", flush=True)


# ------------------------------------------------------------------------------------------------ the turn tail
# Opt-in ("turn_tail_check": true): when a turn would end, the server appends one short turn asking the
# model whether every task in the request is done. A bare confirmation ends the turn; anything else - a
# "no", or the model ignoring the question and carrying on - is the turn still open, so the work
# continues and the question is asked again, up to "turn_tail_checks" times.
TAIL_CHECK_PROMPT = ('Before this turn ends, check your work against the request: did you complete every '
                     'task it asked for? If everything is done, reply with the single word "done" and '
                     'nothing else. If anything is still outstanding, do that work now.')
TAIL_CHECK_MAX = 3              # how many times one turn may be asked, at most
TAIL_CONFIRM_CHARS = 120        # a confirmation is a word or two; longer text is the model working on
TAIL_OUTSTANDING = ("not yet", "still need", "still have", "haven't", "have not", "incomplete",
                    "unfinished", "remaining", "i need", "i'll", "i will", "are outstanding",
                    "is outstanding", "pending", "not done", "next step", "continue working", "to do")
TAIL_CONFIRM = ("done", "complete", "completed", "finished", "everything is done", "all tasks done",
                "all tasks complete", "no outstanding", "nothing outstanding", "nothing left",
                "nothing more", "yes")


def tail_normalize(text: str) -> str:
    """A reply as words: lower case, punctuation and runs of space gone."""
    return " ".join((text or "").lower().replace(".", " ").replace(",", " ").replace("!", " ").split())


def tail_is_done(text: str) -> bool:
    """A reply to the turn-tail check: is the work finished?

    A turn is still open when the reply says so (an outstanding-work word), when it is longer than a
    word or two (a model carrying on is not confirming), or when there is no reply at all - ignoring
    the question is not an answer.
    """
    t = tail_normalize(text)
    if not t or len(t) > TAIL_CONFIRM_CHARS:
        return False
    if any(w in t for w in TAIL_OUTSTANDING):
        return False
    return any(w in t for w in TAIL_CONFIRM)


class TailHold:
    """A check round's reply, held until it is known whether the work is finished.

    A confirmation is never shown to the client; everything else is the model doing its remaining work
    and is streamed as soon as it is clear. The same shape as the stop-string matcher in Service.run:
    text is emitted only when it cannot still be a confirmation.
    """

    def __init__(self):
        self.held, self.open = "", True

    def push(self, text: str) -> str:
        if not self.open:
            return text
        self.held += text
        t = tail_normalize(self.held)
        if not t:
            return ""                       # only whitespace so far: nothing to say yet
        if len(t) > TAIL_CONFIRM_CHARS or any(w in t for w in TAIL_OUTSTANDING):
            out, self.held, self.open = self.held, "", False
            return out
        return ""

    def flush(self) -> str:
        out, self.held = self.held, ""
        return out


# ------------------------------------------------------------------------------------------------ MCP tool loop
def _compact_event(plan: dict) -> dict:
    """What the web app stores so the next send continues from the handoff, not the full transcript."""
    return {"event": "compacted", "summary": plan["summary"], "prefix": plan["prefix"],
            "omit_tool_ids": plan["omit_tool_ids"]}


def _recover_overflow(svc: Service, hub, messages, tools, kw, max_new, force):
    """A hypercore_mcp prompt that does not fit is folded into a handoff and prepared again.

    The model cannot summarize a prompt the engine has already refused, so this pass is
    deterministic. None when folding does not produce a prompt that fits; the caller
    re-raises the original refusal. Requests without hypercore_mcp never reach here.
    """
    retain = int(hub.settings.get("compact_retain_bytes") or 80_000)
    summary = deterministic_summary(messages)
    last = None
    for keep_round in (True, False):
        plan = plan_compaction(messages, summary, retain, keep_round=keep_round)
        if plan is None:
            continue
        try:
            packed = svc.prepare(plan["messages"], tools, kw, max_new, force=force)
        except ValueError as error:
            last = error
            continue
        return plan["messages"], packed, _compact_event(plan)
    shrunk = shrink_messages(messages, retain)
    if shrunk is not None:
        try:
            return shrunk, svc.prepare(shrunk, tools, kw, max_new, force=force), None
        except ValueError as error:
            last = error
    if last is not None:
        raise last
    return None


def _fit_prepared(svc: Service, hub, messages, tools, kw, max_req):
    """Prepare a transcript, folding it into a handoff when the engine would refuse it.

    Yields ("mcp", event) when a fold happens, then ("ready", (messages, ids, thinking, max_new)).
    """
    try:
        packed = svc.prepare(messages, tools, kw, max_req)
    except ValueError as error:
        if "never truncated" not in str(error):
            raise
        recovered = _recover_overflow(svc, hub, messages, tools, kw, max_req, None)
        if recovered is None:
            raise
        messages, packed, event = recovered
        yield "mcp", event
    yield "ready", (messages, packed)


def run_with_mcp(svc: Service, hub, messages, tools, kw, ids, thinking, max_new, max_req, sampling, cancel,
                 mcp_names, seed_compact=None):
    """Service.run with the MCP tools executed here: the model writes a call to an MCP tool, the server runs it, adds
    the call and its result to the conversation and lets the model continue - up to `max_rounds` times, and up to
    `max_tool_calls` calls in total (0: no call cap).  Yields what
    Service.run yields (text, thinking, the request's own tool calls) plus ("mcp", {...}) for the tool activity, and
    one ("done", ...) at the very end with the output tokens of every round.

    `mcp_names`: the MCP tools this request offered; any other call is one of the request's own tools and ends the
    turn as always (the client answers it).  MCP calls written in the same answer are then not run (their results
    could not reach the model before the client's)."""
    max_rounds = int(hub.settings["max_rounds"])
    max_calls = int(hub.settings.get("max_tool_calls") or 0)   # 0: no separate cap, the round cap is the cap
    total, rounds, calls_run, done = 0, 0, 0, None
    messages = list(messages)
    if seed_compact:
        # The prompt did not fit at all, so it was folded before this loop started.
        yield "mcp", seed_compact
    while True:
        ctx = svc.engine.max_context
        passes = 0
        while passes < 2 and hub.needs_compacting(len(ids), ctx) and not cancel.is_set():
            before = len(ids)
            retain = int(hub.settings.get("compact_retain_bytes") or 80_000)
            summary = None
            # The first pass asks the model, which can read this prompt: it is over the
            # fraction but still inside the window. A second pass means the newest tool
            # round alone is still too big, and another full summary would not fit any
            # better, so the handoff is built from the text.
            if passes == 0:
                yield "mcp", {"event": "compacting"}
                try:
                    prompt = list(messages) + [{"role": "user", "content": COMPACT_PROMPT}]
                    sum_ids, _, room = svc.prepare(prompt, None, {**kw, "enable_thinking": False}, 0)
                    if room >= 256:
                        parts = []
                        for kind, ev in svc.run(sum_ids, False, None, min(1024, room), sampling, cancel):
                            if kind == "ping":
                                yield "ping", None
                            elif kind == "event" and ev.kind == "content" and ev.text:
                                parts.append(ev.text)
                        summary = "".join(parts).strip()
                except EngineStarting:
                    raise
                except Exception as error:  # noqa: BLE001 - a failed summary must not end the turn
                    print(f"[hypercore] compaction summary failed ({error}); using the transcript handoff",
                          flush=True)
                    summary = None
            if cancel.is_set():
                break
            if not summary:
                summary = deterministic_summary(messages)
            plan = plan_compaction(messages, summary, retain, keep_round=passes == 0)
            if plan is None:
                break
            messages = plan["messages"]
            yield "mcp", _compact_event(plan)
            for kind, payload in _fit_prepared(svc, hub, messages, tools, kw, max_req):
                if kind == "ready":
                    messages, (ids, thinking, max_new) = payload
                else:
                    yield kind, payload
            print(f"[hypercore] compacted the transcript, prompt {before:,} -> {len(ids):,} tokens "
                  f"of the {ctx:,}-token context", flush=True)
            if len(ids) >= before:
                break
            passes += 1
        text, reasoning, calls, own_calls = [], [], [], 0
        for kind, x in svc.run(ids, thinking, tools, max_new, sampling, cancel):
            if kind == "done":
                done = x
                continue
            if kind == "event":
                ev: Event = x
                if ev.call is not None and ev.call.name in mcp_names:
                    if ev.kind == "tool_start":          # the model has started writing a call: say so at once
                        yield "mcp", {"event": "start", "id": ev.call.id, "name": ev.call.name}
                    elif ev.kind == "tool_call":
                        calls.append(ev.call)
                    continue                             # its argument pieces are not streamed to the client
                if ev.kind == "tool_call":
                    own_calls += 1
                elif ev.kind == "content":
                    text.append(ev.text)
                elif ev.kind == "reasoning":
                    reasoning.append(ev.text)
            yield kind, x
        total += done["completion_tokens"]
        run_them = calls and not own_calls and done["finish"] == "stop" and not cancel.is_set()
        if run_them and rounds >= max_rounds:
            yield "mcp", {"event": "limit", "max_rounds": max_rounds, "max_tool_calls": max_calls,
                          "rounds": rounds, "calls": calls_run}
            run_them = False
        if not run_them:
            for c in calls:                              # announced, never run: close them in the client's view
                yield "mcp", {"event": "result", "id": c.id, "ok": False, "skipped": True, "text": "not run",
                              "chars": 0, "truncated": False, "ms": 0}
            break
        rounds += 1
        results, ran, cut = [], [], False
        for c in calls:
            # The call cap: one round may announce more calls than the request is allowed to run. The
            # ones that fit are run, the rest are closed in the client's view and never reach the model.
            if max_calls and calls_run >= max_calls:
                cut = True
                yield "mcp", {"event": "result", "id": c.id, "ok": False, "skipped": True,
                              "text": f"not run: this request reached its {max_calls} tool call limit",
                              "chars": 0, "truncated": False, "ms": 0}
                continue
            ran.append(c)
            calls_run += 1
            s, tool = hub.routes().get(c.name, (None, c.name))
            yield "mcp", {"event": "call", "id": c.id, "name": c.name, "server": s.name if s else None,
                          "tool": tool, "arguments": c.arguments, "round": rounds}
            # The call runs on a thread while this generator keeps yielding heartbeats: they reach the client as
            # keep-alives, which is how a client that went away (the web app's Stop) is noticed during a slow tool.
            box = {}

            def work(c=c, box=box):
                try:
                    box["r"] = hub.call(c.name, c.arguments, cancel)
                except McpCancelled:
                    box["cancelled"] = True
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            try:
                while worker.is_alive():
                    worker.join(1.0)
                    if worker.is_alive():
                        yield "ping", None
            except GeneratorExit:
                cancel.set()                             # the client is gone: stop the tool too
                raise
            if "r" not in box:
                break
            r = box["r"]
            print(f"[hypercore] tool {c.name}: {'ok' if r['ok'] else 'error'}, {r['chars']:,} characters in "
                  f"{r['ms'] / 1000:.1f} s{' (truncated for the model)' if r['truncated'] else ''}", flush=True)
            results.append(r["text"])
            yield "mcp", {"event": "result", "id": c.id, **{k: r[k] for k in ("ok", "text", "chars", "truncated", "ms")}}
        if cut:
            yield "mcp", {"event": "limit", "max_rounds": max_rounds, "max_tool_calls": max_calls,
                          "rounds": rounds, "calls": calls_run}
        if cancel.is_set() or len(results) < len(ran):
            done = {**done, "finish": "cancel"}
            break
        messages.append({"role": "assistant", "content": "".join(text).strip(),
                         **({"reasoning_content": "".join(reasoning).strip()} if reasoning else {}),
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.name, "arguments": c.arguments}} for c in ran]})
        messages += [{"role": "tool", "content": r} for r in results]
        if cut:
            # The call budget is spent. The results that did run are in the transcript; no further
            # round can use them, so the turn ends here instead of looping on calls that will not run.
            break
        for kind, payload in _fit_prepared(svc, hub, messages, tools, kw, max_req):
            if kind == "ready":
                messages, (ids, thinking, max_new) = payload
            else:
                yield kind, payload
        # The condenser: every tool result is re-prefilled on the next round, so the prompt grows with the
        # number of rounds and the size of each result. Earlier results the model has already read are
        # shortened - head and tail kept, the middle a count - once the prompt is actually close to the
        # context, not after a fixed number of rounds. Every result from this round is left
        # whole: those are the ones the model is answering to. Compaction, at the top of the next
        # iteration, folds what is still too large into one handoff.
        if hub.needs_condensing(len(ids), svc.engine.max_context, rounds):
            messages, count, dropped = hub.condense_messages(messages)
            if count:
                print(f"[hypercore] condensed {count} earlier tool result(s), {dropped:,} characters, to keep "
                      f"room in the {svc.engine.max_context:,}-token context", flush=True)
                yield "mcp", {"event": "condensed", "results": count, "chars": dropped}
                for kind, payload in _fit_prepared(svc, hub, messages, tools, kw, max_req):
                    if kind == "ready":
                        messages, (ids, thinking, max_new) = payload
                    else:
                        yield kind, payload
    yield "done", {**done, "completion_tokens": total, "prompt_tokens": len(ids)}


def with_tail_check(svc: Service, gen, messages, tools, kw, max_new, sampling, cancel, restart):
    """Run a turn; when it would end, ask once whether the work is finished, and keep going if it is not.

    `gen`: this turn's events (Service.run's or run_with_mcp's). `restart(messages, ids, thinking,
    max_new)`: the generator for the next round, with the check turn appended. The turn ends on a bare
    confirmation, on the check limit, or when there is nothing to check: a reply cut short (a length
    stop, a cancel), a forced tool call, or no text at all. The check's own prompt is not shown to the
    client, and neither is a bare confirmation; everything else the model writes is streamed as usual,
    and every round's output tokens are counted in the turn's usage.
    """
    checks, tokens = 0, 0
    while True:
        hold, answer, done = TailHold(), [], None
        for kind, x in gen:
            if kind == "done":
                done = x
                continue
            if kind == "event" and getattr(x, "kind", "") == "content":
                raw = x.text or ""
                answer.append(raw)
                out = hold.push(raw)
                if out:
                    yield "event", Event("content", out)
                continue
            yield kind, x
        if done is None:
            return
        tokens += int(done.get("completion_tokens") or 0)
        text = "".join(answer)
        # A bare confirmation is never shown to the client; anything the hold kept back is the
        # model doing its remaining work, and is sent now.
        rest = hold.flush()
        if rest and not tail_is_done(text):
            yield "event", Event("content", rest)
        if tail_is_done(text):
            yield "mcp", {"event": "tail_check", "result": "complete", "checks": checks + 1}
            yield "done", {**done, "completion_tokens": tokens}
            return
        if not (svc.turn_tail_check and checks < svc.turn_tail_checks and done.get("finish") == "stop"
                and text.strip() and not cancel.is_set()):
            yield "done", {**done, "completion_tokens": tokens}
            return
        checks += 1
        nxt = messages + [{"role": "assistant", "content": text},
                         {"role": "user", "content": svc.turn_tail_prompt}]
        try:
            ids, thinking, room = svc.prepare(nxt, tools, kw, max_new)
        except (ValueError, EngineDied) as error:
            print(f"[hypercore] the turn-tail check could not be prepared ({error}); the turn ends here",
                  flush=True)
            yield "done", {**done, "completion_tokens": tokens}
            return
        yield "mcp", {"event": "tail_check", "result": "continue", "checks": checks,
                      "max_checks": svc.turn_tail_checks}
        gen = restart(nxt, ids, thinking, room)


# ------------------------------------------------------------------------------------------------ OpenAI
# The engine's last PP line stops short of the prompt: up to `--short-read` tokens (generate.cpp's `short_read`, 64 by
# default) are read through the verify windows instead of the batched path, so the batched path never reports the whole
# prompt.  A PP that close to the end says the prompt is read.  It matters because a prompt shorter than one `--prefill`
# chunk sends exactly one PP, that one: a client would see a bar that appears already full.  Such a line is dropped, and
# the answer's first token ends the progress for the client anyway.
PP_DONE_TAIL = 64


def prompt_progress(svc: Service):
    """The last PP line as llama.cpp's `prompt_progress`, so a client written for llama.cpp reads both servers: the
    same four fields, `time_ms` elapsed since prompt reading started, and `processed` counting from token 0 so that a
    client takes the reused prefix out with `cache` (llama.cpp's rule: the timed progress is
    `(processed-cache)/(total-cache)`).  None when there is nothing to say: no PP yet, or the line that says the prompt
    is read.  An engine too old to print RESUME gives `cache` 0, which only makes a reused prefix count as work."""
    engine = svc.engine
    if getattr(engine, "batch", 0):      # --batch: the engine's progress is one global line, not this request's
        return None
    progress = getattr(engine, "progress", None)
    if not progress:
        return None
    read, total = progress
    if total <= 0 or total - read <= PP_DONE_TAIL:
        return None
    return {"total": int(total), "cache": int(getattr(engine, "reused", 0) or 0), "processed": int(read),
            "time_ms": int(getattr(engine, "progress_ms", 0) or 0)}


def openai_chunks(svc: Service, req: dict, ids, thinking, tools, max_new, cancel, run=None, force=None):
    """`run`: the events to send instead of Service.run's (run_with_mcp); its ("mcp", {...}) items become chunks with
    an empty delta and a `hypercore_mcp` field, which only the web app reads.  `force`: see Service.run."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    model = svc.model_for(req)
    # llama.cpp's flag, and its default: a client that wants prompt progress asks for it, and nothing changes for one
    # that does not.  HyperCore already sends a keep-alive comment on every PP line, so this only fills that line in.
    want_progress = bool(req.get("return_progress"))

    def chunk(delta, finish=None):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    calls = 0
    streamed = {}                                  # tool call id -> index, for calls sent piece by piece
    finished = set()                               # ... and the ones whose final tool_call came (#211)
    for kind, x in run if run is not None else svc.run(ids, thinking, tools, max_new, req, cancel, force=force):
        if kind == "ping":
            # a PP line: the prompt is being read and there is nothing to say yet.  It used to be only the SSE comment;
            # now it carries the progress when the client asked, and stays the comment when there is nothing to report.
            pp = prompt_progress(svc) if want_progress else None
            yield None if pp is None else {**chunk({}), "prompt_progress": pp}
        elif kind == "mcp":
            c = chunk({})
            c["hypercore_mcp"] = x
            yield c
        elif kind == "event":
            ev: Event = x
            if ev.kind == "reasoning" and ev.text:
                yield chunk({"reasoning_content": ev.text})
            elif ev.kind == "content" and ev.text:
                yield chunk({"content": ev.text})
            elif ev.kind == "tool_start":
                streamed[ev.call.id] = calls
                calls += 1
                yield chunk({"tool_calls": [{"index": streamed[ev.call.id], "id": ev.call.id, "type": "function",
                                             "function": {"name": ev.call.name, "arguments": ""}}]})
            elif ev.kind == "tool_args":
                yield chunk({"tool_calls": [{"index": streamed[ev.call.id], "function": {"arguments": ev.text}}]})
            elif ev.kind == "tool_call" and ev.call.id in streamed:
                finished.add(ev.call.id)
            elif ev.kind == "tool_call":
                yield chunk({"tool_calls": [{"index": calls, "id": ev.call.id, "type": "function",
                                             "function": {"name": ev.call.name,
                                                          "arguments": json.dumps(ev.call.arguments, ensure_ascii=False)}}]})
                calls += 1
        else:
            # a streamed call without its final tool_call is one the output ended inside: not "tool_calls" (#211)
            whole = calls and streamed.keys() <= finished
            finish = "tool_calls" if whole and x["finish"] == "stop" else {"cancel": "stop"}.get(x["finish"], x["finish"])
            last = chunk({}, finish)
            pt = x.get("prompt_tokens", len(ids))     # after MCP rounds: the last round's prompt
            last["usage"] = {"prompt_tokens": pt, "completion_tokens": x["completion_tokens"],
                             "total_tokens": pt + x["completion_tokens"],
                             # the part of the prompt the conversation cache already held (OpenAI's field)
                             "prompt_tokens_details": {"cached_tokens": x.get("reused") or 0}}
            if x.get("timings"):
                last["timings"] = x["timings"]          # llama.cpp's field: the speed its clients show
            yield last


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def openai_collect(chunks) -> dict:
    content, reasoning, by_index, last, mcp = [], [], {}, None, []
    for c in chunks:
        if c is None:                              # a heartbeat
            continue
        if c.get("hypercore_mcp"):
            mcp.append(c["hypercore_mcp"])
        d = c["choices"][0]["delta"]
        content.append(d.get("content") or "")
        reasoning.append(d.get("reasoning_content") or "")
        for tc in d.get("tool_calls") or []:       # streamed calls arrive in pieces: merge them by index
            cur = by_index.setdefault(tc.get("index", len(by_index)), {"id": None, "type": "function",
                                                                        "function": {"name": "", "arguments": ""}})
            cur["id"] = tc.get("id") or cur["id"]
            fn = tc.get("function") or {}
            cur["function"]["name"] += fn.get("name") or ""
            cur["function"]["arguments"] += fn.get("arguments") or ""
        last = c
    calls = [by_index[i] for i in sorted(by_index)]
    if last["choices"][0]["finish_reason"] != "tool_calls":
        calls = [c for c in calls if _is_json(c["function"]["arguments"])]   # a call the output ended inside (#211)
    msg = {"role": "assistant", "content": "".join(content) or None}
    if "".join(reasoning):
        msg["reasoning_content"] = "".join(reasoning)
    if calls:
        msg["tool_calls"] = calls
    if mcp:
        msg["hypercore_mcp"] = mcp
    out = {"id": last["id"], "object": "chat.completion", "created": last["created"], "model": last["model"],
           "choices": [{"index": 0, "message": msg, "finish_reason": last["choices"][0]["finish_reason"]}],
           "usage": last["usage"]}
    if last.get("timings"):
        out["timings"] = last["timings"]
    return out


def structured_chunks(chunks, validator):
    """Buffer structured streams so a client never receives unvalidated content."""
    buffered = []
    heartbeat = time.monotonic()
    try:
        for chunk in chunks:
            progress = chunk is not None and "prompt_progress" in chunk
            if chunk is not None and not progress:
                buffered.append(chunk)      # a progress chunk carries no content: there is nothing in it to validate
            if chunk is None or progress or time.monotonic() - heartbeat >= 1:
                heartbeat = time.monotonic()
                yield chunk if progress else None
        result = openai_collect(buffered)
        choice = result["choices"][0]
        content = validated_json(choice["message"]["content"], validator, choice["finish_reason"])
        yield buffered[0]
        delta = {"content": content}
        if choice["message"].get("reasoning_content"):
            delta["reasoning_content"] = choice["message"]["reasoning_content"]
        yield {**buffered[0], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        yield buffered[-1]
    finally:
        chunks.close()


# ------------------------------------------------------------------------------------------------ Anthropic
def anthropic_events(svc: Service, req: dict, ids, thinking, tools, max_new, cancel, force=None, run=None):
    mid = "msg_" + uuid.uuid4().hex[:24]
    yield "message_start", {"type": "message_start", "message": {
        "id": mid, "type": "message", "role": "assistant", "model": svc.model_for(req), "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": len(ids), "output_tokens": 0}}}
    index, open_kind, used_tool = -1, None, False

    def close():
        return ("content_block_stop", {"type": "content_block_stop", "index": index})

    streamed, finished = set(), set()              # calls sent piece by piece; those whose final tool_call came (#211)
    for kind, x in run if run is not None else svc.run(ids, thinking, tools, max_new, req, cancel, force=force):
        if kind == "ping":
            yield None
            continue
        elif kind == "mcp":
            continue
        if kind == "event":
            ev: Event = x
            if ev.kind == "tool_args":
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "input_json_delta", "partial_json": ev.text}}
                continue
            if ev.kind == "tool_call" and ev.call.id in streamed:
                finished.add(ev.call.id)
                continue
            want = {"reasoning": "thinking", "content": "text", "tool_call": "tool_use", "tool_start": "tool_use"}[ev.kind]
            if ev.kind not in ("tool_call", "tool_start") and not ev.text:
                continue
            if ev.kind == "tool_start":
                streamed.add(ev.call.id)
                used_tool = True
            if open_kind != want or want == "tool_use":
                if open_kind is not None:
                    yield close()
                index += 1
                open_kind = want
                block = {"thinking": {"type": "thinking", "thinking": "", "signature": ""},
                         "text": {"type": "text", "text": ""},
                         "tool_use": {"type": "tool_use", "id": ev.call.id if ev.call else "", "name":
                                      ev.call.name if ev.call else "", "input": {}}}[want]
                yield "content_block_start", {"type": "content_block_start", "index": index, "content_block": block}
            if want == "thinking":
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "thinking_delta", "thinking": ev.text}}
            elif want == "text":
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "text_delta", "text": ev.text}}
            elif ev.kind == "tool_start":
                pass                                # its input follows as tool_args pieces
            else:
                used_tool = True
                yield "content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(ev.call.arguments, ensure_ascii=False)}}
        else:
            if open_kind is not None:
                yield close()
            stop = "stop_sequence" if x.get("stop_sequence") is not None else \
                "tool_use" if used_tool and streamed <= finished and x["finish"] == "stop" else \
                {"stop": "end_turn", "length": "max_tokens", "cancel": "end_turn"}[x["finish"]]
            # the final counts, Anthropic's way: input_tokens leaves out what the conversation cache already held,
            # which is cache_read_input_tokens (message_start could only say the whole prompt)
            reused = min(x.get("reused") or 0, len(ids))
            yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": stop,
                                                                       "stop_sequence": x.get("stop_sequence")},
                                    "usage": {"input_tokens": len(ids) - reused, "cache_read_input_tokens": reused,
                                              "output_tokens": x["completion_tokens"]}}
            yield "message_stop", {"type": "message_stop"}


def anthropic_collect(events) -> dict:
    msg, blocks = None, []
    for item in events:
        if item is None:                           # a heartbeat
            continue
        name, e = item
        if name == "message_start":
            msg = e["message"]
        elif name == "content_block_start":
            blocks.append(dict(e["content_block"]))
        elif name == "content_block_delta":
            d, b = e["delta"], blocks[-1]
            if d["type"] == "text_delta":
                b["text"] += d["text"]
            elif d["type"] == "thinking_delta":
                b["thinking"] += d["thinking"]
            else:                                  # input_json_delta pieces: parsed when complete
                b["_json"] = b.get("_json", "") + d["partial_json"]
        elif name == "content_block_stop" and blocks and "_json" in blocks[-1]:
            b = blocks[-1]
            try:
                b["input"] = json.loads(b.pop("_json") or "{}")
            except ValueError:                     # a call the output ended inside (#211): it has no input to give
                blocks.pop()
        elif name == "message_delta":
            msg["stop_reason"] = e["delta"]["stop_reason"]
            msg["stop_sequence"] = e["delta"].get("stop_sequence")
            msg["usage"].update(e["usage"])
    msg["content"] = blocks
    return msg


# ------------------------------------------------------------------------------------------------ HTTP
def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"                       # SSE ends by closing the connection

        record = None                                       # #332: this request's monitor record, if kept
        watch_done = None                                   # #430 #431: stops this request's disconnect watcher
        body_read = False                                   # whether a handler took this request's body
        DRAIN_SECONDS = 5                                   # the longest an unread body is read and dropped

        def log_message(self, fmt, *args):
            pass

        def handle_one_request(self):
            super().handle_one_request()
            self._drain_body()

        def _body(self) -> bytes:
            self.body_read = True
            return self.rfile.read(int(self.headers.get("Content-Length", 0)))

        def _drain_body(self):
            """An answer sent before the body was read (a 401, a 403, /load, a method with no handler) must not close
            the connection on unread bytes: the close then sends a reset, and a client that sends its body after the
            headers (http.client, urllib, requests) gets a connection error instead of the answer.  So the body is
            read and dropped here, once, after an answer, in pieces so that its size is never held in memory.  What
            has not arrived DRAIN_SECONDS later is left unread: the limit is time, so that a conversation of many
            megabytes, at any speed the client has, still gets its answer."""
            headers = getattr(self, "headers", None)
            if self.body_read or headers is None:
                return
            try:
                left = int(headers.get("Content-Length", 0))
            except ValueError:
                return
            if left <= 0:
                return
            deadline = time.monotonic() + self.DRAIN_SECONDS
            try:
                while left > 0 and (wait := deadline - time.monotonic()) > 0:
                    self.connection.settimeout(wait)         # a client that never sends what it announced
                    piece = self.rfile.read1(min(left, 1 << 20))   # one socket read: read() would wait for it all
                    if not piece:
                        break
                    left -= len(piece)
            except OSError:
                pass

        def parse_request(self):
            """Without an API key, every request (any method) first passes the Host check: DNS rebinding protection
            (host_allowed).  With a key a rebinding page cannot authenticate, so the check is skipped: tunnels and
            proxies that pass their own name on keep working."""
            if not super().parse_request():
                return False
            host = self.headers.get("Host")
            if svc.api_key or host_allowed(host, svc.host_names, "*" in svc.allowed_hosts):
                return True
            print(f"[hypercore] refused a request for Host {host!r} from {self.client_address[0]}: not a name this server "
                  f"answers to (add it to \"allowed_hosts\" in the config or HYPERCORE_ALLOWED_HOSTS, or set an API key)",
                  flush=True)
            self._json(403, {"error": {"type": "forbidden", "message":
                             f"Host {host!r} is not allowed (DNS rebinding protection). Reaching HyperCore under this "
                             f"name on purpose? Add it to \"allowed_hosts\" in the config (hypercore-<model>.json) or to "
                             f"the HYPERCORE_ALLOWED_HOSTS environment variable, or set an API key (\"api_key\"), which "
                             f"turns this check off"}})
            return False

        def _foreign_page(self) -> bool:
            """Without an API key, a /v1 POST from a browser page of another site (any site can POST text/plain
            there without a CORS preflight) would burn GPU time: an Origin header must name an allowed page, and
            then the body must be JSON.  No Origin (curl, the SDKs, other servers): any content type, as before."""
            origin = self.headers.get("Origin")
            if svc.api_key or not origin:
                return False
            if not origin_allowed(origin, self.headers.get("Host"), svc.host_names,
                                  [*svc.trusted_origins, *svc.cors_origins]):
                print(f"[hypercore] refused an API request from the web page {origin!r} (no API key; add its host to "
                      f"\"allowed_hosts\" or its origin to \"cors_origins\" in the config)", flush=True)
                self._json(403, {"error": {"type": "forbidden", "message":
                                 f"web pages of {origin} may not use this server without an API key; set \"api_key\", "
                                 f"or add the page's host to \"allowed_hosts\" (or its origin to \"cors_origins\") "
                                 f"in the config"}})
                return True
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": {"message": "send application/json"}})
                return True
            return False

        def end_headers(self):
            # HTTP/1.0 without keep-alive: the server closes the connection after every response.  Say so, or a
            # client that pools connections (.NET's SocketsHttpHandler) can put its next request on this socket
            # before the close reaches it, and that request fails with "response ended prematurely" without the
            # server ever reading it (a .NET agent app, 2026-10-03: 25 of ~1000 requests, 1-24 ms after the previous answer).
            self.send_header("Connection", "close")
            super().end_headers()

        def _watch_client(self, cancel: threading.Event) -> None:
            """#430 #431: cancel the request as soon as its client hangs up.  A non-streamed request writes nothing
            until it ends, and a streamed one only a keep-alive per prompt chunk (and the first write after a hang-up
            usually still succeeds), so a dropped request kept the engine busy until its answer or the whole prompt
            was done.  Every 0.5 s: the socket readable with nothing to read (EOF) means the client closed it.  A
            request is HTTP/1.0 and fully read here, so no later bytes are expected - data is not a hang-up."""
            done = self.watch_done = threading.Event()
            sock = self.connection

            def watch():
                while not done.wait(0.5) and not cancel.is_set():
                    try:
                        readable, _, _ = select.select([sock], [], [], 0)
                        gone = bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
                    except (ConnectionError, TimeoutError):
                        gone = True
                    except (OSError, ValueError):            # the socket was closed here: the request has ended
                        return
                    if gone:
                        self._note(outcome="disconnected")
                        cancel.set()
                        return

            threading.Thread(target=watch, daemon=True, name="hypercore-client-watch").start()

        def _note(self, **values):
            """#332: what the monitor shows about this request (nothing when the monitor is off)."""
            if self.record is not None:
                with svc.status_lock:
                    self.record.update(values)

        def _cors(self):
            """#321: CORS headers for an API path (/v1/*) and an origin the config lists in cors_origins - nothing
            otherwise, so a browser keeps every other page away from the API, /settings, /unload and the MCP tools."""
            if not svc.cors_origins or not self.path.split("?")[0].startswith("/v1/"):
                return
            origin = (self.headers.get("Origin") or "").rstrip("/")
            if "*" in svc.cors_origins:
                allow = "*"
            elif origin and origin in svc.cors_origins:
                allow = origin
                self.send_header("Vary", "Origin")
            else:
                return
            self.send_header("Access-Control-Allow-Origin", allow)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            # the headers the preflight asks for (SDKs add their own; "*" does not cover Authorization)
            asked = self.headers.get("Access-Control-Request-Headers")
            self.send_header("Access-Control-Allow-Headers",
                             asked or "Authorization, Content-Type, x-api-key, anthropic-version, anthropic-beta")
            self.send_header("Access-Control-Max-Age", "600")

        def do_OPTIONS(self):
            # a CORS preflight: no credentials come with it, so no API key; the headers only for cors_origins
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _json(self, code, obj, headers=None):
            if self.record is not None:
                with svc.status_lock:
                    self.record["http_status"] = code
                    raw = json.dumps(obj, ensure_ascii=False, indent=2)
                    self.record.update(response=raw[:262144], response_truncated=len(raw) > 262144)
                    for key in ("error", "usage", "timings"):
                        if key in obj:
                            self.record[key] = obj[key]
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            # The rule itself lives in serve/http_auth.py: the stub the load test runs
            # against enforces the same function, so a stub pass is evidence about this
            # server and not about a copy of the rule (#213: constant-time compare).
            if key_matches(self.headers, svc.api_key):
                return True
            self._json(401, auth_error())
            return False

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path.startswith("/fonts/"):
                # the web app's font (Outfit, OFL: serve/web/fonts); the page falls back to the system font
                name = path[len("/fonts/"):]
                f = ROOT / "serve" / "web" / "fonts" / name
                if "/" in name or "\\" in name or not name.endswith(".woff2") or not f.is_file():
                    self._json(404, {"error": {"message": "not found"}})
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/web/"):
                # the web app's own files (serve/web): styles, script, icon sprite - same origin, no CDN
                name = path[len("/web/"):]
                types = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                         ".svg": "image/svg+xml"}
                f = ROOT / "serve" / "web" / name
                ext = os.path.splitext(name)[1]
                if "/" in name or "\\" in name or ext not in types or not f.is_file():
                    self._json(404, {"error": {"message": "not found"}})
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", types[ext])
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/metrics":
                if self._authorized():
                    # the last 12 requests; `?requests=all` every one kept (the Monitor's "Show all", issue #35)
                    self._json(200, svc.metrics(all_requests="requests=all" in self.path))
                return
            if path == "/api/requests" and svc.api_monitor:
                if self._authorized():
                    request_id = parse_qs(urlsplit(self.path).query).get("id", [None])[0]
                    records = svc.request_records(request_id)
                    self._json(404 if records is None else 200,
                               {"error": {"message": "request no longer retained"}} if records is None else
                               records if request_id else {"requests": records, "retention": 100, "persistent": False,
                                                          "loaded": svc.loaded(), "auto_load": hasattr(svc.engine, "restart")})
                return
            if path == "/settings":
                if self._authorized():
                    self._json(200, {"shared": bool(svc.shared), "defaults": svc.shared})
                return
            if path == "/config/strata-hook":               # the upstream Strata setup hook: state, and the log tail
                if self._authorized():
                    offset = 0
                    query = self.path.split("?")[1] if "?" in self.path else ""
                    if "offset=" in query:
                        try:
                            offset = int(query.split("offset=")[1].split("&")[0])
                        except ValueError:
                            offset = 0
                    self._json(200, {**strata_hook.JOB.status(), **strata_hook.JOB.tail(offset)})
                return
            if path == "/config":                            # #564: the Settings view's keys of the run config
                if self._authorized():
                    self._config_get()
                return
            if path == "/history/distil":                    # the state of the history rebuild
                if self._authorized():
                    self._json(200, chat_archive.distil_status())
                return
            if path == "/history/status":                    # what is in the history folder now
                if self._authorized():
                    self._json(200, chat_archive.history_status())
                return
            if path == "/mcp":
                # the MCP servers, their state and tools (the web app's switch and Monitor card), plus
                # the size of the tool block the template adds: the page needs it before it has a
                # prompt_tokens of its own to anchor on.
                if self._authorized():
                    body = svc.mcp.status() if svc.mcp else {"servers": [], "tools": 0}
                    body["prefix"] = svc.tool_prefix(svc.mcp.template_tools() if svc.mcp else None)
                    # #565: what this server does with a prompt that is over the window. The page clips its
                    # own payload because a plain engine answers 400; a server started with --trim-context
                    # fits it here instead, so the page can send the turn whole and let the tokenizer decide.
                    body["context"] = {"max_context": int(getattr(svc.engine, "max_context", 0) or 0),
                                       "trim_context": bool(svc.trim_context),
                                       "fit_max_tokens": bool(svc.fit_max_tokens)}
                    self._json(200, body)
                return
            if path == "/assist/models":
                # the dropdown's list. serve/assist_fast_models.json pins it to the models that answered
                # under the lag limit; ?all=1 asks for everything the endpoint serves instead.
                if self._authorized():
                    try:
                        show_all = "all" in (self.path.split("?")[1] if "?" in self.path else "")
                        self._json(200, assist.list_models(show_all=show_all))
                    except Exception as e:
                        self._json(502, {"error": {"type": "assist_error", "message": str(e)}})
                return
            if path == "/assist/log":
                # the Monitor's "External assistants" card: the endpoint, the pinned list, the models the
                # last probe timed but did not pin, and the calls this server and the MCP bridge made.
                if self._authorized():
                    try:
                        self._json(200, assist.call_log(limit=int((self.path.split("?")[1].split("limit=")[1])
                                                                if "limit=" in self.path else 40)))
                    except Exception as e:
                        self._json(502, {"error": {"type": "assist_error", "message": str(e)}})
                return
            if path == "/assist/probe/status":
                # "Check lag" takes minutes: the POST that runs it stays open until the allowlist is
                # written, so the page polls this to show how far it has got instead of a dead button.
                if self._authorized():
                    self._json(200, assist.probe_progress())
                return
            if path == "/prompts":
                # the agent profiles the chat composer's dropdown is built from (serve/prompts.py, prompt.json)
                if self._authorized():
                    self._json(200, agent_prompts.catalog(svc.prompts_path))
                return
            if path == "/build/status":
                # what engine is installed, which releases this machine can install, and
                # whether one of them is newer (serve/build_api.py:build_status)
                if self._authorized():
                    self._json(200, build_api.build_status())
                return
            if path == "/versions":
                # every model version this machine can start: the engine builds stored
                # per quant and the run configs that name one (serve/quant_store.py)
                if self._authorized():
                    self._json(200, quant_store.view())
                return
            if path == "/tune/status":
                if self._authorized():
                    self._json(200, tune_api.JOB.status())
                return
            if path == "/tune/params":
                if self._authorized():
                    self._json(200, {"models": tune_api.available_models(),
                                     "objectives": list(tune_api.OBJECTIVES),
                                     "pinable": list(tune_api.PINABLE),
                                     "pin_options": tune_api.pin_options(),
                                     "priors": tune_api.priors(),
                                     "records": tune_api.records()})
                return
            if path == "/tune/results":
                # the finished sweep: the winner, its benchmark, and the files it wrote
                if self._authorized():
                    model = parse_qs(urlsplit(self.path).query).get("model", [""])[0]
                    self._json(200, {"model": model, "job": tune_api.JOB.status(),
                                     "deliverables": tune_api.deliverables(model)})
                return
            if path == "/build/options":
                # the build and setup choices of a release. With ?tag=<release> they are
                # read from that release's own CMakeLists.txt and setup.py, without cloning
                # it, so the tab can list them before any build starts.
                if self._authorized():
                    tag = parse_qs(urlsplit(self.path).query).get("tag", [""])[0]
                    if not tag:
                        self._json(200, json.loads(build_api.MANIFEST.read_text(encoding="utf-8"))
                                   if build_api.MANIFEST.is_file() else {"cmake_options": [], "setup_choices": []})
                        return
                    options = build_api.options_for(tag)
                    self._json(200 if not options.get("error") else 502,
                              {**options, "setup": build_api.setup_choices(options)})
                return
            if path == "" or (path == "/api-monitor" and svc.api_monitor):
                body = (ROOT / "serve" / "web" / ("monitor.html" if path else "index.html")).read_bytes()
                # The page is served by this server, so it may carry the key this server
                # requires: an install set up with a default key would otherwise 401 until
                # someone typed the key into the Settings field.
                if svc.api_key:
                    # insert before the title, never over it: replacing "<title>" left a stray
                    # "HyperCore" text node at the top of the page, above the banner
                    body = body.replace(b"<title>",
                        ("<script>window.HYPERCORE_API_KEY = %s</script>\n<title>" % json.dumps(svc.api_key)).encode(), 1)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                # No validator and no freshness: a page whose tabs change when the server
                # is restarted must never be served from a browser cache.
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in ("/health", "/api/health"):
                if isinstance(svc.engine, OfflineEngine):
                    engine_state = svc.engine.status()
                elif svc.loaded():
                    engine_state = {"state": "running"}
                else:
                    # A real engine whose process is gone: say so in its own words rather
                    # than reporting "running" for a process that is not there.
                    note = ""
                    for name in ("death_note", "last_stderr"):
                        get = getattr(svc.engine, name, None)
                        if callable(get):
                            note = str(get() or "")
                            if note:
                                break
                    engine_state = {"state": "offline", "code": UNLOADED_CODE,
                                   "reason": note or "the engine is not running", "detail": note}
                # A Stop from the app leaves the engine object in place with no process
                # behind it, so "running" would be a lie: the page would paint a Stop
                # button over an engine that is already down.
                if getattr(svc, "engine_stopped", False):
                    engine_state["state"] = "offline"
                    engine_state["stopped"] = True
                    engine_state.setdefault("code", "stopped")
                    engine_state.setdefault("reason", "the engine was stopped from this app")
                engine_state.setdefault("code", "running" if engine_state.get("state") == "running" else UNLOADED_CODE)
                if svc.engine_retry:
                    engine_state["retry"] = svc.engine_retry.status()
                # "status" is the engine's, not the server's: this HTTP server is up even
                # when no model is, and the page uses the engine state to mask the chat
                # while leaving the other tabs interactive.
                self._json(200, {"status": "ok" if svc.loaded() else "offline",
                                 "max_context": svc.engine.max_context, "model": svc.model,
                                 "images": svc.vision is not None, "api_key": bool(svc.api_key),
                                 "loaded": svc.loaded(), "service": "hypercore",
                                 "engine": engine_state})
            elif path == "/status":
                if not self._authorized():                  # #212: it shows the end of the last answer
                    return
                with svc.status_lock:
                    s = dict(svc.status)
                now = time.time()
                if s.get("busy"):
                    s["elapsed_s"] = round(now - s["started"], 1)
                    if s.get("first_token"):
                        s["tokens_per_s"] = round(svc._tok_s(), 1)
                        s["tokens_per_s_mean"] = round(svc._tok_s_mean(), 1)
                for k in ("started", "first_token"):
                    s.pop(k, None)
                self._json(200, s)
            elif path in ("/v1/models", "/models"):
                if self._authorized():
                    loaded = svc.loaded()
                    model = {"id": svc.model, "object": "model", "status": {"value": "loaded"},
                             "meta": {"n_ctx": svc.engine.max_context},
                             "architecture": {"input_modalities": ["text", "image"] if svc.vision is not None else ["text"],
                                              "output_modalities": ["text"]}}
                    if not loaded and (svc.idle_unload_s or getattr(svc.engine, "unloaded", False)):
                        model["status"] = {"value": "unloaded"}   # like llama-server's router: listed, loads on use
                        loaded = True
                    if svc.aliases:                       # #297: the aliases, and each one listed under its own id
                        model["aliases"] = list(svc.aliases)
                    data = [model, *({**model, "id": x, "alias_of": svc.model} for x in svc.aliases)]
                    self._json(200, {"object": "list", "data": data if loaded else []})
            elif path == "/props":
                if self._authorized():
                    self._props()
            elif path == "/slots":
                if self._authorized():
                    loaded = not hasattr(svc.engine, "alive") or svc.engine.alive()
                    with svc.status_lock:
                        busy = bool(svc.status.get("busy"))
                    slot = {"id": 0, "n_ctx": svc.engine.max_context, "is_processing": busy}
                    self._json(200, [slot] if loaded else [])
            elif path == "/v1/status":
                if self._authorized():
                    self._json(200, svc.v1_status())
            else:
                self._json(404, {"error": {"message": "not found"}})

        def _raw_stream(self, req: dict) -> None:
            """POST /infer/stream: the token frames, unshaped.

            Load tests and embedders use this. It is deliberately not the API path: no
            chat template, no detokenizer, no structured filter - just the frames the
            worker sent, in order.
            """
            sup = svc.engine.supervisor
            tokens = req.get("prompt_token_ids") or req.get("tokens")
            if not isinstance(tokens, list) or not tokens:
                self._json(400, {"error": {"type": "invalid_request",
                                          "message": "prompt_token_ids: a non-empty list of ints"}})
                return
            if not svc.engine.alive():
                self._json(503, {"error": {"type": "engine_unavailable",
                                          "message": "the compute node is offline"}})
                return
            # No host-side slot pre-check. The snapshot in max_batch_slots is taken at
            # startup and can disagree with the worker after a config change, so the
            # worker's own answer is the only one we act on.
            request_id = sup.next_request_id()
            outcome = stream_sse(
                self,
                sup,
                request_id,
                [int(t) for t in tokens],
                temperature=float(req.get("temperature", 0.0)),
                top_p=float(req.get("top_p", 1.0)),
                top_k=int(req.get("top_k", 1)),
                max_new_tokens=int(req.get("max_new_tokens", 64)),
                pcie_frac=float(req.get("pcie_frac", -1.0)),
                spec_min_p=float(req.get("spec_min_p", -1.0)),
            )
            if outcome == "busy":
                self._json(503, {"error": {"type": "busy",
                                          "message": "the compute node has no free batch slot"}})
            elif outcome == "rejected":
                self._json(400, {"error": {"type": "invalid_request_error",
                                          "message": "the engine rejected this request"}})
            elif outcome == "worker_gone":
                self._json(503, {"error": {"type": "engine_unavailable",
                                          "message": "the compute node is offline"}})
            elif outcome != "complete":
                logger.warning("raw stream %d ended: %s", request_id, outcome)

        def do_POST(self):
            if not self._authorized():
                return
            path = self.path.split("?")[0].rstrip("/")   # issue #55: Claude Code posts /v1/messages?beta=true
            if path.startswith("/v1/") and self._foreign_page():
                return
            if path == "/settings":
                self._settings()
                return
            if path == "/config/strata-hook":
                # PLAN 10.1: run the installed Strata setup.py from the page. The subprocess
                # is detached and its output goes to a file in logs/; this call returns as
                # soon as it is spawned, so a model download never holds the request loop.
                try:
                    body = json.loads(self._body() or b"{}")
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                args = body.get("args")
                if not isinstance(args, list):
                    args = None
                result = strata_hook.JOB.start(str(body.get("path") or "") or None,
                                             [str(a) for a in args] if args else None,
                                             run_config=svc.config_path)
                self._json(200 if result.get("state") == "running" else 400, result)
                return
            if path == "/config/strata-hook/stop":
                self._json(200, strata_hook.JOB.cancel())
                return
            if path == "/config":
                self._config_post()
                return
            if path in ("/unload", "/load") and not self._control_body():
                return
            # JSON from HyperCore's own page only, as /settings: else a plain form POST from any site unloads the model
            if path in ("/unload", "/load") and not self._own_page("the model can be loaded or unloaded"):
                return
            # the same for the slot files: else any site could overwrite a saved conversation or replace the live one
            if path.startswith("/slots/") and not self._own_page("conversations can be saved or restored"):
                return
            if path == "/unload":                            # give the GPU back now (between requests)
                wait_s = unload_wait(parse_qs(urlsplit(self.path).query).get("wait", [None])[0], svc.UNLOAD_WAIT_S)
                try:
                    r = svc.unload_waiting(wait_s) if wait_s else svc.unload()
                except EngineStuck as e:
                    self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                    return
                self._json(409 if r == "busy" else 200, {"status": r})
                return
            if path == "/load":                              # load now, e.g. ahead of a request
                try:
                    # A deliberate Stop is not an unload: the Monitor's Load button is exactly the Start
                    # that brings the engine back, so it is not answered 409 to a request that plainly
                    # means "run it again".
                    if svc.engine_stopped:
                        if not svc.start_engine():
                            self._json(503, {"error": {"type": "server_error",
                                                      "message": "the engine did not come back: see the engine log"}})
                            return
                        self._json(200, {"status": "loaded",
                                         "note": "the engine was stopped with /v1/stop; this started it"})
                        return
                    svc.load()
                    self._json(200, {"status": "loaded"})
                except GpuBusy as e:
                    self._json(503, {"error": {"type": "server_error", "message": str(e)}},
                              headers={"Retry-After": int(e.retry_after)} if e.retry_after else None)
                return
            try:
                req = json.loads(self._body() or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if path == "/mcp":
                    # Turn one MCP server on or off. The model stays loaded; the next chat
                    # simply is not offered that server's tools.
                    if svc.mcp is None:
                        self._json(404, {"error": {"message": "no MCP servers are configured"}})
                        return
                    if not self._own_page("an MCP server can be turned on or off"):
                        return
                    name, enabled = req.get("server"), req.get("enabled")
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("server: the server's name")
                    if not isinstance(enabled, bool):
                        raise ValueError("enabled: true or false")
                    try:
                        svc.mcp.set_enabled(name.strip(), enabled)
                    except KeyError:
                        self._json(404, {"error": {"message": f"no MCP server named {name.strip()!r}"}})
                        return
                    print(f"[hypercore] MCP server {name.strip()!r} {'on' if enabled else 'off'}", flush=True)
                    self._json(200, svc.mcp.status())
                    return
                if path == "/build/check":
                    self._json(200, {"installed": build_api.installed_version(),
                                     "latest": build_api.latest_release()})
                    return
                if path == "/build/fetch-prebuilt":
                    releases = build_api.latest_release().get("releases") or []
                    tag = req.get("tag")
                    release = next((r for r in releases if r["tag"] == tag), releases[0] if releases else None)
                    if not release:
                        self._json(400, {"error": {"message": "no release found"}})
                        return
                    result = build_api.JOB.fetch_prebuilt(release, req.get("quant"))
                    self._json(200 if result.get("ok") else 500, result)
                    return
                if path == "/build/start":
                    self._json(200, build_api.JOB.start(str(req.get("tag") or "main"),
                                                       req.get("options") or {},
                                                       req.get("quant")))
                    return
                if path == "/versions/select":
                    # start one of the stored quant builds: place its binaries, point
                    # the engine at its run config, restart it. The app stays up either way.
                    wanted = str(req.get("version") or req.get("quant") or "").strip()
                    if not wanted:
                        self._json(400, {"error": {"message": 'send {"version": "q2_0"}'}})
                        return
                    try:
                        entry = quant_store.select(wanted)
                    except ValueError as exc:
                        self._json(400, {"error": {"message": str(exc)}})
                        return
                    ok, note = apply_model_version(svc, entry)
                    self._json(200 if ok else 503, {"status": "switching" if ok else "failed",
                                                   "version": entry["key"], "model": svc.model,
                                                   "message": note, "versions": quant_store.list_versions()})
                    return
                if path == "/build/log":
                    # the tail from a byte offset, so the tab appends instead of re-reading
                    offset = 0
                    query = self.path.split("?")[1] if "?" in self.path else ""
                    if "offset=" in query:
                        try:
                            offset = int(query.split("offset=")[1].split("&")[0])
                        except ValueError:
                            offset = 0
                    self._json(200, {**build_api.JOB.status(), **build_api.JOB.tail(offset)})
                    return
                if path == "/build/cancel":
                    self._json(200, build_api.JOB.cancel())
                    return
                if path == "/build/apply":
                    # the new binary is already in engine/: restart the engine on it
                    if getattr(svc.engine, "attached", False):
                        # We do not own this engine: the binary is in engine/, but the
                        # server that started it is the one that has to run it.
                        self._json(200, {
                            "ok": False,
                            "attached": True,
                            "installed": build_api.installed_version(),
                            "note": "the new binary is in engine/. This server is attached to "
                                   "an engine it did not start: restart that server to run it.",
                        })
                        return
                    if not hasattr(svc.engine, "restart"):
                        self._json(400, {"error": {"message": "this server has no engine to restart"}})
                        return
                    ok = False
                    try:
                        ok = bool(svc.engine.restart())
                    except Exception as exc:  # noqa: BLE001 - the engine may need a new config
                        self._json(500, {"error": {"message": f"the engine did not restart: {exc}"}})
                        return
                    self._json(200 if ok else 500, {
                        "ok": ok,
                        "installed": build_api.installed_version(),
                        "note": "the engine was restarted with the new binary" if ok else
                                "the binary is in engine/ but the engine would not start: check logs/",
                    })
                    return
                if path == "/tune/run":
                    # a sweep needs the GPU to itself: give the chat engine's card back
                    # first. unload() is the ask both engines understand - a direct one
                    # quits its process, an attached one asks its own server to free the
                    # card and can bring it back with /load.
                    try:
                        if hasattr(svc.engine, "unload"):
                            svc.engine.unload()
                        elif hasattr(svc.engine, "stop"):
                            svc.engine.stop()
                    except Exception:  # noqa: BLE001 - an already-stopped engine is not an error
                        pass
                    self._json(200, tune_api.JOB.start(req))
                    return
                if path == "/tune/stop":
                    self._json(200, tune_api.JOB.stop())
                    return
                if path == "/tune/apply":
                    model = str(req.get("model") or "")
                    result = tune_api.apply(model, Path(svc.config_path) if svc.config_path else ROOT / "config" / f"run-{model}.json")
                    if result.get("ok") and hasattr(svc.engine, "restart"):
                        try:
                            svc.engine.restart()
                            result["restarted"] = True
                        except Exception as exc:  # noqa: BLE001
                            result["restarted"] = False
                            result["note"] = str(exc)
                    self._json(200 if result.get("ok") else 400, result)
                    return
                if path == "/history/archive":               # the Chat tab's "Save to history" button
                    if not self._own_page("a chat can be written to the history folder"):
                        return
                    try:
                        result = chat_archive.archive_chat(
                            str(req.get("chat") or req.get("text") or ""),
                            str(req.get("title") or ""))
                        # The archive is written; the distillation that turns it into topic
                        # files runs behind the response, not inside it.
                        result["distil"] = chat_archive.queue_distil()
                        self._json(200, result)
                    except ValueError as e:
                        self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                    except OSError as e:
                        self._json(500, {"error": {"type": "server_error",
                                                  "message": f"could not write the chat: {e}"}})
                    return
                if path == "/history/distil":                # rebuild the topic files now
                    if not self._own_page("the history can be rebuilt"):
                        return
                    result = chat_archive.run_builder()
                    self._json(200 if result["ok"] else 500, result)
                    return
                if path == "/assist/models/probe":           # re-time the endpoint and rewrite the pinned list
                    try:
                        self._json(200, assist.probe_models(
                            max_lag=float(req.get("max_lag") or assist.DEFAULT_MAX_LAG),
                            rounds=int(req.get("rounds") or 2),
                            workers=int(req.get("workers") or 8)))
                    except Exception as e:
                        self._json(502, {"error": {"type": "assist_error", "message": str(e)}})
                    return
                if path == "/assist/chat":
                    model = str(req.get("model") or "").strip()
                    messages = req.get("messages") or []
                    max_tokens = int(req.get("max_tokens") or assist.ASSIST_DEFAULTS["max_tokens"])
                    try:
                        self._json(200, assist.converse(model, messages, max_tokens))
                    except Exception as e:
                        self._json(502, {"error": {"type": "assist_error", "message": str(e)}})
                    return
                if path.startswith("/v1/responses/"):        # retrieve/delete/cancel/compact: nothing is stored
                    self._json(404, responses_error_body("this server keeps no responses (stateless); send the "
                                                         "whole conversation to POST /v1/responses", code="not_found"))
                    return
                if path in ("/v1/load", "/v1/unload"):
                    if not self._own_page("the model can be loaded or unloaded"):
                        return
                    if req.get("model") not in (None, svc.model):
                        self._json(404, {"error": {"message": "model not found"}})
                        return
                    if path == "/v1/unload":
                        wait_s = unload_wait(parse_qs(urlsplit(self.path).query).get("wait", [None])[0],
                                            svc.UNLOAD_WAIT_S)
                        result = svc.unload_waiting(wait_s) if wait_s else svc.unload()
                        if result == "busy":
                            raise ModelBusy("a request is running or queued")
                    else:
                        if not svc.fifo.acquire(blocking=False):
                            raise ModelBusy("a request is running or queued")
                        try:
                            with svc.status_lock:
                                if svc.status.get("busy") or svc.status.get("queued"):
                                    raise ModelBusy("a request is running or queued")
                            svc.ensure_loaded()
                            svc.last_request_at = time.time()
                        finally:
                            svc.fifo.release()
                        result = "loaded"
                    self._json(200, {"status": result, **svc.v1_status()})
                    return
                if path == "/v1/shutdown":               # #641: the banner's Shut Down button
                    if not self._own_page("the server can be shut down"):
                        return
                    running = [n for n, p in (("the model engine", getattr(svc.engine, "proc", None)),
                                             ("the image encoder", svc.vision),
                                             ("the MCP servers", getattr(svc, "mcp", None))) if p]
                    svc.stop_requested.set()             # main() closes them, in order, and exits
                    self._json(200, {"status": "shutting_down", "model": svc.model,
                                     "closing": running or ["the HTTP server"],
                                     "note": "the engine is asked to free its memory first, which can "
                                             "take a few seconds; nothing is killed until it answers"})
                    return
                if path in ("/v1/stop", "/v1/start"):   # the engine-only pair: the HTTP server stays up
                    if not self._own_page("the engine can be stopped or started"):
                        return
                    if path == "/v1/stop":
                        closed = []
                        if getattr(svc, "engine", None) is not None and getattr(svc.engine, "alive", lambda: False)():
                            svc.engine.unload()
                            closed.append("the model engine")
                        if getattr(svc, "vision", None) is not None:
                            # the image encoder is freed the same way close_everything frees it
                            off = getattr(svc.vision, "unload", None) or getattr(svc.vision, "shutdown", None)
                            if off is not None:
                                off()
                            closed.append("the image encoder")
                        if getattr(svc, "mcp", None) is not None:
                            svc.mcp.close()
                            closed.append("the MCP servers")
                        svc.engine_stopped = True
                        self._json(200, {"status": "stopped", "model": svc.model,
                                         "stopped": closed or ["nothing was running"],
                                         "note": "the HTTP server stays up: POST /v1/start brings the engine back"})
                        return
                    try:
                        ok = svc.start_engine()
                    except Exception as e:
                        self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                        return
                    if not ok:
                        self._json(503, {"error": {"type": "server_error",
                                                  "message": "the engine did not come back: see the engine log"}})
                        return
                    if getattr(svc, "mcp", None) is not None:
                        try:
                            svc.mcp.start()             # a hub that will not come back is not a failed start
                        except Exception:
                            pass
                    svc.engine_stopped = False
                    self._json(200, {"status": "running", "model": svc.model,
                                     "note": "the engine is loading: the first answer takes a minute or two"})
                    return
                if path == "/v1/vram":                       # #533: give VRAM back to other programs, or take it
                    if not self._own_page("the VRAM reserve can be changed"):
                        return
                    r = req.get("reserve_mib")
                    if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
                        raise ValueError("reserve_mib: a whole number of MiB (0 or more), or null for the start's")
                    try:
                        self._json(200, svc.vram(r))
                    except EngineDied as e:
                        self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                    return
                if path == "/infer/stream":
                    self._raw_stream(req)
                    return
                if path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
                    self.record = svc.begin_request(path, req)
                if path == "/v1/responses":
                    self._responses(req)
                elif path.startswith("/slots/"):
                    query = parse_qs(self.path.partition("?")[2])
                    try:
                        code, body = svc.slot_action(path[len("/slots/"):], (query.get("action") or [""])[0],
                                                     req.get("filename"))
                    except EngineDied as e:                  # EngineSilent included: the engine was ended
                        code, body = 500, {"error": {"code": 500, "message": str(e) + "; the next request starts "
                                                     "the engine again", "type": "server_error"}}
                    self._json(code, body)
                elif path == "/v1/chat/completions":
                    self._openai(req)
                elif path == "/v1/messages":
                    self._anthropic(req)
                elif path == "/v1/messages/count_tokens":
                    self._count_tokens(req)
                else:
                    self._json(404, {"error": {"message": "not found"}})
            except ValueError as e:
                print(f"[hypercore] 400 invalid request: {e}", flush=True)
                if path == "/v1/responses":
                    self._json(400, responses_error_body(str(e)))
                else:
                    self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except WorkerBusy as e:
                self._json(503, {"error": {"type": "busy", "message": str(e)}})
            except ModelBusy as e:
                self._json(409, {"error": {"type": "model_busy", "message": str(e)}})
            except EngineStopped as e:
                self._json(409, {"error": {"type": "engine_stopped", "message": str(e)}})
            except StructuredOutputError as e:
                self._json(502, {"error": {"type": "structured_output_failed", "code": "structured_output_failed",
                                          "message": str(e)}})
            except (GpuBusy, EngineStarting, SlotRefused) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}},
                          headers={"Retry-After": int(e.retry_after)} if getattr(e, "retry_after", None) else None)
            except EngineUnavailable as e:                 # the app is up; the model is not
                self._json(503, {"error": {"type": "engine_unavailable", "message": str(e)}},
                          headers={"Retry-After": int(e.retry_after)})
                return
            except EngineDied as e:                          # before the answer started (not streamed)
                self._json(503, {"error": {"type": "server_error", "message": f"{e}; the next request restarts it"}})
            except EngineStuck as e:                         # an unload or restart that could not end the engine
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})
            except Exception as e:                           # a bug in this request path: answer 500, not a socket trace
                import traceback
                print(f"[hypercore] 500 unhandled request error: {e}", flush=True)
                traceback.print_exc()
                self._note(error={"type": "server_error", "message": str(e)})
                if getattr(self, "sse_started", False):
                    # The SSE headers are already on the wire, so a JSON 500 would be a second
                    # response. Close the stream with an error event instead.
                    try:
                        if path == "/v1/responses":
                            self.wfile.write(b"data: " + json.dumps(
                                responses_error_body(str(e), "server_error", code="server_error")
                            ).encode() + b"\n\ndata: [DONE]\n\n")
                        elif path == "/v1/messages":
                            err = {"type": "error", "error": {"type": "api_error", "message": str(e)}}
                            self.wfile.write(b"event: error\ndata: " + json.dumps(err).encode() + b"\n\n")
                        else:
                            err = {"error": {"type": "server_error", "message": str(e)}}
                            self.wfile.write(b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")
                        self.wfile.flush()
                    except OSError:
                        pass
                else:
                    try:
                        if path == "/v1/responses":
                            self._json(500, responses_error_body(str(e), "server_error", code="server_error"))
                        else:
                            self._json(500, {"error": {"type": "server_error", "message": str(e)}})
                    except OSError:
                        pass

            except OSError:
                self._note(outcome="disconnected")
                raise                                        # as before #332: the server's own handling
            finally:
                svc.drop_embeddings()                        # the images' file of a request that never got to run()
                if self.watch_done is not None:
                    self.watch_done.set()
                record = self.record
                if record is not None:
                    with svc.status_lock:
                        record["wallclock_s"] = round(time.perf_counter() - record["_clock"], 3)
                        record["finished_at"] = time.time()
                        record["state"] = "error" if record.get("error") else record.get("outcome", "completed")
                    svc.request_trace.record = None

        def _props(self):
            model = parse_qs(urlsplit(self.path).query).get("model", [svc.model])[0]
            if model not in svc.model_names():
                self._json(404, {"error": {"message": "model not found"}})
                return
            if not svc.loaded() and not getattr(svc.engine, "unloaded", False):
                self._json(503, {"error": {"message": "the engine is not running"}})
                return
            defaults = {**svc.sampling_defaults, **svc.shared}
            names = {"repetition_penalty": "repeat_penalty", "penalty_last_n": "repeat_last_n"}
            params = {names.get(k, k): v for k, v in defaults.items()
                      if k in ("temperature", "top_p", "top_k", "min_p", "seed", "repetition_penalty",
                               "presence_penalty", "frequency_penalty", "penalty_last_n")}
            params["n_predict"] = svc.shared.get("max_tokens", -1)
            props = {"default_generation_settings": {"n_ctx": svc.engine.max_context, "params": params},
                     "total_slots": 1, "model_alias": svc.model, "chat_template": svc.template.source,
                     "modalities": {"vision": svc.vision is not None}, "models_autoload": hasattr(svc.engine, "restart"),
                     "is_sleeping": not svc.loaded()}
            if getattr(svc.engine, "model_path", None):
                props["model_path"] = svc.engine.model_path
            version = getattr(svc.engine, "info", {}).get("version")
            if version:
                props["build_info"] = "HyperCore " + str(version)
            self._json(200, props)

        def _control_body(self) -> bool:
            """Consume the unused control body before replying/closing (Windows otherwise sends a TCP reset)."""
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(400, {"error": {"message": "invalid Content-Length"}})
                return False
            if not 0 <= length <= 65536:
                self._json(413, {"error": {"message": "control request body is limited to 64 KiB"}})
                return False
            timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(2.0)
                complete = len(self.rfile.read(length)) == length
            except OSError:
                complete = False
            finally:
                self.connection.settimeout(timeout)
                self.body_read = True                    # nothing left for the drain to wait for (#594)
            if not complete:
                self._json(400, {"error": {"message": "incomplete control request body"}})
            return complete

        def _own_page(self, what) -> bool:
            """Only JSON (a form or a "simple" cross-site request can't send it without a CORS preflight, which this
            server never grants) and no foreign Origin: a web page elsewhere must not change settings or run tools."""
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": {"message": "send application/json"}})
                return False
            if self._foreign_origin():
                self._json(403, {"error": {"message": f"{what} only from HyperCore's own page (or an origin in the "
                                                      f"config's trusted_origins)"}})
                return False
            return True

        def _config_get(self):
            if not svc.config_path:
                self._json(404, {"error": {"message": "this server was started without a run config"}})
                return
            try:
                cfg = runconfig.load(svc.config_path)
            except (OSError, ValueError) as e:
                self._json(500, {"error": {"type": "server_error", "message": f"the run config cannot be read: {e}"}})
                return
            self._json(200, runconfig.view(cfg, svc.config_path))

        def _config_post(self):
            """#564: change a few documented keys of the run config - JSON from HyperCore's own page only, as
            /settings (the key is checked before); every other key of the file stays as it is."""
            body = self._body()
            if not self._own_page("the run config can be changed"):
                return
            if not svc.config_path:
                self._json(404, {"error": {"message": "this server was started without a run config"}})
                return
            try:
                req = json.loads(body or b"{}")
                with svc.config_lock:
                    cfg = runconfig.load(svc.config_path)
                    new, changed = runconfig.apply(cfg, req.get("set") if isinstance(req, dict) else None)
                    if changed:
                        bak = runconfig.save(svc.config_path, new)
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            except OSError as e:
                self._json(500, {"error": {"type": "server_error", "message": f"the run config cannot be written: {e}"}})
                return
            if changed:
                print(f"[hypercore] the Settings view changed {', '.join(changed)} in {Path(svc.config_path).name} "
                      f"(the earlier file: {bak.name}); used from the next start", flush=True)
            self._json(200, {**runconfig.view(new, svc.config_path), "changed": changed})
        def _foreign_origin(self) -> bool:
            """A web page of another origin sent this.  The Origin must be this server's own address (host and port),
            or an origin the config trusts (trusted_origins: the web app behind a reverse proxy or tunnel); no Origin
            is a client that is no browser.  Headers a proxy adds (X-Forwarded-*, CF-Ray, CF-Connecting-IP) prove
            nothing about the page that sent the request, so they open nothing."""
            origin = (self.headers.get("Origin") or "").rstrip("/")
            return bool(origin) and origin.split("://", 1)[-1] != self.headers.get("Host", "") and \
                origin not in svc.trusted_origins

        def _no_local_images(self, messages):
            """A web page of another origin must not have a file on this computer read as an image.  Without an
            api_key, _foreign_page refuses such a page unless cors_origins lets it in - and a page cors_origins lets
            in (["*"]: every page) could name any file and, CORS allowing, read what the model says about it.  data:
            and http(s) images are the page's own to send; files come from HyperCore's own page, a trusted origin or a
            client that is no browser.  ValueError (a 400)."""
            if self._foreign_origin() and any(not src.startswith(("data:", "http://", "https://"))
                                              for src in images_of(messages)):
                raise ValueError("a web page of another origin cannot have a file on this computer read as an "
                                 "image: send it as a data: URL (or add the page's origin to the config's "
                                 "trusted_origins)")

        def _settings(self):
            # They change what every client gets, so only the app's own page may set them
            body = self._body()
            if not self._own_page("settings can be changed"):
                return
            try:
                req = json.loads(body or b"{}")
                shared = svc.set_shared(req.get("defaults") if isinstance(req, dict) else None)
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            print("[hypercore] other apps now use the Chat settings: " + ", ".join(f"{k}={v}" for k, v in shared.items())
                  if shared else "[hypercore] other apps use their own settings again", flush=True)
            self._json(200, {"shared": bool(shared), "defaults": shared})

        def _sse(self):
            self._note(http_status=200)
            self.sse_started = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")   # nginx and similar proxies pass each event at once
            self._cors()
            self.end_headers()

        def _capture(self, items, api):
            """Retain bounded input/output for the monitor without changing the API response (the items as they
            are when the monitor is off)."""
            if self.record is None:
                return items
            return self._captured(items, api)

        def _captured(self, items, api):
            try:
                for item in items:
                    if item is not None:
                        if api == "openai":
                            delta = item["choices"][0]["delta"]
                            content, reasoning = delta.get("content", ""), delta.get("reasoning_content", "")
                            usage, timings = item.get("usage"), item.get("timings")
                        elif api == "responses":
                            kind = item["type"]
                            content = item["delta"] if kind == "response.output_text.delta" else ""
                            reasoning = item["delta"] if kind == "response.reasoning_text.delta" else ""
                            usage, timings = (item.get("response") or {}).get("usage"), None
                        else:
                            _, event = item
                            delta = event.get("delta", {})
                            content, reasoning = delta.get("text", ""), delta.get("thinking", "")
                            usage, timings = event.get("usage"), None
                        with svc.status_lock:
                            for name, value in (("output", content), ("reasoning", reasoning)):
                                if value:
                                    combined = self.record[name] + value
                                    self.record[name] = combined[:262144]
                                    self.record[name + "_truncated"] |= len(combined) > 262144
                            if usage:
                                self.record["usage"] = usage
                            if timings:
                                self.record["timings"] = timings
                    yield item
            finally:
                items.close()

        def _openai(self, req):
            req = svc.with_shared(req, "openai")
            messages, tools, kw = openai_to_messages(req)
            self._no_local_images(messages)
            if tool_choice_of(req.get("tool_choice"))[0] == "none":   # as the Responses route: no tools are offered
                tools = None
            force = forced_call(req.get("tool_choice"), tools)      # a bad value is a 400 before anything is sent
            messages, validator = prepare_format(req.get("response_format"), messages)
            if validator is not None and (tools or req.get("hypercore_mcp")):
                raise ValueError("structured response_format with tools/MCP is not supported")
            svc.load()
            max_req = max_new = int(req.get("max_completion_tokens") or req.get("max_tokens") or 0)   # 0/-1: the rest
            use_mcp = req.get("hypercore_mcp") is True and svc.mcp is not None      # the web app's opt-in (serve/mcp/)
            own = {t.get("name") for t in tools or [] if isinstance(t, dict)}   # #592: a second line of defence
            if use_mcp:
                if not self._own_page("MCP tools can be used"):   # tools run with the user's rights on this PC
                    return
                svc.mcp.wait(10)                                  # servers still starting (only right after start)
                extra = svc.mcp.template_tools(exclude=own)       # the request's own tools win a name clash
                use_mcp = bool(extra)
                tools = (tools or []) + extra or None
            if force and use_mcp:
                raise ValueError("a forced tool_choice with MCP tools is not supported")
            svc.reasoning_budget(req)                         # a bad value is a 400 before anything is sent
            stop_strings(req)                                 # ... and so is a bad stop / stop_sequences
            seed_compact = None
            try:
                ids, thinking, max_new = svc.prepare(messages, tools, kw, max_new, force=force)
            except ValueError as error:
                # Only the web app's tool loop recovers. A raw API client keeps the
                # engine's contract: an oversized prompt is refused, never cut.
                if not use_mcp or "never truncated" not in str(error):
                    raise
                recovered = _recover_overflow(svc, svc.mcp, messages, tools, kw, max_new, force)
                if recovered is None:
                    raise
                messages, (ids, thinking, max_new), seed_compact = recovered
            _debug_req("openai", req, messages, tools, max_new, thinking, len(ids))
            cancel = threading.Event()
            self._watch_client(cancel)                       # #430 #431
            mcp_names = {t["name"] for t in (extra if use_mcp else [])}

            def turn(m, i, th, mn):
                """This turn's events: the MCP loop when the request opted in, the engine otherwise."""
                if use_mcp:
                    return run_with_mcp(svc, svc.mcp, m, tools, kw, i, th, mn, max_req, req, cancel, mcp_names)
                return svc.run(i, th, tools, mn, req, cancel, force=force)

            run = run_with_mcp(svc, svc.mcp, messages, tools, kw, ids, thinking, max_new, max_req, req, cancel,
                               mcp_names, seed_compact=seed_compact) if use_mcp else None
            if svc.turn_tail_check and not force:        # the end-of-turn "is the work done?" check
                run = with_tail_check(svc, run or svc.run(ids, thinking, tools, max_new, req, cancel, force=force),
                                      messages, tools, kw, max_new, req, cancel, turn)
            chunks = openai_chunks(svc, req, ids, thinking, tools, max_new, cancel, run=run, force=force)
            if validator is not None:
                chunks = structured_chunks(chunks, validator)
            chunks = self._capture(chunks, "openai")
            if not req.get("stream"):
                return self._json(200, openai_collect(chunks))
            self._sse()
            try:
                for c in chunks:
                    if c is None:
                        self.wfile.write(b": keep-alive\n\n")      # an SSE comment: clients ignore it
                    else:
                        self.wfile.write(b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
            except OSError:
                self._note(outcome="disconnected")
                cancel.set()                                 # client went away: stop the engine
                chunks.close()
            except EngineDied as e:                          # mid-stream: say so, then end the stream properly
                err = {"error": {"type": "server_error", "message": f"{e}; the next request restarts it"}}
                self._note(error=err["error"])
                self.wfile.write(b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")
            except StructuredOutputError as e:
                err = {"error": {"type": "structured_output_failed", "code": "structured_output_failed", "message": str(e)}}
                self._note(error=err["error"])
                self.wfile.write(b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")
            except ValueError as e:                          # the engine's ERR after the stream started: the
                err = {"error": {"type": "server_error", "message": str(e)}}   # headers are sent, so no 400 now
                self._note(error=err["error"])
                self.wfile.write(b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")

        def _responses(self, req):
            """#451: POST /v1/responses - OpenAI's Responses API, stateless (serve/responses.py), on the chat path.
            Errors use the Responses format; once the stream has started they arrive as a response.failed event."""
            try:
                ids, thinking, tools, max_new, asm, validator, req, messages, kw = self._responses_prepare(req)
            except ResponsesError as e:
                return self._json(e.status, e.body())
            except WorkerBusy as e:
                return self._json(503, responses_error_body(str(e), "server_error", code="server_error"))
            except ModelBusy as e:
                return self._json(409, responses_error_body(str(e), "server_error", code="model_busy"))
            except (GpuBusy, EngineStarting, EngineStuck, EngineDied, SlotRefused) as e:
                return self._json(503, responses_error_body(str(e), "server_error", code="server_error"),
                                  headers={"Retry-After": int(e.retry_after)}
                                  if getattr(e, "retry_after", None) else None)
            cancel = threading.Event()
            self._watch_client(cancel)                       # #430 #431
            run = svc.run(ids, thinking, tools, max_new, req, cancel)
            if svc.turn_tail_check:                          # the end-of-turn "is it done?" check
                run = with_tail_check(svc, run, messages, tools, kw, max_new, req, cancel,
                                      lambda m, i, th, mn: svc.run(i, th, tools, mn, req, cancel))

            def check(text, finish):
                return validated_json(text, validator, finish)

            def events():
                yield from asm.start()
                done = None
                for kind, x in run:
                    if kind == "ping":
                        yield None
                    elif kind == "event":
                        yield from asm.feed(x)
                    elif kind == "mcp":
                        continue
                    elif kind == "done":
                        done = x
                if done is not None and done["finish"] != "cancel" and not cancel.is_set():
                    yield from asm.finish(done, check)
            items = self._capture(events(), "responses")
            if not req.get("stream"):
                try:
                    result = responses_api.collect(items)
                except StructuredOutputError as e:
                    return self._json(502, responses_error_body(str(e), "server_error",
                                                                code="structured_output_failed"))
                except EngineDied as e:
                    return self._json(503, responses_error_body(f"{e}; the next request restarts it", "server_error",
                                                                code="server_error"))
                except ValueError as e:                      # the engine's ERR line
                    return self._json(500, responses_error_body(str(e), "server_error", code="server_error"))
                return self._json(200, result)
            self._sse()
            last = time.monotonic()

            def send(e):
                self.wfile.write(f"event: {e['type']}\n".encode() + b"data: " +
                                 json.dumps(e, ensure_ascii=False).encode() + b"\n\n")

            try:
                for e in items:
                    if e is None:
                        # Codex's idle timeout (5 minutes by default) counts events, not SSE comments: while a long
                        # prompt is read, a response.in_progress every 15 s tells it the server is still working
                        if time.monotonic() - last >= 15:
                            send(asm.in_progress())
                            last = time.monotonic()
                        else:
                            self.wfile.write(b": keep-alive\n\n")
                    else:
                        send(e)
                        last = time.monotonic()
                    self.wfile.flush()
            except OSError:
                self._note(outcome="disconnected")
                cancel.set()                                 # client went away: stop the engine
                items.close()
            except EngineDied as e:                          # mid-stream: response.failed, then the stream ends
                self._responses_failed(asm, f"{e}; the next request restarts it", "server_error", send)
            except StructuredOutputError as e:
                self._responses_failed(asm, str(e), "structured_output_failed", send)
            except ValueError as e:                          # the engine's ERR after the stream started
                self._responses_failed(asm, str(e), "server_error", send)

        def _responses_failed(self, asm, message, code, send):
            e = asm.failed(message, code)
            self._note(error=e["response"]["error"])
            try:
                send(e)
                self.wfile.flush()
            except OSError:
                pass

        def _responses_prepare(self, req):
            responses_api.check_request(req)
            messages = responses_api.input_messages(req)
            tools, names, skipped = responses_api.request_tools(req)
            kw = responses_api.template_kwargs(req, svc.shared)
            try:
                self._no_local_images(messages)              # as on the chat route: no file read for a foreign page
            except ValueError as e:
                raise ResponsesError(str(e), "input") from None
            try:
                messages, validator = prepare_format(responses_api.text_format(req), messages)
            except ValueError as e:
                raise ResponsesError(str(e).replace("response_format", "text.format"), "text.format") from None
            if validator is not None and tools:
                raise ResponsesError("a JSON text.format with tools is not supported", "text.format",
                                     "unsupported_parameter")
            noted = svc.__dict__.setdefault("hosted_tools_noted", set())   # said once per tool, not per request
            if set(skipped) - noted:
                print(f"[hypercore] /v1/responses: left out the hosted tools {', '.join(sorted(set(skipped) - noted))} "
                      f"(this server cannot run them)", flush=True)
                noted.update(skipped)
            if req.get("max_output_tokens") is None and "max_tokens" in svc.shared:   # the shared Chat settings
                req = {**req, "max_output_tokens": svc.shared["max_tokens"]}
            try:
                svc.reasoning_budget(req)                    # a bad value is a 400 before anything is sent
            except ValueError as e:
                raise ResponsesError(str(e), "reasoning_budget_tokens") from None
            try:
                stop_strings(req)
            except ValueError as e:
                raise ResponsesError(str(e), "stop") from None
            svc.load()
            try:
                ids, thinking, max_new = svc.prepare(messages, tools, kw, req.get("max_output_tokens") or 0)
            except ResponsesError:
                raise
            except ValueError as e:                          # too long for the context, an image without vision
                raise ResponsesError(str(e), "input", "context_length_exceeded" if "context" in str(e) else None) \
                    from None
            _debug_req("responses", req, messages, tools, max_new, thinking, len(ids))
            include = req.get("include") if isinstance(req.get("include"), list) else []
            asm = responses_api.Assembler(req, svc.model_for(req), len(ids), names,
                                          "reasoning.encrypted_content" in include, validator is not None)
            # the request is also the sampling dict, as on the chat path (temperature, top_p, top_k, seed, ...)
            return ids, thinking, tools, max_new, asm, validator, req, messages, kw

        def _count_tokens(self, req):
            """Anthropic's token count, which Claude Code asks for its context figures: the prompt this server would
            read for the same request, rendered and tokenized - the model does not run."""
            req = svc.with_shared(req, "anthropic")
            messages, tools, kw = anthropic_to_messages(req, svc.anthropic_think_unasked)
            stop_strings(req)                                 # the same 400 as the request itself would get
            self._json(200, {"input_tokens": len(svc.encode_prompt(messages, tools, kw))})

        def _anthropic(self, req):
            svc.load()
            req = svc.with_shared(req, "anthropic")
            messages, tools, kw = anthropic_to_messages(req, svc.anthropic_think_unasked)
            self._no_local_images(messages)
            if tool_choice_of(req.get("tool_choice"))[0] == "none":   # Anthropic's {"type": "none"}: no tools offered
                tools = None
            force = forced_call(req.get("tool_choice"), tools)        # "any" / {"type": "tool", "name": N}
            max_new = int(req.get("max_tokens") or 0)                  # 0/-1: the rest of the context
            svc.reasoning_budget(req)                         # a bad value is a 400 before anything is sent
            stop_strings(req)                                 # ... and so is a bad stop / stop_sequences
            ids, thinking, max_new = svc.prepare(messages, tools, kw, max_new, force=force)
            _debug_req("anthropic", req, messages, tools, max_new, thinking, len(ids))
            cancel = threading.Event()
            self._watch_client(cancel)                       # #430 #431
            run = svc.run(ids, thinking, tools, max_new, req, cancel, force=force)
            if svc.turn_tail_check and not force:            # the end-of-turn "is it done?" check
                run = with_tail_check(svc, run, messages, tools, kw, max_new, req, cancel,
                                      lambda m, i, th, mn: svc.run(i, th, tools, mn, req, cancel))
            events = anthropic_events(svc, req, ids, thinking, tools, max_new, cancel, force=force, run=run)
            events = self._capture(events, "anthropic")
            if not req.get("stream"):
                return self._json(200, anthropic_collect(events))
            self._sse()
            try:
                for item in events:
                    if item is None:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        name, e = item
                        self.wfile.write(f"event: {name}\n".encode() + b"data: " +
                                         json.dumps(e, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
            except OSError:
                self._note(outcome="disconnected")
                cancel.set()
                events.close()
            except EngineDied as e:                          # mid-stream: Anthropic's error event
                err = {"type": "error", "error": {"type": "api_error", "message": f"{e}; the next request restarts it"}}
                self._note(error=err["error"])
                self.wfile.write(b"event: error\ndata: " + json.dumps(err).encode() + b"\n\n")
            except ValueError as e:                          # the engine's ERR after the stream started
                err = {"type": "error", "error": {"type": "api_error", "message": str(e)}}
                self._note(error=err["error"])
                self.wfile.write(b"event: error\ndata: " + json.dumps(err).encode() + b"\n\n")

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is already serving, and requests then land on
    # either one (a forgotten second start of run-<model>.bat).  Without it the second start fails loudly instead.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):   # a client that hangs up needs no stack trace
            super().handle_error(request, client_address)


def warn_tight_ram(arena_mib) -> None:
    """The model's experts live in RAM (INFO arena_mib, engine 0.1.10+).  With less than ~6 GB left beside them for the
    system, the engine and this server, Linux ends the engine mid-answer when memory runs out (issue #27) and Windows
    pages to disk; say so at start instead of after a lost answer."""
    if not isinstance(arena_mib, int) or arena_mib <= 0:
        return
    try:
        import psutil
        total = psutil.virtual_memory().total
    except Exception:  # noqa: BLE001 - psutil is optional here
        return
    left = total / 2**30 - arena_mib / 1024
    if left < 6:
        print(f"[hypercore] WARNING: RAM is tight - the model's experts take {arena_mib / 1024:.1f} GB of this PC's "
              f"{total / 2**30:.0f} GB, leaving {left:.1f} GB for everything else. "
              + ("Linux may stop the engine in the middle of an answer. " if os.name != "nt" else
                 "Windows will slow down (paging to disk). ")
              + "Close other programs, or run START-HERE --setup and pick a smaller size (Q2_0 / IQ2_XS).", flush=True)


DESKTOP_FREE_MIB = 2048          # #560 #516: below this, an AMD card that also drives a Linux desktop can run out
DESKTOP_RESERVE_MIB = 3072       # what kept KDE/Wayland alive beside a full expert cache in both reports


def linux_desktop(env=None) -> bool:
    """A graphical session on Linux (Wayland or X): its compositor, browser and apps take VRAM after the model has."""
    env = os.environ if env is None else env
    return os.name != "nt" and sys.platform != "darwin" and bool(env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"))


def desktop_vram_note(backend, vram_free_mib, args: list, desktop: bool) -> str:
    """#560 #516: on Linux, when the desktop needs VRAM the AMD card does not have, amdgpu moves GPU memory (the expert
    cache, ~24 GB) to system RAM, which the experts already fill - the OOM killer then ends the compositor.  The
    default reserve (700 MiB) is sized for a card without a desktop.  A recommendation, nothing changes: "" when it
    does not apply."""
    if backend != "hip" or not desktop or not isinstance(vram_free_mib, int) or vram_free_mib >= DESKTOP_FREE_MIB:
        return ""
    try:
        reserve = int(args[args.index("--vram-reserve-mib") + 1]) if "--vram-reserve-mib" in args else 700
    except (ValueError, IndexError):
        reserve = 700
    if reserve >= DESKTOP_RESERVE_MIB:
        return ""
    return (f"[hypercore] note: {vram_free_mib} MiB of VRAM free with the model loaded. If this AMD card also drives your "
            "desktop and the desktop or apps crash after the start (the driver moves the expert cache to RAM and "
            "the OOM killer ends the session), keep more VRAM free: ./setup.sh --vram-reserve-mib "
            f"{DESKTOP_RESERVE_MIB} (remembered; the expert cache gets "
            f"{(DESKTOP_RESERVE_MIB - reserve) / 1024:.1f} GB less, a few % of speed)")










def allowed_hosts_of(value, env: str = "") -> list[str]:
    """The config's allowed_hosts (a name or a list) plus $HYPERCORE_ALLOWED_HOSTS (comma-separated): host names, "*" or
    ".example.com" (it and every name below it).  A scheme, port or path is dropped ("https://a.example.com:8443/"
    -> "a.example.com"); a wrong entry stops the start (ValueError)."""
    items = [] if value in (None, "") else [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise ValueError("allowed_hosts: expected a host name or a list of them")
    out = []
    for raw in items + [x for x in env.split(",") if x.strip()]:
        x = raw.strip().lower()
        if x == "*":
            out.append(x)
            continue
        x = x.split("://", 1)[-1].split("/", 1)[0]
        dot = x.startswith(".")
        name = host_name(x[1:] if dot else x)
        if not name:
            raise ValueError(f"allowed_hosts: {raw!r} is not a host name like hypercore.example.com")
        out.append("." + name if dot else name)
    return list(dict.fromkeys(out))








def close_everything(svc: Service, httpd) -> list[str]:
    """#641: stop every process this server started, in the order that lets each one free
    its own memory before the next is stopped. Ctrl+C, SIGTERM and the web page's Shut
    Down button all come through here, so a shutdown from the page and one from the
    terminal are the same code path - and it is plain Python, with no signals and no
    process scanning, so it behaves identically on Windows and Linux.

    Returns the names it closed, for the log and the tests."""
    closers = [("the HTTP server", httpd.shutdown),
               ("the model engine", getattr(svc.engine, "close", None)),
               ("the image encoder", svc.vision.shutdown if svc.vision else None),
               ("the MCP servers", svc.mcp.close if getattr(svc, "mcp", None) is not None else None)]
    closed: list[str] = []
    for name, close in closers:
        if close is None:
            continue
        try:
            close()
        except KeyboardInterrupt:                   # a second Ctrl+C: don't wait for the engine to free its memory
            proc = getattr(svc.engine, "proc", None)
            if proc:
                proc.kill()
        closed.append(name)
    return closed


def serve(svc: Service, host="127.0.0.1", port=8095) -> ThreadingHTTPServer:
    svc.host_names = host_names_for(host, svc.allowed_hosts, svc.trusted_origins)
    svc.start_telemetry()
    httpd = Server((host, port), make_handler(svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def origins_of(value, key: str, wildcard: bool) -> list[str]:
    """#321: a config's origin list ("https://chat.example.com" or a list of them) - scheme://host[:port], no path;
    "*" only where `wildcard` allows it.  A wrong entry stops the start rather than opening less or more than meant."""
    if value is None or value == "" or value == []:
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise SystemExit(f"[hypercore] {key}: expected an origin or a list of origins")
    out = []
    for x in items:
        x = x.strip().rstrip("/")
        if x == "*" and wildcard:
            out.append(x)
            continue
        scheme, sep, rest = x.partition("://")
        if scheme not in ("http", "https") or not sep or not rest or "/" in rest or "*" in rest:
            raise SystemExit(f"[hypercore] {key}: {x!r} is not an origin like https://chat.example.com"
                             + ("" if wildcard else " (no wildcards here)"))
        out.append(x)
    return out


SHARED_KEYS = ("reasoning_effort", "temperature", "top_p", "top_k", "seed", "max_tokens", "experimental_speed_projection")


def clean_shared_defaults(d) -> dict:
    """The Chat settings other apps get (POST /settings): only known keys, each checked; ValueError names a bad one."""
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ValueError("defaults must be an object")
    out = {}
    for key, value in d.items():
        if value is None or value == "":
            continue
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if key == "reasoning_effort":
            if value not in ("none", "low", "medium", "high"):
                raise ValueError("reasoning_effort: none, low, medium or high")
        elif key == "temperature":
            if not number or not 0 <= value <= 2:
                raise ValueError("temperature: 0..2")
        elif key == "top_p":
            if not number or not 0 < value <= 1:
                raise ValueError("top_p: 0 < top_p <= 1")
        elif key == "top_k":
            if not number or value != int(value) or not 1 <= value <= 64:
                raise ValueError("top_k: an integer 1..64")
            value = int(value)
        elif key in ("seed", "max_tokens"):
            if not number or value != int(value) or value <= 0:
                raise ValueError(f"{key}: a positive integer")
            value = int(value)
        elif key == "experimental_speed_projection":
            if not isinstance(value, bool):
                raise ValueError("experimental_speed_projection: true or false")
        else:
            raise ValueError(f"unknown setting {key!r}")
        out[key] = float(value) if key in ("temperature", "top_p") else value
    return out


def sampling_defaults_from_config(cfg: dict) -> dict:
    """The run config's optional `sampling` block: defaults for the sampling fields a request leaves out, so
    a plain client gets configured sampling instead of greedy.  Supported: temperature, top_p, top_k, min_p,
    presence_penalty, repetition_penalty, frequency_penalty, penalty_last_n, seed.  The request's own fields
    always win - an explicit temperature=0 still means greedy, a field set to null falls back to the default.
    A bad value refuses to start the server (a typo'd config should not quietly change sampling); unknown keys
    are named at startup and ignored."""
    out = {}
    for key, value in (cfg.get("sampling") or {}).items():
        if value is None:
            continue
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if key == "temperature":
            if not number or value < 0:
                raise SystemExit(f"[hypercore] config sampling.temperature={value!r}: expected a number >= 0 (0 = greedy)")
            out[key] = float(value)
        elif key == "top_p":
            if not number or not 0 < value <= 1:
                raise SystemExit(f"[hypercore] config sampling.top_p={value!r}: expected 0 < top_p <= 1")
            out[key] = float(value)
        elif key == "min_p":
            if not number or not 0 <= value <= 1:
                raise SystemExit(f"[hypercore] config sampling.min_p={value!r}: expected 0 <= min_p <= 1")
            out[key] = float(value)
        elif key == "top_k":
            if not number or value != int(value) or not 1 <= value <= 64:
                raise SystemExit(f"[hypercore] config sampling.top_k={value!r}: the sampled path takes an integer 1..64")
            out[key] = int(value)
        elif key == "presence_penalty":
            if not number or value < 0:
                raise SystemExit(f"[hypercore] config sampling.presence_penalty={value!r}: expected a number >= 0")
            out[key] = float(value)
        elif key == "frequency_penalty":
            if not number or value < 0:
                raise SystemExit(f"[hypercore] config sampling.frequency_penalty={value!r}: expected a number >= 0")
            out[key] = float(value)
        elif key == "repetition_penalty":
            if not number or value <= 0:
                raise SystemExit(f"[hypercore] config sampling.repetition_penalty={value!r}: expected a number > 0 (1 = off)")
            out[key] = float(value)
        elif key == "penalty_last_n":
            if not number or value != int(value) or value < 0:
                raise SystemExit(f"[hypercore] config sampling.penalty_last_n={value!r}: expected a non-negative integer")
            out[key] = int(value)
        elif key == "seed":
            if not number or value != int(value) or value <= 0:
                raise SystemExit(f"[hypercore] config sampling.seed={value!r}: expected a positive integer")
            out[key] = int(value)
        elif key == "experimental_speed_projection":
            if not isinstance(value, bool):
                raise SystemExit(f"[hypercore] config sampling.experimental_speed_projection={value!r}: expected true or "
                                 "false (the default for requests that leave it out, when the engine has the vector)")
            out[key] = value
        else:
            print(f"[hypercore] config sampling.{key}={value!r}: unknown key, ignored", flush=True)
    return out


MAX_CONFIG_BYTES = 10 * 1024 * 1024   # the ceiling hypercore_engine_init also enforces


def verbatim_object_slice(text: str, key: str) -> str | None:
    """The exact characters of `"key": { ... }` in the original document.

    Braces are matched on the raw text, so indentation, key order and spacing survive.
    Returns None when the key is absent or the object is malformed.
    """
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', text)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def resolve_configuration_payload(config_input: str | None) -> str:
    """Return the configuration text the worker will receive, unmutated.

    Inline JSON is forwarded verbatim: whitespace, indentation and key order are kept, so
    a deployment that hashes its config still matches the payload the worker parses.
    A path is read here, once - the worker is never handed a path. The 10 MB ceiling is
    checked before anything is buffered, and an empty file is an error rather than a
    silent fall back to defaults.

    Raises ValueError for an empty file, an oversized payload or a non-object document,
    and FileNotFoundError when the path cannot be opened.
    """
    if not config_input:
        return ""
    if not config_input.strip():
        return ""                      # a blank argument is no configuration, not a bad path

    payload_bytes = len(config_input.encode("utf-8"))
    if payload_bytes > MAX_CONFIG_BYTES:
        raise ValueError(
            f"configuration is {payload_bytes} bytes; the ceiling is {MAX_CONFIG_BYTES}"
        )

    if is_inline_config(config_input):
        json.loads(config_input)          # validate, but never re-serialise
        return config_input               # verbatim

    path = Path(config_input.strip())
    try:
        size = path.stat().st_size
    except OSError as e:
        raise FileNotFoundError(f"configuration file not readable: {path} ({e})") from e
    if size == 0:
        raise ValueError(
            f"configuration file {path} is empty; refusing to run on hidden defaults"
        )
    if size > MAX_CONFIG_BYTES:
        raise ValueError(
            f"configuration file {path} is {size} bytes; the ceiling is {MAX_CONFIG_BYTES}"
        )

    text = path.read_text(encoding="utf-8-sig")   # Notepad adds a BOM
    if not is_inline_config(text):
        raise ValueError(f"configuration file {path} does not contain a JSON object")
    return text


def parse_config_object(text: str) -> dict:
    """Validate a configuration payload as an object, without rewriting it."""
    if not text:
        return {}
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"configuration is not valid JSON: {e}") from e
    if not isinstance(cfg, dict):
        raise ValueError("configuration must be a JSON object")
    return cfg


def worker_binary_path(root: Path = ROOT) -> str:
    """The worker the server spawns.

    build-shared is the canonical build (shared library plus the thin worker), but a
    cleaned tree leaves the same worker in build/ - its RUNPATH points at that build, so
    it loads the same libhypercore_api.so.0. Candidates are tried in that order; the
    canonical name is returned when none exists, so the error still names what to build.
    """
    for path in (root / "build-shared" / "hypercore-worker",
                 root / "build" / "hypercore-worker",
                 root / "engine" / "hypercore-worker"):
        if path.is_file():
            return str(path)
    return str(root / "build-shared" / "hypercore-worker")


def hypercore_worker_config(cfg: dict, raw_config: str = "") -> str:
    """Build the JSON config passed to hypercore-worker.

    The old text engine used CLI arguments. The worker boundary uses the same numeric
    knobs, but as an instance config. Parse the existing config args first, then fall
    back to top-level config keys. Unknown keys are ignored by the bridge.
    """
    args = cfg.get("args") or []

    def flag_value(flags, default=None):
        for i, arg in enumerate(args):
            if arg in flags and i + 1 < len(args):
                return args[i + 1]
        return default

    worker = cfg.get("worker_config") or cfg.get("ipc_config") or {}
    if worker:
        # Forward the block's exact characters. Re-serialising it would change spacing
        # and key order, which breaks a config-hash audit against the deployment spec.
        key = "worker_config" if "worker_config" in cfg else "ipc_config"
        verbatim = verbatim_object_slice(raw_config, key)
        return verbatim if verbatim is not None else json.dumps(worker)

    batch = flag_value(["--batch", "--slots"], cfg.get("parallel") or cfg.get("batch") or 4)
    pool = flag_value(["--pool-workers", "--threads"], cfg.get("pool_workers") or cfg.get("workers") or -1)
    ctx = flag_value(["--ctx-size", "--context", "--max-context"], cfg.get("max_context") or cfg.get("n_ctx") or 4096)
    out = {"batch": int(batch), "pool_workers": int(pool), "max_context": int(ctx)}

    pcie = flag_value(["--pcie-frac"], cfg.get("pcie_frac"))
    if pcie is not None:
        out["pcie_frac"] = float(pcie)

    spec = flag_value(["--spec-min-p"], cfg.get("spec_min_p"))
    if spec is not None:
        out["spec_min_p"] = float(spec)

    # The model itself. Without these the worker is the fixture; with them it starts the real
    # engine as a child and every token and timing on the wire comes from that engine.
    exe = cfg.get("exe")
    if exe:
        if not os.path.isabs(exe):
            exe = os.path.abspath(os.path.join(cfg.get("cwd") or ".", exe))
        out["engine_exe"] = exe
        out["engine_args"] = list(args)
        startup = cfg.get("startup_timeout_s") or cfg.get("model_startup_s")
        if startup:
            out["model_startup_s"] = float(startup)

    return json.dumps(out)

def is_inline_config(raw: str) -> bool:
    """The same rule the C boundary uses: the first non-whitespace character decides."""
    return bool(raw) and raw.strip().startswith("{")


def run_configs() -> list[Path]:
    """Every run config in config/, newest first.

    The build manifest is not a run config and is left out, so a directory that holds
    only it still counts as having no config to run.
    """
    if not (ROOT / "config").is_dir():
        return []
    out = [p for p in (ROOT / "config").glob("*.json") if p.name != "build_options.json"]
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def discover_config() -> Path | None:
    """Which config to run when --config is not given: a run-*.json first, then any other.

    A config is named by the model it runs, so the name is the user's to choose; the
    run- prefix is only a hint about which one was written by the Tune export.
    """
    named = [p for p in run_configs() if p.name.startswith("run-")]
    return (named or run_configs() or [None])[0]


def config_error(message: str) -> str:
    """A missing config says what is in config/ rather than only what is not there."""
    found = run_configs()
    if not found:
        return message
    listing = ", ".join(str(p.relative_to(ROOT)) for p in found)
    return f"{message}\n  configs in config/: {listing}"


def engine_binary(cfg: dict) -> str | None:
    """Where the engine is: the run config's "exe", then engine/strata in this project,
    then $STRATA_ENGINE.

    A relative exe is resolved against the config's cwd: Windows' CreateProcess resolves
    "engine/strata.exe" against nothing it is told about (WinError 2), so it is made
    absolute here rather than left for the OS to guess.
    """
    exe = cfg.get("exe")
    if exe:
        return exe if os.path.isabs(exe) else os.path.abspath(os.path.join(cfg.get("cwd") or ".", exe))
    for candidate in (ROOT / "engine" / ("strata.exe" if os.name == "nt" else "strata"),
                      os.environ.get("STRATA_ENGINE")):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def point_engine_at_config(engine: Any, cfg: dict[str, Any]) -> None:
    """Give an engine object the run config it should run next.

    Every engine the server can hold - the worker supervisor, serve/direct_engine.py,
    an attached engine - keeps its own copy of what to launch, so the switch is done
    by attribute presence rather than by type: a name the object does not have is left
    alone.
    """
    if hasattr(engine, "config"):
        engine.config = hypercore_worker_config(cfg, json.dumps(cfg))
    if hasattr(engine, "env"):
        engine.env = child_env(cfg)
    if hasattr(engine, "exe"):
        exe = engine_binary(cfg)
        if exe:
            engine.exe = exe
    if hasattr(engine, "args"):
        args = list(cfg.get("args") or [])
        if getattr(engine, "args", []) and engine.args and engine.args[-1] == "--serve":
            args.append("--serve")
        engine.args = args


def apply_model_version(svc: Service, entry: dict[str, Any]) -> tuple[bool, str]:
    """Point the service at one of the stored engine builds and restart the engine.

    The binaries are already in place (serve/quant_store.py:materialise put them at
    engine/strata); this updates what the engine is told to run. A version switch that
    fails leaves the app answering: the HTTP server is never touched, and the previous
    engine stays where it was.
    """
    engine = getattr(svc, "engine", None)
    if engine is None:
        return False, "there is no engine to switch"
    cfg: dict[str, Any] | None = None
    if entry.get("config"):
        try:
            cfg = runconfig.load(entry["config"])
        except (OSError, ValueError) as exc:
            return False, f"the run config of {entry.get('key')} could not be read: {exc}"
        with svc.config_lock:
            svc.config_path = entry["config"]
        svc.model = cfg.get("model_name") or svc.model
        svc.sampling_defaults = sampling_defaults_from_config(cfg)
        point_engine_at_config(engine, cfg)
        # The retry thread holds the engine that failed to start; it must not bring
        # back the version the user just replaced.
        candidate = getattr(getattr(svc, "engine_retry", None), "candidate", None)
        if candidate is not None:
            point_engine_at_config(candidate, cfg)
    if isinstance(engine, OfflineEngine):
        # Nothing is running, so there is nothing to restart: the version is set, and
        # the retry thread or the Load button is what brings it up.
        return True, (f"{entry.get('label')} is set as the model version. "
                      "Load it (the button in the Chat tab, or POST /v1/start) to run it.")
    try:
        unload = getattr(engine, "unload", None)
        if callable(unload):
            unload()
        svc.engine_stopped = False
        ok = svc.start_engine()
    except Exception as exc:  # noqa: BLE001 - a version switch is reported, not raised
        return False, f"the engine did not come back: {exc}"
    return ok, (f"{entry.get('label')} is loading" if ok
                else "that version did not start: see the engine log")


def engine_url(cfg: dict, given: str | None) -> str | None:
    """An engine the user told us to attach to: --engine-url, the config's engine_url,
    or $HYPERCORE_ENGINE_URL. None when nobody named one."""
    for url in (given, cfg.get("engine_url"), cfg.get("attach_url"), os.environ.get("HYPERCORE_ENGINE_URL")):
        if url and str(url).strip():
            url = str(url).strip()
            return url if url.startswith("http") else "http://" + url
    return None


# The ports an engine is usually on, tried after the port the run config names.
USUAL_ENGINE_PORTS = (8080, 8081, 8095)


def engine_is_loaded(body: str) -> bool:
    """Does a /health answer say a model is loaded behind it?

    "answers at all" is not enough. An engineless WebUI answers the same route with
    `"max_context": 0`, so a server that attaches to the first thing that replies can
    end up attached to itself: the "engine" is then this very WebUI, every chat request
    is refused by the same refusal, and the banner never clears. The JSON is read, not
    grepped, so the check is the answer's own claim of a loaded model.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return '"status": "ok"' in body or '"loaded": true' in body
    if not isinstance(data, dict):
        return False
    context = data.get("max_context")
    return bool(data.get("loaded")) or (isinstance(context, int) and context > 0)


def find_running_engine(cfg: dict, own_port: int,
                        ports: tuple[int, ...] | None = None) -> str | None:
    """Is an engine already up on this machine?

    A second copy of the same model would not fit, so before starting one the server
    asks the port the config names (and the usual ones) for a /health. The first one
    that answers with a loaded model is the engine to attach to.

    `ports` overrides which ports are tried; it is there so a test can point the probe
    at one port instead of the machine's usual ones.
    """
    import socket
    wanted = list(ports) if ports else [cfg.get("port"), *USUAL_ENGINE_PORTS]
    candidates = [p for p in wanted if isinstance(p, int) and p != own_port]
    for port in dict.fromkeys(candidates):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3.0) as s:
                s.sendall(b"GET /health HTTP/1.0\r\n\r\n")
                # One recv gets the headers and stops there; the JSON answer is the next packet.
                raw = b""
                while len(raw) < 16384:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                body = raw.decode("utf-8", "replace")
        except OSError:
            continue
        if engine_is_loaded(body):
            return f"http://127.0.0.1:{port}"
    return None


def load_config_text(raw: str | None) -> dict:
    """Read --config, which may be a path to a JSON file or an inline JSON object.

    Decided by the first character, not by extension or by try/except, so the CLI and
    hypercore_engine_init cannot disagree about the same argument. A path that cannot be
    read, an empty file, or a payload over the ceiling is an error, not an empty config.
    """
    try:
        return parse_config_object(resolve_configuration_payload(raw))
    except (ValueError, FileNotFoundError) as e:
        raise SystemExit(f"[hypercore] {e}") from e


def mcp_yaml() -> str:
    """The MCP config this project reads when --mcp-config is not given.

    `mcp.yaml` beside the server first (where a Strata box keeps it), then `config/mcp.yaml`
    (where HyperCore-WebUI keeps it, so the project root stays free of loose files). The root
    path is returned when neither exists, so a missing file is simply no servers.
    """
    for candidate in (ROOT / "mcp.yaml", ROOT / "config" / "mcp.yaml"):
        if candidate.is_file():
            return str(candidate)
    return str(ROOT / "mcp.yaml")


def _env_off(name: str, env: dict[str, str]) -> bool:
    """True when an install-wide switch is set to a value that means off."""
    return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def resolve_turn_tail(cli_flag: bool, cfg: dict, env: dict[str, str] | None = None) -> bool:
    """Whether the end-of-turn "is the work done?" check runs for this server.

    Order, highest first: `HYPERCORE_NO_TURN_TAIL_CHECK` (a whole install, whatever
    every run config says), then `--turn-tail-check`, then `"turn_tail_check": true`
    in the model's run config. Off unless one of them says otherwise.
    """
    env = os.environ if env is None else env
    if _env_off("HYPERCORE_NO_TURN_TAIL_CHECK", env):
        return False
    return bool(cli_flag or cfg.get("turn_tail_check") is True)


def engine_from_active_config(config_path: str | None = None, own_port: int | None = None,
                              api_key: str = "", tokenizer: Any = None) -> Any | None:
    """The engine the active run config names now, or None while nothing is there.

    A server started engineless reads the run config once, at startup, and never again: a
    build that installs an engine is not seen, and the app stays engineless until it is
    started again. This is the same read at attempt time, so a build is adopted without
    taking the web app down - the Build tab puts the engine in engine/engine_<quant>/,
    writes config/run-<quant>.json, and activates it.

    The worker is HyperCore's process boundary, so a config whose worker exists is built
    the way the startup path builds it; otherwise the engine is run directly. Returns None
    while there is no run config or no engine binary - that is not an error, it is the
    state before the first build.

    `config_path` is the run config this server was started with. Given, it is the one to
    re-read: a build rewrites it, and a server named for a model should not silently pick
    a different one. Not given (a fresh clone, or a --ui-only start with no config) means
    the version the store says is active.

    `own_port` is the port this web app is listening on. Given, an engine already up on
    the machine is adopted over HTTP rather than started a second time, exactly as the
    startup path does; the server's own port is never a candidate, so a deferred engine
    can never become the web app it is running in. Not given (a read with no server yet, or
    a --no-attach start) means no attach check.
    """
    run_cfg: dict[str, Any] | None = None
    raw = ""
    if config_path:
        try:
            raw = Path(config_path).read_text(encoding="utf-8-sig")
            run_cfg = json.loads(raw)
        except (OSError, ValueError):
            run_cfg = None
    if run_cfg is None:
        configs = quant_store._run_configs()
        if not configs:
            return None
        active = quant_store.index().get("active")
        chosen = next((c for c in configs if c["quant"] == active), configs[0])
        try:
            raw = Path(chosen["path"]).read_text(encoding="utf-8-sig")
            run_cfg = json.loads(raw)
        except (OSError, ValueError):
            return None
    if not run_cfg.get("exe") and not run_cfg.get("worker"):
        # Nothing in the config names an engine. `engine_binary` would hand back this
        # project's engine/strata - the fallback for a machine where one was installed by
        # hand - but a deferred engine is asked for because a build produced one, and a
        # config that names nothing is the state before the first build, not an engine to
        # adopt. Adopting it would load a model the person never asked for.
        return None
    exe = engine_binary(run_cfg)
    if not exe or not Path(exe).is_file():
        # engine_binary hands back the path the config names without looking at it, so an
        # absolute exe left by another install, or by a build that was cleaned, comes back
        # here even though nothing is on disk. The read is asked at attempt time: an engine
        # that is not there is the state before the first build, not an engine to adopt.
        return None
    if own_port is not None:
        url = find_running_engine(run_cfg, own_port)
        if url:
            from serve.attached_engine import AttachedEngine
            return AttachedEngine(url, api_key=api_key, tokenizer=tokenizer,
                                  model=run_cfg.get("model_name") or "")
    worker = run_cfg.get("worker") or worker_binary_path()
    if not Path(worker).is_absolute():
        worker = str(Path(run_cfg.get("cwd") or ".").resolve() / worker)
    if Path(worker).is_file():
        try:
            supervisor = HyperCoreProcessSupervisor(
                worker_binary=worker,
                socket_path=unique_socket_path("hypercore-worker"),
                config=hypercore_worker_config(run_cfg, raw),
                stderr_log=run_cfg.get("log"),
                startup_timeout_s=model_startup_s(run_cfg),
                silence_s=engine_silence_s(run_cfg),
                env=run_cfg.get("env") or {},
            )
        except (ValueError, OSError):
            return None
        # The Engine surface the server uses is the adapter, not the process owner: the
        # same wrapper main() builds. The supervisor has no status() and no max_context of
        # its own - those are the adapter's, and the health route reads them.
        return HyperCoreRequestHandler(supervisor)
    # imported here, as the other build paths do
    from serve.direct_engine import DirectEngine
    return DirectEngine(exe, run_cfg.get("args") or [],
                        startup_timeout_s=model_startup_s(run_cfg),
                        env=child_env(run_cfg), log_path=run_cfg.get("log"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["mock", "hypercore", "strata", "attach"], default="mock",
                    help="mock: a scripted answer, for clients and tests; hypercore: the built worker; "
                         "strata: engine/strata over its stdin/stdout protocol; attach: an engine that is "
                         "already running, reached over its HTTP API (one engine per machine)")
    ap.add_argument("--engine-url", dest="engine_url", default=os.environ.get("HYPERCORE_ENGINE_URL"),
                    help="the address of an engine already running, e.g. http://127.0.0.1:8080 - the WebUI "
                         "attaches to it instead of starting a second copy of the model (also \"engine_url\" "
                         "in the config, and $HYPERCORE_ENGINE_URL)")
    ap.add_argument("--no-attach", action="store_true",
                    help="start this project's own engine even when one is already loaded elsewhere "
                         "(by default the WebUI attaches to the engine it finds, rather than asking the "
                         "machine to hold a second copy of the model)")
    ap.add_argument("--config", help="hypercore engine config (JSON: exe, args, cwd, tokenizer, model_name), "
                                     "written by setup.py")
    ap.add_argument("--host", default=None,
                    help="the address to listen on: 127.0.0.1 = this PC only (the default), 0.0.0.0 = also other devices "
                         "on your network (set an API key); also \"host\" in the config")
    ap.add_argument("--script", action="append",
                    help="the mock engine's answer (default: a short greeting); given more than once, requests get "
                         "them in turn and the last one repeats")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--gpu", help="the GPU to run on, as nvidia-smi numbers them, or several for a layer split "
                                  "(\"0,2\"; also \"gpu\" in the config)")
    ap.add_argument("--tokenizer", default=str(ROOT / "pack/full/tokenizer"),
                    help="pack tokenizer directory (falls back to a byte tokenizer if absent)")
    ap.add_argument("--open", action="store_true", help="open the local page in the browser once the model is ready")
    ap.add_argument("--fit-max-tokens", action="store_true",
                    help="clamp max_tokens to the remaining context instead of rejecting the request "
                         "(default: reject with 400, like llama.cpp; also \"fit_max_tokens\": true in the config)")
    ap.add_argument("--trim-context", action="store_true",
                    help="when a prompt alone exceeds the context, drop the oldest turns and head/tail-truncate "
                         "the newest instead of rejecting it with 400 (default: reject; also \"trim_context\": "
                         "true in the config)")
    ap.add_argument("--api-key", default=os.environ.get("HYPERCORE_API_KEY", ""),
                    help="require this key on /v1/* (Authorization: Bearer ... or x-api-key); also $HYPERCORE_API_KEY")
    ap.add_argument("--mcp-config", help="a JSON file with MCP servers in Claude Desktop's format ({\"mcpServers\": "
                                         "{...}}) or a mcp.yaml; the web app's chat can use their tools (also "
                                         "\"mcp_servers\" in the config, and the mcp.yaml beside this server or "
                                         "config/mcp.yaml)")
    ap.add_argument("--lazy", action="store_true", help="start the text-only API unloaded; load on first request")
    ap.add_argument("--ui-only", action="store_true",
                    help="serve the app without starting an engine: the page, the Monitor, the Tune and Build tabs "
                         "stay usable while the GPU is held by a sweep, a build or another server. A request that "
                         "needs the model is answered 503 with a Retry-After.")
    ap.add_argument("--engine-retry-s", type=float, default=None,
                    help="seconds between attempts to bring an engine back when it could not start "
                         "(default 30; \"engine_retry_s\" in the config; 0 turns the retry off)")
    ap.add_argument("--api-monitor", action="store_true",
                    help="the API request monitor at /api-monitor: keeps the last 100 requests' prompts and answers in "
                         "memory (also \"api_monitor\": true in the config; off by default)")
    ap.add_argument("--turn-tail-check", action="store_true",
                    help="at the end of a turn, ask the model whether every task in the request is done: a bare "
                         "confirmation ends the turn, a no or more work keeps it going (also turn_tail_check: true "
                         "in the config; off by default; HYPERCORE_NO_TURN_TAIL_CHECK turns it off for a whole "
                         "install whatever the flag or the config says)")
    ap.add_argument("--turn-tail-checks", type=int, default=None, metavar="N",
                    help="how many times one turn may be asked (default 3; also \"turn_tail_checks\" in the config)")
    ap.add_argument("--idle-unload", type=float, default=None, metavar="SECONDS",
                    help="unload the model after this many seconds without requests, so other programs (games, other "
                         "model servers) can use the VRAM; the next request loads it again (also \"idle_unload_s\" "
                         "in the config; default: never)")
    ap.add_argument("--min-free-vram-mib", type=int, default=None,
                    help="load an unloaded model only when this much VRAM is free, else answer 503 (also "
                         "\"min_free_vram_mib\" in the config; default: always load)")
    ap.add_argument("--before-load", help="a command run before the model is loaded again (e.g. to unload another "
                                          "server's model; also \"before_load\" in the config, a string or a list)")
    ap.add_argument("--slot-save-path", default=None, metavar="DIR",
                    help="enable POST /slots/0?action=save|restore {\"filename\": NAME} (llama-server's API): the "
                         "conversation the engine holds, to or from DIR/NAME (also \"slot_save_path\" in the config)")
    ap.add_argument("--worker", default=None, help="hypercore-worker binary; default build-shared/hypercore-worker")
    ap.add_argument("--ipc-socket", default=None, help="UNIX socket path for the worker; default a namespaced /tmp/hypercore-worker-<pid>.sock")
    a = ap.parse_args()
    if a.config is None:              # no --config: the newest run config in config/, if there is one
        found = discover_config()
        if found:
            print(f"[hypercore] no --config: using {found.relative_to(ROOT)}", flush=True)
            a.config = str(found)
    try:                              # a path or inline JSON; a bad path is an error, not defaults
        config_text = resolve_configuration_payload(a.config)
    except (ValueError, FileNotFoundError) as e:
        ap.error(config_error(str(e)))
    cfg = parse_config_object(config_text)
    if a.gpu is not None:
        cfg["gpu"] = int(a.gpu) if a.gpu.strip().isdigit() else a.gpu
    a.host = a.host or cfg.get("host") or "127.0.0.1"   # issue #26: the run scripts pass no --host, the config can
    try:                                                # before the minutes of loading: is the port free?
        Server((a.host, a.port), BaseHTTPRequestHandler).server_close()
    except OSError as e:
        ap.error(listen_problem(a.host, a.port, e))
    if cfg.get("tokenizer"):
        a.tokenizer = cfg["tokenizer"]
    tok = ByteTokenizer()
    tpath = Path(a.tokenizer)
    if a.engine == "hypercore" and not (tpath / "vocab.json").exists():
        ap.error(f"the model's tokenizer is missing ({tpath / 'vocab.json'}); run setup again")
    if (tpath / "vocab.json").exists():
        import hypercore_tokenizer as ST
        vocab = json.loads((tpath / "vocab.json").read_text(encoding="utf-8"))
        tokens = [None] * len(vocab)
        for t, i in vocab.items():
            tokens[i] = t
        merges = [m for m in (tpath / "merges.txt").read_text(encoding="utf-8").split("\n") if m.strip()]
        types = json.loads((tpath / "token_type.json").read_text())
        tok = ST.Tokenizer(tokens, merges, types)
    hub = hub_from_config(cfg, a.mcp_config, yaml_path=mcp_yaml())   # before the minutes of loading: a bad entry stops here
    attach_url = engine_url(cfg, a.engine_url)
    if a.engine == "attach" and not attach_url:
        ap.error("--engine attach needs --engine-url (or \"engine_url\" in the config, or $HYPERCORE_ENGINE_URL)")
    if not attach_url and not a.no_attach and a.engine in ("strata", "hypercore"):
        found = find_running_engine(cfg, a.port)
        if found:
            why = "no engine in engine/" if not engine_binary(cfg) else "one engine per machine"
            print(f"[hypercore] an engine is already loaded at {found} ({why}) - attaching to it "
                  "instead of starting a second copy. Use --no-attach to start this project's own.",
                  flush=True)
            attach_url = found
    engine, engine_candidate, engine_unavailable = None, None, None
    if a.ui_only:
        # The GPU is somewhere else: a sweep, a build, another server. The app still runs;
        # every route that does not need weights answers, and a chat is refused with a reason.
        vision = None
        sampling_defaults = sampling_defaults_from_config(cfg)
        effort_end = None

        def deferred_engine() -> Any | None:
            """What this install can run now, read at the moment it is asked for.

            A server started engineless read its run config once, at startup, and never
            looked again: a build that put an engine in engine/engine_<quant>/ was invisible
            until the server was started by hand. This is read on every attempt, so the
            build is adopted without taking the web app down - and an engine already up on
            the machine is attached to rather than started a second time, with this server's
            own port excluded so the engine can never be the web app itself.
            """
            return engine_from_active_config(a.config,
                                            own_port=None if a.no_attach else a.port,
                                            api_key=a.api_key or cfg.get("api_key", ""),
                                            tokenizer=tok)

        engine = DeferredEngine(deferred_engine,
                               "started with --ui-only: no engine was asked for",
                               model=cfg.get("model_name") or "no model loaded")
        engine_candidate, engine_unavailable = engine, engine.reason
        print("[hypercore] the web app is up without an engine: the Chat, Monitor, Tune, "
              "Build and About tabs work. A chat request is answered 503 until an engine is "
              "loaded (POST /v1/load, or the Load button in the Chat tab).", flush=True)
    elif attach_url:
        # One engine per machine: the WebUI runs against the engine another server
        # already loaded, over the OpenAI API that engine's server speaks.
        from serve.attached_engine import AttachedEngine
        vision = None
        sampling_defaults = sampling_defaults_from_config(cfg)
        engine = AttachedEngine(attach_url, api_key=a.api_key, tokenizer=tok,
                               model=cfg.get("model_name") or "")
        print(f"attaching to the engine at {attach_url} ...", flush=True)
        try:
            engine.start()
        except Exception as exc:  # noqa: BLE001 - the engine may not be up yet
            engine_candidate, engine, engine_unavailable = engine, OfflineEngine(
                f"the engine at {attach_url} is not answering: {exc}",
                model=cfg.get("model_name") or "no model loaded"), str(exc)
            print(f"[hypercore] cannot attach to {attach_url}: {exc}", flush=True)
            print(f"[hypercore] the web app is up anyway; it will try that engine again in the "
                  f"background every {engine.retry_after_s:g} s.", flush=True)
        else:
            print(f"attached: {engine.model}, context {engine.max_context} tokens "
                  f"(this server does not own that engine: stop it from its own page)", flush=True)
        effort_end = None
    elif a.engine == "strata":
        exe = engine_binary(cfg)
        if not exe:
            ap.error('no engine to run: put one at engine/strata, set STRATA_ENGINE, '
                     'or give a run config with an "exe"')
        from serve.direct_engine import DirectEngine
        vision = None
        sampling_defaults = sampling_defaults_from_config(cfg)
        engine = DirectEngine(exe, cfg.get("args") or [],
                             startup_timeout_s=model_startup_s(cfg),
                             env=child_env(cfg), log_path=cfg.get("log"))
        print("starting the Strata engine ...", flush=True)
        try:
            engine.start()
        except Exception as exc:  # noqa: BLE001 - the engine may not be installed yet
            engine_candidate, engine, engine_unavailable = engine, OfflineEngine(
                str(exc), model=cfg.get("model_name") or "no model loaded"), str(exc)
            print(f"[strata] {exc}", flush=True)
            print(f"[hypercore] the web app is up anyway: the GPU is free for a sweep or a "
                  f"build, and the engine is retried in the background every "
                  f"{engine.retry_after_s:g} s.", flush=True)
        effort_end = None
    elif a.engine == "hypercore":
        if not cfg:
            ap.error("--engine hypercore needs --config")
        vision = None
        env = child_env(cfg)
        sampling_defaults = sampling_defaults_from_config(cfg)
        if sampling_defaults:
            pretty = ", ".join(f"{k}={v}" for k, v in sampling_defaults.items())
            print(f"[hypercore] sampling defaults from the config: {pretty}", flush=True)
        lazy = a.lazy or cfg.get("lazy_load") is True
        if lazy and cfg.get("vision"):
            ap.error("lazy loading is text-only; disable vision in the config")
        if cfg.get("vision"):
            print("loading the vision encoder ...", flush=True)
            # relative paths are the config's cwd's, as for the engine below
            vcfg = {k: (os.path.abspath(os.path.join(cfg.get("cwd") or ".", v))
                        if k in ("exe", "mmproj", "model") and isinstance(v, str) and not os.path.isabs(v) else v)
                    for k, v in cfg["vision"].items()}
            vision = Vision(vcfg, log=open(cfg["log"], "a", encoding="utf-8") if cfg.get("log") else None,
                            env=vision_env(cfg, env))
        print("model unloaded; the first request loads it ..." if lazy else
              "loading the model (the first start takes a minute or two) ...", flush=True)
        if len(gpu_list(cfg)) > 1:
            try:
                split = layer_split_value(cfg)          # #644: before the (minutes-long) start
            except ValueError as e:
                raise SystemExit(f"[hypercore] config {e}")
            print(f"[hypercore] layer split across GPUs {gpu_list(cfg)} ({split})", flush=True)
        # a relative "exe" is the config's cwd's: Windows' CreateProcess resolves "engine/hypercore.exe" against nothing
        # it is told about (WinError 2), so it is made absolute here
        exe = engine_binary(cfg)
        if not exe:
            ap.error('no engine to run: put one at engine/strata, set STRATA_ENGINE, '
                     'or give a run config with an "exe"')
        try:
            silence = engine_silence_s(cfg)             # #481: checked before the (minutes-long) start
        except ValueError as e:
            raise SystemExit(f"[hypercore] config {e}")
        try:
            effort_end = effort_end_args(cfg, exe, tok)  # #458
        except ValueError as e:
            raise SystemExit(f"[hypercore] config {e}")
        worker_config = hypercore_worker_config(cfg, config_text)
        worker_binary = a.worker or cfg.get("worker") or worker_binary_path()
        if not Path(worker_binary).is_absolute():
            worker_binary = str(Path(cfg.get("cwd") or ".").resolve() / worker_binary)
        if not Path(worker_binary).is_file():
            # The worker is HyperCore's process boundary, not part of upstream Strata.
            # If it was never built, the same engine is still drivable over its own
            # stdin/stdout protocol, so the app runs in direct mode rather than refusing
            # to start. serve/direct_engine.py is that path.
            from serve.direct_engine import DirectEngine
            print(f"[hypercore] worker: not built ({worker_binary} is not there). "
                  "Running the engine in direct mode over its stdin/stdout protocol. "
                  "Build the worker with: cmake --build build-shared --target hypercore-worker",
                  flush=True)
            engine = DirectEngine(exe, cfg.get("args") or [], startup_timeout_s=model_startup_s(cfg),
                                 env=child_env(cfg), log_path=cfg.get("log"))
            try:
                engine.start()
            except Exception as exc:  # noqa: BLE001 - an engine that will not start is reported, not raised
                engine_candidate, engine, engine_unavailable = engine, OfflineEngine(
                    str(exc), model=cfg.get("model_name") or "no model loaded"), str(exc)
                print(f"[strata] {exc}", flush=True)
                print(f"[hypercore] the web app is up anyway; the engine is retried in the "
                      f"background every {engine.retry_after_s:g} s.", flush=True)
        else:
            socket_path = a.ipc_socket or cfg.get("ipc_socket") or unique_socket_path("hypercore-worker")
            # A worker with no model binds its socket in milliseconds. A worker that has to load
            # weights - the pack, the expert arena, the VRAM tier - takes the "minute or two" the
            # run script warns about, and the default 30s would kill a healthy load. The config
            # can set it; otherwise a model config gets a minute and a half.
            startup_s = model_startup_s(cfg)
            supervisor = HyperCoreProcessSupervisor(
                worker_binary=worker_binary,
                socket_path=socket_path,
                config=worker_config,
                stderr_log=cfg.get("log"),
                startup_timeout_s=startup_s,
                silence_s=silence,                    # #481: the stream's own wall, not the handshake's
                env=cfg.get("env") or {},            # #1733: the engine settings the calibration wrote for this PC
            )
            engine = HyperCoreRequestHandler(supervisor)
        if not lazy and not engine.restart():
            engine_candidate, engine, engine_unavailable = engine, OfflineEngine(
                "the worker did not complete the READY handshake (the GPU is busy, or the "
                "model paths in the run config are wrong)",
                model=cfg.get("model_name") or "no model loaded"), "the worker did not come up"
            print("[hypercore] the worker did not complete the READY handshake.", flush=True)
            print(f"[hypercore] the web app is up anyway; the worker is retried in the "
                  f"background every {engine.retry_after_s:g} s.", flush=True)
        else:
            warn_tight_ram(engine.info.get("arena_mib"))
            note = desktop_vram_note(cfg.get("backend"), engine.info.get("vram_free_mib"),
                                     engine_args(cfg), linux_desktop())
            if note:                                    # #560 #516: before --open starts a browser on that card
                print(note, flush=True)
            print(expert_config_line(engine.info), flush=True)
    else:
        effort_end = None
        engine, vision, sampling_defaults = MockEngine(tok, a.script or [
            "Thinking about it.</think>\n\nHello from the mock engine."]), None, {}
    # the model's own chat template (exported with its tokenizer), else the original model's
    tpl = tpath / "chat_template.jinja"
    svc = Service(engine, tok, ChatTemplate(tpl if tpl.exists() else ROOT / "serve/chat_template.jinja"),
                  model_name=cfg.get("model_name", "qwen3.8-flash-next"), vision=vision,
                  sampling_defaults=sampling_defaults,
                  fit_max_tokens=a.fit_max_tokens or cfg.get("fit_max_tokens") is True,
                  trim_context=a.trim_context or cfg.get("trim_context") is True)
    if engine_unavailable:
        interval = a.engine_retry_s if a.engine_retry_s is not None else cfg.get("engine_retry_s", DEFAULT_RETRY_S)
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            raise SystemExit(f'[hypercore] config "engine_retry_s" must be a number of seconds, not {interval!r}')
        if interval > 0 and engine_candidate is not None:
            def adopt(eng: object) -> None:
                svc.engine = eng
                print(f"[hypercore] the engine is back: context "
                      f"{getattr(eng, 'max_context', 0)} tokens", flush=True)

            def card_is_busy() -> str | None:
                """A sweep or a build has the card. Retrying an engine then fails, and the
                attempt itself disturbs what the sweep is measuring, so the retry holds off
                until that work is done.

                A sweep started from the CLI is invisible to this process, so an operator can
                say so directly: HYPERCORE_HOLD_THE_CARD="the 200-trial sweep on the 4090".
                """
                held = os.environ.get("HYPERCORE_HOLD_THE_CARD", "").strip()
                if held:
                    return held
                if tune_api.JOB.state == "running":
                    return "a tune sweep is using the GPU; the engine is retried when it finishes"
                build_state = getattr(build_api.JOB, "state", "idle")
                if build_state not in ("idle", "done", "failed", "cancelled"):
                    return f"a build is using the GPU ({build_state}); the engine is retried when it finishes"
                return None

            svc.engine_retry = EngineRetry(engine_candidate, adopt, engine_unavailable,
                                         interval_s=interval, busy=card_is_busy).start()
            print(f"[hypercore] the engine is retried in the background every {interval:g} s; "
                  "the web app keeps serving in the meantime, and the retry waits for a "
                  "tune sweep or a build to finish.", flush=True)
        else:
            print("[hypercore] no engine to retry: load one from the Chat tab, or start this "
                  "server again with a run config.", flush=True)
    try:
        svc.set_aliases(cfg.get("aliases"))             # #297: other names the model answers to
    except ValueError as e:
        raise SystemExit(f"[hypercore] config {e}")
    if svc.aliases:
        print(f"[hypercore] model aliases: {', '.join(svc.aliases)}", flush=True)
    if ("HYPERCORE_API_KEY" in os.environ and not os.environ["HYPERCORE_API_KEY"].strip()) or             any(x == "--api-key" and i + 1 < len(sys.argv) and not sys.argv[i + 1].strip() or x.strip() == "--api-key="
                for i, x in enumerate(sys.argv)):
        # #213: an empty key would switch authentication off without a word
        print("[hypercore] an API key was given but it is empty: set a key, or leave --api-key / HYPERCORE_API_KEY out",
              file=sys.stderr)
        return 2
    svc.api_key = a.api_key or cfg.get("api_key", "")
    svc.cors_origins = origins_of(cfg.get("cors_origins"), "cors_origins", wildcard=True)
    svc.trusted_origins = origins_of(cfg.get("trusted_origins"), "trusted_origins", wildcard=False)
    try:
        svc.allowed_hosts = allowed_hosts_of(cfg.get("allowed_hosts"), os.environ.get("HYPERCORE_ALLOWED_HOSTS", ""))
    except ValueError as e:
        raise SystemExit(f"[hypercore] config {e}")
    if svc.allowed_hosts:
        print("[hypercore] Host check off: any name reaches this server (allowed_hosts \"*\")" if "*" in svc.allowed_hosts
              else f"[hypercore] also answers to the host names {', '.join(svc.allowed_hosts)} (allowed_hosts)", flush=True)
    svc.api_monitor = a.api_monitor or cfg.get("api_monitor") is True
    if svc.api_monitor:
        print("[hypercore] API request monitor on (/api-monitor): the last 100 requests' prompts and answers are kept in "
              "memory" + ("" if svc.api_key else "; anyone who can reach this server can read them (no API key)"),
              flush=True)
    if svc.cors_origins:
        print(f"[hypercore] CORS on /v1/* for {', '.join(svc.cors_origins)}"
              + ("" if svc.api_key or "*" not in svc.cors_origins else
                 " - WARNING: any web page may use the model (no API key)"), flush=True)
    svc.idle_unload_s = a.idle_unload if a.idle_unload is not None else float(cfg.get("idle_unload_s") or 0)
    svc.min_free_vram_mib = a.min_free_vram_mib if a.min_free_vram_mib is not None else \
        int(cfg.get("min_free_vram_mib") or 0)
    svc.before_load = a.before_load or cfg.get("before_load") or None
    svc.before_load_timeout_s = float(cfg.get("before_load_timeout_s") or 120.0)   # #1997
    svc.before_load_ignore_failure = bool(cfg.get("before_load_ignore_failure"))
    svc.before_load_strict = bool(cfg.get("before_load_strict"))
    # --slot-save-path is relative to the working directory the server was started in; the config's slot_save_path
    # to the config's "cwd" (the engine's folder) when it has one.  Either way the engine gets an absolute path.
    if a.slot_save_path or cfg.get("slot_save_path"):
        try:
            svc.slot_save_path = slot_save_dir(a.slot_save_path) if a.slot_save_path else \
                slot_save_dir(cfg["slot_save_path"], cfg.get("cwd"))
        except (ValueError, OSError) as e:
            raise SystemExit(f"[hypercore] {e}")
        print(f"[hypercore] slot save/restore on: {svc.slot_save_path} (session files are kept until deleted: "
              "about 1.2 GB per 63K-token conversation; restore only files this server wrote)", flush=True)
    mode = str(cfg.get("anthropic_thinking") or "model")   # #278: "on_request" = only when the request asks
    if mode not in ("model", "on_request"):
        raise SystemExit(f"[hypercore] config anthropic_thinking must be \"model\" or \"on_request\", not {mode!r}")
    svc.anthropic_think_unasked = mode == "model"
    rs = cfg.get("repeat_stop_tokens", REPEAT_STOP_TOKENS)   # #606: opt-out with 0
    if isinstance(rs, bool) or not isinstance(rs, int) or rs < 0:
        raise SystemExit(f"[hypercore] config \"repeat_stop_tokens\" must be a whole number >= 0 (0 = off), not {rs!r}")
    svc.repeat_stop_tokens = rs
    tail = resolve_turn_tail(a.turn_tail_check, cfg)   # the end-of-turn "is it done?" check
    if not tail and (a.turn_tail_check or cfg.get("turn_tail_check") is True):
        print("[hypercore] the turn-tail check was asked for but HYPERCORE_NO_TURN_TAIL_CHECK is "
              "set: it is off for this install", flush=True)
    svc.turn_tail_check = tail
    checks = a.turn_tail_checks if a.turn_tail_checks is not None else cfg.get("turn_tail_checks", TAIL_CHECK_MAX)
    if isinstance(checks, bool) or not isinstance(checks, int) or not 0 <= checks <= 10:
        raise SystemExit(f"[hypercore] \"turn_tail_checks\" must be a whole number 0-10, not {checks!r}")
    svc.turn_tail_checks = checks
    custom_prompt = cfg.get("turn_tail_prompt")
    if isinstance(custom_prompt, str) and custom_prompt.strip():
        svc.turn_tail_prompt = custom_prompt.strip()
    if tail:
        print(f"[hypercore] turn-tail check on: when a turn would end the model is asked whether its tasks are "
              f"done, and the turn goes on while it says otherwise (up to {svc.turn_tail_checks} times per turn; "
              "turn_tail_check in the config)", flush=True)
    svc.effort_end = bool(effort_end)                   # #458: "effort_position": "end" with an engine that has it
    if cfg.get("reasoning_budget_tokens") is not None:  # #123: a default thinking budget for every request
        try:
            svc.reasoning_budget_tokens = cfg["reasoning_budget_tokens"]
            budget = svc.reasoning_budget({})
        except ValueError as e:
            raise SystemExit(f"[hypercore] config {e}")
        if budget:
            print(f"[hypercore] thinking budget: {budget} tokens (reasoning_budget_tokens; a request can set its own)",
                  flush=True)
    svc.tool_call_recovery = bool(cfg.get("tool_call_recovery", False))   # #2037: a tool call written as JSON
    recovery = cfg.get("reasoning_loop_recovery", False)   # #728: false (default) | "stop" | "recover" (true)
    if recovery is True:
        recovery = "recover"
    if recovery is not False and recovery not in ("stop", "recover"):
        raise SystemExit("[hypercore] config \"reasoning_loop_recovery\" must be false, \"stop\" or \"recover\", "
                         f"not {recovery!r}")
    svc.reasoning_loop_recovery = recovery
    svc.gpu_index = (gpu_list(cfg) or [0])[0]           # the Monitor reads the card the engine runs on (issue #51)
    svc.gpu_indices = gpu_list(cfg)                     # ... or every card of a layer split (issue #112)
    svc.backend = cfg.get("backend")                    # "hip": the AMD cards' readings come from sysfs (#301)
    if a.config:
        svc.config_path = a.config                      # #564: the web page's Settings view
    if a.config and not is_inline_config(a.config):            # the Chat settings shared with other apps, from last time
        svc.shared_path = str(Path(a.config).with_suffix("")) + ".shared-settings.json"
        try:
            svc.shared = clean_shared_defaults(json.loads(Path(svc.shared_path).read_text(encoding="utf-8")))
            if svc.shared:
                print("[hypercore] other apps use the Chat settings: " +
                      ", ".join(f"{k}={v}" for k, v in svc.shared.items()), flush=True)
        except (OSError, ValueError):
            svc.shared = {}
    if hub is not None:
        import atexit
        svc.mcp = hub
        print(f"[hypercore] starting {len(hub.servers)} MCP server{'s' * (len(hub.servers) != 1)} for the web app's "
              f"chat: {', '.join(hub.servers)}", flush=True)
        hub.start()
        atexit.register(hub.close)                      # the servers HyperCore started end with it
    httpd = serve(svc, host=a.host, port=a.port)
    svc.start_idle_unload()
    here = "127.0.0.1" if a.host in ("0.0.0.0", "", "::") else a.host
    print(f"ready: http://{here}:{a.port}/v1  (OpenAI: /v1/chat/completions, Anthropic: /v1/messages, "
          f"{f'context {engine.max_context} tokens' if not isinstance(engine, OfflineEngine) else 'no engine loaded'}"
          f"{', images on' if vision else ''}"
          f"{', API key required' if svc.api_key else ''})", flush=True)
    print(f"       open http://{here}:{a.port}/ in a browser to chat; close this window to stop the model", flush=True)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        # issue #26: reachable from other devices - say at which address, and what can still block it
        ips = lan_addresses()
        for ip in ips:
            print(f"       from other devices: http://{ip}:{a.port}/   (API: http://{ip}:{a.port}/v1)", flush=True)
        if not ips:
            print("       from other devices: http://<this PC's IP address>:" + str(a.port) + "/", flush=True)
        if not svc.api_key:
            print("       WARNING: no API key - anyone on your network can use this model. Add \"api_key\": \"...\" "
                  "to the config (clients send it as their API key; the web page asks for it)", flush=True)
        if os.name == "nt":
            print("       nothing arrives? Windows Firewall blocks it until allowed: accept its prompt for Python, or run "
                  "in an admin PowerShell:\n         New-NetFirewallRule -DisplayName \"HyperCore " + str(a.port) + "\" "
                  "-Direction Inbound -Protocol TCP -LocalPort " + str(a.port) + " -Action Allow -Profile Private\n"
                  "       (and set this network to Private in Windows' network settings)", flush=True)
    if a.open and cfg.get("open_browser") is not False:   # #609: the config's "open_browser": false wins (an older
        import webbrowser  # run-<model>.bat still passes --open)
        webbrowser.open(f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/")
    # #96: docker stop sends SIGTERM, which Python ignores by default, so the container's PID 1 would be killed after
    # the grace period with the engine still running. SIGTERM takes Ctrl+C's path below (QUIT to the engine).
    # SIGINT keeps Python's own handler, so Ctrl+C and a second Ctrl+C work as before.
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except (ValueError, OSError, AttributeError):         # not the main thread
        pass
    def stop_everything(reason: str) -> None:
        """Close every process this server started, then say so. Used by Ctrl+C, SIGTERM
        and the banner's Shut Down button, so all three end the same way (#641)."""
        print(f"\n[hypercore] stopping ({reason}) ...", flush=True)
        close_everything(svc, httpd)
        print("[hypercore] stopped", flush=True)

    try:
        while not svc.stop_requested.is_set():
            time.sleep(1)                               # Windows never delivers Ctrl+C to an untimed Event.wait()
        stop_everything("asked from the web page")
    except KeyboardInterrupt:
        stop_everything("Ctrl+C")
    return 0


if __name__ == "__main__":
    sys.exit(main())
