#!/usr/bin/env python3
"""serve/ipc_parser.py - the wire format between the server and a HyperCore worker.

This module owns the framing and nothing else: no process handling, no HTTP, no policy.
It is the only Python place that knows what a byte offset means, so the supervisor and
any future client decode through it rather than re-reading struct layouts.

The contract is the one stated in src/interface/worker_main.cpp: a 24-byte header, then
payload_length bytes, all little-endian. Offsets are fixed and there is no compiler between
the two sides, so a change here has to be made in both files at once.

    offset  size  field
    0       4     magic 0x53545241
    4       4     message type
    8       8     request id
    16      8     payload length (<= 0x00FFFFFF)

Everything the server needs is delivered through a per-request queue.Queue: the listener
thread is the only reader of the socket, and a request thread never touches the socket.
"""
from __future__ import annotations

import queue
import socket
import struct
import threading
from typing import Any

MAGIC = 0x53545241
MAX_PAYLOAD = 0x00FFFFFF

CMD_SUBMIT, CMD_CANCEL, RESP_STATUS, STREAM_TOKEN, STREAM_TERMINAL = 1, 2, 3, 4, 5
CMD_CAPS, RESP_CAPS, CMD_PING, RESP_PONG = 6, 7, 8, 9
CMD_GET_CAPS = CMD_CAPS  # the name the request/reply table uses

# Request -> reply, stated as data rather than derived by arithmetic.
#
# The wire started out symmetric (SUBMIT->STATUS, CAPS->CAPS, PING->PONG), and a reader
# that assumed reply = request + 1 worked. It is not a property of the protocol, it is a
# coincidence of the first six constants. A command that answers with a frame the client
# has to correlate by hand - a partial cancel, a capability revision - breaks every
# `msg_type + 1` check at once and the failure looks like a dead worker.
MSG_TYPE_MAP = {
    CMD_SUBMIT: RESP_STATUS,      # immediate ack: 0 scheduled, -2 BUSY, -1 invalid arg
    CMD_CANCEL: RESP_STATUS,      # ack of a cancel; NOT_FOUND if the id is not in flight
    CMD_GET_CAPS: RESP_CAPS,      # instance capability snapshot
    CMD_PING: RESP_PONG,          # liveness probe
}

# Frames that carry no request-correlated reply. Kept separate so a typo in the table
# above shows up as a KeyError rather than a timeout.
UNSOLICITED_FRAMES = (STREAM_TOKEN, STREAM_TERMINAL)


def expected_reply(msg_type: int) -> int | None:
    """The frame type a well-behaved worker answers `msg_type` with, or None if it
    answers nothing. Raises nothing: callers decide how to fail."""
    return MSG_TYPE_MAP.get(msg_type)

HEADER = struct.Struct("<IIQQ")
CFG = struct.Struct("<IffIIff")                    # 28 bytes: hypercore_request_config_t
SUBMIT_HEAD = struct.Struct("<IffIIffIQ")          # CFG + reserved + prompt_len
# The terminal payload is 4 bytes of prompt_reused followed by the 40-byte summary, written
# field by field by the worker. "<" means no alignment padding, so these are the frozen wire
# offsets from hypercore/hypercore_api.h - not a C struct's in-memory image.
SUMMARY_WIRE_SIZE = 68
SUMMARY_MIN_SIZE = 40        # every reader since 0.1.30 knows this prefix
TERMINAL_WIRE = struct.Struct("<IIIQQIIII" + "IIIIIII")   # 72 bytes: the 44-byte prefix above
                                                 # plus the expert-tier accounting (hits, lookups,
                                                 # offloaded, prompt_read, ram_blobs, file_blobs, file_kb)
TERMINAL_PREFIX = struct.Struct("<IIIQQIIII")   # 44 bytes: what every worker since 0.1.30 sends
TERMINAL_V1 = struct.Struct("<IIQIIII")          # 32 bytes: the pre-packing draft, still
                                                 # accepted from an older worker binary
TOKEN = struct.Struct("<iI")                       # token_id, prompt_reused
CAPS_REPLY = struct.Struct("<IIIIiff" + "IIIIIII")   # 56 bytes: backend, slots, max_ctx, pool_workers,
                                                  # status, instance_pcie_frac, instance_spec_min_p,
                                                  # then the expert tier (slots, slots_full, cache MiB,
                                                  # cache full MiB, vram free MiB, expert source, prompt chunk)
CAPS_REPLY_PREFIX = struct.Struct("<IIIIiff")        # 28 bytes: what a worker before 0.1.41 sends

STOP_REASONS = {0: "unspecified", 1: "eos", 2: "limit", 3: "cancel", 4: "error"}

# A summary field the worker did not measure. It is sent as 0xFFFFFFFF, never as 0, because a
# 0 in a hit-rate cell is a measurement and the absence of one is not - a dashboard that sees 0
# will alert on a cache that is empty when it is simply unreported.
NOT_MEASURED = 0xFFFFFFFF


def _or_none(value: int) -> int | None:
    return None if value == NOT_MEASURED else value

# The one table that says what the Monitor reports and where each value comes from.
#
# Key = the name the dashboard and the alert rules were built on. Value = (source, field).
# "summary" reads a field off the terminal frame; "process" and "watchdog" read the worker's
# lifecycle, which is not on the wire at all; "derived" computes from the summary.
# Adding a metric means adding one row here - nothing else has to change.
MONITOR_FIELDS: dict[str, tuple[str, str]] = {
    "prefill_ms":      ("summary", "prefill_ms"),
    "prompt_ms":       ("derived", "prompt_ms"),
    "prefill_tok_s_mean": ("derived", "prefill_tok_s_mean"),
    "decode_ms":       ("summary", "decode_ms"),
    "ttft_ms":         ("summary", "ttft_ms"),
    "tokens_produced": ("summary", "produced_tokens"),
    "drafts_accepted": ("summary", "drafts_accepted"),
    "drafts_offered":  ("summary", "drafts_offered"),
    "stop_reason":     ("summary", "stop_reason"),
    "queue_hold_ms":   ("derived", "queue_hold_ms"),
    "decode_tok_s":    ("derived", "decode_tok_s"),
    # #Monitor: the expert-tier columns of the Recent requests table. The worker has always
    # computed these; they were not on the wire, so the table rendered "-" for every request.
    "prompt_read":     ("summary", "prompt_read"),
    "cache_hits":      ("summary", "cache_hits"),
    "cache_lookups":   ("summary", "cache_lookups"),
    "offloaded":       ("summary", "offloaded"),
    "ram_blobs":       ("summary", "ram_blobs"),
    "file_blobs":      ("summary", "file_blobs"),
    "file_kb":         ("summary", "file_kb"),
    "hit_rate":        ("derived", "hit_rate"),
    "pcie_share":      ("derived", "pcie_share"),
    "exit_code":       ("process", "exit_code"),
    "panic":           ("process", "panic"),
    "restarts":        ("watchdog", "restarts"),
    "status":          ("derived", "status"),
}

# Fields a caller cannot get from the wire because the worker that produced the frame never
# had them. Absent is reported as None, never 0: a zero is a measurement, a None is the
# absence of one, and a dashboard that sees 0 for an unmeasured field will alert on it.
def derive_monitor_field(name: str, summary: dict[str, Any], watchdog: dict[str, Any]) -> object:
    if name == "prompt_ms":
        prefill = summary.get("prefill_ms")
        return float(prefill) if prefill is not None else None
    if name == "prefill_tok_s_mean":
        # The engine's own number: the tokens this request read of its prompt over the time it
        # took to read them. The cached prefix is not read, so counting it would report a speed
        # the engine never achieved. A worker that reported neither is unmeasured, not instant.
        read, prefill = summary.get("prompt_read"), summary.get("prefill_ms")
        if not read or not prefill:
            return None
        return round(read / (prefill / 1000), 1)
    if name == "queue_hold_ms":
        # ttft - prefill. Either side being None means the worker never measured it, and
        # the hold is unknown rather than zero - a dashboard that sees 0 here will page
        # for a congestion that was never there.
        ttft, prefill = summary.get("ttft_ms"), summary.get("prefill_ms")
        return max(0, ttft - prefill) if ttft is not None and prefill is not None else None
    if name == "decode_tok_s":
        produced, decode_ms = summary.get("produced_tokens"), summary.get("decode_ms")
        return round(produced / (decode_ms / 1000), 1) if produced and decode_ms else None
    if name == "hit_rate":
        # The share of the decode's routed experts that were already in the VRAM tier. A worker
        # that never reported the counters gives None, not 0%.
        hits, lookups = summary.get("cache_hits"), summary.get("cache_lookups")
        return round(hits / lookups, 3) if hits is not None and lookups else None
    if name == "pcie_share":
        # #588: the experts the GPU read over PCIe or another GPU computed. They are in neither
        # the hits nor the lookups, so the share is of everything routed, not of the VRAM tier.
        off, lookups = summary.get("offloaded"), summary.get("cache_lookups")
        routed = (lookups or 0) + (off or 0)
        return round(off / routed, 3) if off is not None and routed else None
    if name == "status":
        return "up" if watchdog.get("alive") else "down"
    return None

# A frame delivered to a request queue is always (msg_type, token_id, payload), whatever
# the frame is. The older shape put the token id in slot 0 and the message type in slot 0
# for the ack, which worked until a token id of 3 collided with RESP_STATUS = 3. Tagging
# every frame with its type costs nothing and removes the collision.
StreamItem = tuple[int, int, Any]  # msg_type, token_id (-1 for terminal), payload


class FrameError(ValueError):
    """The bytes on the wire do not match the contract."""


def build_frame(msg_type: int, request_id: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"payload of {len(payload)} bytes exceeds the {MAX_PAYLOAD} byte cap")
    return HEADER.pack(MAGIC, msg_type, request_id, len(payload)) + payload


def decode_config(payload: bytes) -> dict[str, float | int]:
    """Read the request config out of a submit payload.

    Returns {} when the payload is too short to hold one - a caller that wants to
    reject a malformed frame decides that, this function just reports.
    """
    if len(payload) < CFG.size:
        return {}
    struct_size, temperature, top_p, top_k, max_new_tokens, pcie_frac, spec_min_p = CFG.unpack(payload[:CFG.size])
    return {
        "struct_size": struct_size,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "max_new_tokens": max_new_tokens,
        "pcie_frac": pcie_frac,
        "spec_min_p": spec_min_p,
    }


def build_submit_payload(
    *,
    temperature: float = 0.0,
    top_p: float = 1.0,
    top_k: int = 1,
    max_new_tokens: int = 64,
    pcie_frac: float = -1.0,
    spec_min_p: float = -1.0,
    prompt_tokens: list[int],
) -> bytes:
    """The payload of a request frame: config, reserved, prompt_len, then the tokens.

    pcie_frac/spec_min_p of -1.0 mean 'use the instance baseline'; the engine, not this
    module, applies the precedence rule.
    """
    head = SUBMIT_HEAD.pack(
        CFG.size, temperature, top_p, top_k, max_new_tokens, pcie_frac, spec_min_p,
        0, len(prompt_tokens),
    )
    body = struct.pack(f"<{len(prompt_tokens)}i", *prompt_tokens)
    return head + body


def build_submit_frame(request_id: int, **kwargs: Any) -> bytes:
    return build_frame(CMD_SUBMIT, request_id, build_submit_payload(**kwargs))


def decode_header(data: bytes) -> tuple[int, int, int, int]:
    if len(data) < HEADER.size:
        raise FrameError(f"short header: {len(data)} of {HEADER.size} bytes")
    magic, msg_type, request_id, length = HEADER.unpack(data)
    if magic != MAGIC:
        raise FrameError(f"bad magic 0x{magic:08x}, expected 0x{MAGIC:08x}")
    if length > MAX_PAYLOAD:
        raise FrameError(f"payload length {length} exceeds the cap")
    return msg_type, request_id, length


def decode_terminal(payload: bytes) -> dict[str, int | str | None]:
    """The summary in a terminal frame.

    Offsets are read positionally from the frozen wire layout, never from a struct image, so
    a worker built with a different toolchain decodes identically.

    Forward compatibility is one-directional and is the rule, not a courtesy: a declared
    struct_size at or above SUMMARY_WIRE_SIZE is accepted, the known prefix is read, and
    every byte past the prefix is discarded. A worker that appends fields to the summary is
    still readable here. A worker that removed or moved one is not - that is caught by the
    length check, because the prefix itself would no longer be there.

    A field the worker never sent is None, not 0. Those are different facts, and a
    dashboard that reads 0 for an unmeasured field will alert on a machine that was
    instantaneous.
    """
    if len(payload) >= TERMINAL_WIRE.size:
        (reused, struct_size, produced, prefill_ms, decode_ms,
         accepted, offered, stop_reason, ttft_ms,
         hits, lookups, offloaded, prompt_read, ram_blobs, file_blobs, file_kb) = TERMINAL_WIRE.unpack(
            payload[:TERMINAL_WIRE.size])
        if struct_size < SUMMARY_MIN_SIZE:
            raise FrameError(
                f"worker declares summary struct_size={struct_size}, "
                f"below the v1 minimum of {SUMMARY_MIN_SIZE}"
            )
        # struct_size > SUMMARY_WIRE_SIZE: a newer layout. The prefix is authoritative; the
        # tail belongs to fields this reader has never seen and must not guess at.
    elif len(payload) >= TERMINAL_PREFIX.size:
        # A worker built against the 40-byte summary. The timings are all there; the expert-tier
        # counters are not, and are reported as absent rather than as zero.
        (reused, struct_size, produced, prefill_ms, decode_ms,
         accepted, offered, stop_reason, ttft_ms) = TERMINAL_PREFIX.unpack(payload[:TERMINAL_PREFIX.size])
        if struct_size < SUMMARY_MIN_SIZE:
            raise FrameError(
                f"worker declares summary struct_size={struct_size}, "
                f"below the v1 minimum of {SUMMARY_MIN_SIZE}"
            )
        hits = lookups = offloaded = prompt_read = ram_blobs = file_blobs = file_kb = NOT_MEASURED
    elif len(payload) >= TERMINAL_V1.size:
        # A worker built against the first, unpacked draft of the summary. It has no
        # prefill window and no TTFT, so those are reported as absent.
        reused, struct_size, decode_ms, produced, accepted, offered, stop_reason = \
            TERMINAL_V1.unpack(payload[:TERMINAL_V1.size])
        prefill_ms = None
        ttft_ms = None
        hits = lookups = offloaded = prompt_read = ram_blobs = file_blobs = file_kb = NOT_MEASURED
    else:
        raise FrameError(f"short terminal payload: {len(payload)} bytes")

    return {
        "prompt_reused": reused,
        "struct_size": struct_size,
        "prefill_ms": prefill_ms,
        "decode_ms": decode_ms,
        "produced_tokens": produced,
        "drafts_accepted": accepted,
        "drafts_offered": offered,
        "stop_reason": STOP_REASONS.get(stop_reason, "unknown"),
        "stop_reason_code": stop_reason,
        "ttft_ms": ttft_ms,
        "cache_hits": _or_none(hits),
        "cache_lookups": _or_none(lookups),
        "offloaded": _or_none(offloaded),
        "prompt_read": _or_none(prompt_read),
        "ram_blobs": _or_none(ram_blobs),
        "file_blobs": _or_none(file_blobs),
        "file_kb": _or_none(file_kb),
    }


def decode_status(payload: bytes) -> int:
    return struct.unpack("<i", payload)[0]


def decode_caps(payload: bytes) -> dict[str, int | float]:
    """The capability snapshot: what the instance can do and what it is configured to do.

    backend/slots/max_ctx/pool_workers/status are unsigned-or-signed ints; the last two are the
    resolved Level-1 baseline floats. -1.0 means 'this instance was never given a value'.

    A 28-byte reply from an older worker is read in full and the expert tier is reported as
    absent (None), not as zero: "no cache" and "I do not know" are different answers, and the
    Monitor shows them differently.
    """
    if len(payload) >= CAPS_REPLY.size:
        (backend, slots, ctx, workers, status, instance_pcie, instance_spec,
         expert_slots, expert_slots_full, expert_cache_mib, expert_cache_full_mib,
         vram_free_mib, expert_source, prompt_chunk) = CAPS_REPLY.unpack(payload[:CAPS_REPLY.size])
    elif len(payload) >= CAPS_REPLY_PREFIX.size:
        backend, slots, ctx, workers, status, instance_pcie, instance_spec = CAPS_REPLY_PREFIX.unpack(
            payload[:CAPS_REPLY_PREFIX.size])
        expert_slots = expert_slots_full = expert_cache_mib = expert_cache_full_mib = 0
        vram_free_mib = expert_source = prompt_chunk = None
    else:
        raise FrameError(f"short caps payload: {len(payload)} bytes, expected at least {CAPS_REPLY_PREFIX.size}")
    return {
        "backend_type": backend,
        "max_batch_slots": slots,
        "max_context_len": ctx,
        "active_pool_workers": workers,
        "status": status,
        "instance_pcie_frac": instance_pcie,
        "instance_spec_min_p": instance_spec,
        "expert_slots": expert_slots,
        "expert_slots_full": expert_slots_full,
        "expert_cache_mib": expert_cache_mib,
        "expert_cache_full_mib": expert_cache_full_mib,
        "vram_free_mib": vram_free_mib,
        "expert_source": expert_source,
        "prompt_chunk": prompt_chunk,
    }


class HyperCoreIPCListener:
    """Reads frames from one socket and routes them to per-request queues.

    One reader thread per worker, because two readers on the same socket would each see a
    fragment of a frame. Request threads never read the socket; they get a queue.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._routes: dict[int, queue.Queue] = {}
        self._routes_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.dead = threading.Event()
        self.frames_read = 0
        self.bytes_read = 0

    # ---------------------------------------------------------------- routing
    def attach(self, request_id: int, maxsize: int = 1024) -> queue.Queue:
        """Return the queue this request's frames arrive on. The queue is the only
        channel between the reader thread and the request thread."""
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._routes_lock:
            self._routes[request_id] = q
        return q

    def detach(self, request_id: int) -> None:
        with self._routes_lock:
            self._routes.pop(request_id, None)

    def route(self, request_id: int) -> queue.Queue | None:
        with self._routes_lock:
            return self._routes.get(request_id)

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.dead.set()
        if self._thread is None:
            return
        # `dead` cannot wake a thread that is blocked inside recv(): the loop only
        # checks it between frames. Without this the join below waits out its whole
        # 5 s timeout, so every worker teardown - a restart, a shutdown, a CI stage -
        # costs five seconds of wall time for no reason. Closing the read side gives
        # recv an EOF, and the thread leaves the loop on its next iteration.
        try:
            self._sock.shutdown(socket.SHUT_RD)
        except OSError:
            pass  # the link may already be closed by the worker; the join still works
        self._thread.join(timeout=5.0)
        self._thread = None

    def _read_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _read_loop(self) -> None:
        while not self.dead.is_set():
            header = self._read_exact(HEADER.size)
            if header is None:
                self._fail("link closed")
                return
            try:
                msg_type, request_id, length = decode_header(header)
            except FrameError as exc:
                self._fail(str(exc))
                return
            payload = self._read_exact(length) if length else b""
            if length and payload is None:
                self._fail("link closed mid-payload")
                return
            self.frames_read += 1
            self.bytes_read += HEADER.size + length
            self._dispatch(msg_type, request_id, payload)

    def _dispatch(self, msg_type: int, request_id: int, payload: bytes) -> None:
        target = self.route(request_id)
        if target is None:
            return  # a detached request: the frame is dropped rather than crashing the reader
        if msg_type == STREAM_TOKEN:
            token_id, reused = TOKEN.unpack(payload)
            target.put((STREAM_TOKEN, token_id, reused))
        elif msg_type == STREAM_TERMINAL:
            target.put((STREAM_TERMINAL, -1, decode_terminal(payload)))
        elif msg_type in (RESP_STATUS, RESP_CAPS, RESP_PONG):
            target.put((msg_type, 0, payload))
        else:
            target.put((msg_type, 0, payload))

    def _fail(self, reason: str) -> None:
        self.dead.set()
        with self._routes_lock:
            for q in self._routes.values():
                q.put((None, 0, {"error": reason}))
