#!/usr/bin/env python3
"""serve/frame_fixture.py - a mock worker that speaks the binary protocol.

The old integration tests mocked the engine by pushing text lines into `engine.lines`.
That mock is gone with the text protocol, so the tests need something that produces the
same edge behaviours without a GPU and without a subprocess: a socketpair, with the far
end answering frames according to a script.

What this is for:
  - slot saturation and its drain (a second request while the first holds the slot),
  - rapid client abandonment (a CANCEL frame arriving before the first token),
  - the recovery loop (the link dies, the restart budget is consumed, exhaustion is reported).

The mock implements the framing in serve/ipc_parser.py, not a copy of it. If the framing
changes, this mock changes with it - which is the point: a fixture that drifts from the
contract tests nothing.

    worker = ScriptedWorker(max_slots=1, tokens=3)
    sup = HyperCoreProcessSupervisor(transport=worker.connect)
    sup.start()
"""
from __future__ import annotations

import socket
import struct
import threading
import time

from serve.ipc_parser import (
    CAPS_REPLY,
    CMD_CANCEL,
    CMD_CAPS,
    CMD_PING,
    CMD_SUBMIT,
    NOT_MEASURED,
    RESP_CAPS,
    RESP_PONG,
    RESP_STATUS,
    STREAM_TERMINAL,
    SUMMARY_WIRE_SIZE,
    TERMINAL_WIRE,
    decode_config,
    decode_header,
)

STOP_UNSPECIFIED, STOP_EOS, STOP_LIMIT, STOP_CANCEL, STOP_ERROR = 0, 1, 2, 3, 4


class ScriptedWorker:
    """A worker process replaced by a thread on the other end of a socketpair.

    `tokens` is how many token frames a request emits before its terminal frame; `prefill_s`
    is the delay before the first token, which is what makes "cancel during prefill" testable.
    `hold_s` is a queue hold before that, counted in ttft_ms but not in prefill_ms - the
    distinction the wire contract is built on.
    `draft_s` is the first speculative draft cycle: it runs after prompt evaluation and
    before the first token, so it is charged to decode_ms and never to prefill_ms. A
    fixture without it cannot show that prompt timing is independent of sampling.
    `die_after` kills the link after that many frames, standing in for an engine fault.
    `mute_ping` answers no ping, standing in for a worker that is alive but
    not serving - the distinction the supervisor has to make without a heartbeat frame.
    `mute_submit` answers no ack to a request at all, standing in for a worker that is
    up but never schedules: the case the ack timeout has to tell apart from a dead link.
    """

    def __init__(
        self,
        *,
        max_slots: int = 1,
        tokens: int = 3,
        prefill_s: float = 0.0,
        hold_s: float = 0.0,
        draft_s: float = 0.0,
        die_after: int | None = None,
        mute_ping: bool = False,
        mute_submit: bool = False,
        backend: int = 0,
        max_context: int = 4096,
        pool_workers: int = 4,
        instance_pcie_frac: float = -1.0,
        instance_spec_min_p: float = -1.0,
        token_start: int = 1000,
        prompt_reused: int = 0,
        cache_hits: int | None = None,
        cache_lookups: int | None = None,
        offloaded: int | None = None,
        prompt_read: int | None = None,
        ram_blobs: int | None = None,
        file_blobs: int | None = None,
        file_kb: int | None = None,
        expert_slots: int = 0,
        expert_slots_full: int = 0,
        expert_cache_mib: int = 0,
        expert_cache_full_mib: int = 0,
        vram_free_mib: int = 0,
        expert_source: int = 0,
        prompt_chunk: int = 0,
    ) -> None:
        self.max_slots = max_slots
        self.tokens = tokens
        self.prefill_s = prefill_s
        self.hold_s = hold_s
        self.draft_s = draft_s
        self.die_after = die_after
        self.mute_ping = mute_ping
        self.mute_submit = mute_submit
        self.backend = backend
        self.max_context = max_context
        self.pool_workers = pool_workers
        self.instance_pcie_frac = instance_pcie_frac
        self.instance_spec_min_p = instance_spec_min_p
        self.token_start = token_start
        self.prompt_reused = prompt_reused
        self.cache_hits = cache_hits
        self.cache_lookups = cache_lookups
        self.offloaded = offloaded
        self.prompt_read = prompt_read
        self.ram_blobs = ram_blobs
        self.file_blobs = file_blobs
        self.file_kb = file_kb
        self.expert_slots = expert_slots
        self.expert_slots_full = expert_slots_full
        self.expert_cache_mib = expert_cache_mib
        self.expert_cache_full_mib = expert_cache_full_mib
        self.vram_free_mib = vram_free_mib
        self.expert_source = expert_source
        self.prompt_chunk = prompt_chunk

        self.in_flight = 0
        self.received: list[tuple[int, int, bytes]] = []
        self.sent_frames = 0
        self._cancelled: set[int] = set()
        self.link_closed = threading.Event()
        self._thread: threading.Thread | None = None

    def connect(self) -> socket.socket:
        """Return the end of a socketpair the supervisor should talk to. Every call is a
        new 'process': counters reset, so a restart is modelled as a new connection."""
        self.sent_frames = 0
        self.in_flight = 0
        self._cancelled.clear()
        self.link_closed.clear()
        old = getattr(self, "_sock", None)
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        parent, child = socket.socketpair()
        self._sock = child
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return parent

    def close(self) -> None:
        self.link_closed.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # ---------------------------------------------------------------- the wire
    def await_frame(self, msg_type: int, timeout: float = 2.0) -> bool:
        """Wait until a frame of this type has reached the mock. Tests need this because
        the supervisor's write and the mock's read are different threads."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(r[0] == msg_type for r in self.received):
                return True
            time.sleep(0.005)
        return False

    def _send(self, msg_type: int, request_id: int, payload: bytes) -> None:
        if self.link_closed.is_set():
            return
        if self.die_after is not None and self.sent_frames >= self.die_after:
            # A process that dies closes its stdout. shutdown(SHUT_WR) is the faithful
            # stand-in: the peer sees EOF. close() alone does not, because the mock's own
            # reader thread still holds a reference to the socket and keeps the fd alive.
            self.link_closed.set()
            try:
                self._sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            return
        try:
            self._sock.sendall(struct.pack("<IIQQ", 0x53545241, msg_type, request_id, len(payload)) + payload)
        except OSError:
            self.link_closed.set()
            return
        self.sent_frames += 1

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly `n` bytes, or return what arrived before the link closed.

        recv() returns *up to* n bytes. On a socketpair a frame can arrive in pieces, and
        treating a short read as a whole frame desynchronises the reader: the next
        "header" is the tail of the previous payload, and decode_header raises on it.
        """
        out = bytearray()
        while len(out) < n:
            try:
                chunk = self._sock.recv(n - len(out))
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def _serve(self) -> None:
        while not self.link_closed.is_set():
            try:
                header = self._recv_exact(24)
            except OSError:
                return
            if len(header) < 24:
                return
            msg_type, request_id, length = decode_header(header)
            payload = self._recv_exact(length) if length else b""
            self.received.append((msg_type, request_id, payload))

            if msg_type == CMD_SUBMIT:
                self._handle_submit(request_id, payload)
            elif msg_type == CMD_CANCEL:
                self._handle_cancel(request_id)
            elif msg_type == CMD_CAPS:
                self._handle_caps(request_id)
            elif msg_type == CMD_PING and not self.mute_ping:
                # A muted fixture answers no ping: a link that is up but not
                # answering, which is exactly what a heartbeat frame would
                # otherwise have to detect.
                self._send(RESP_PONG, request_id, struct.pack("<I", 1))

    def _handle_submit(self, request_id: int, payload: bytes) -> None:
        # Validation comes before the slot check, exactly as the engine does it: an
        # invalid config is a 400, not a 503, and a mock that never rejects one cannot
        # test the rejection path.
        cfg = decode_config(payload)
        if cfg.get("max_new_tokens", 1) < 1:
            self._send(RESP_STATUS, request_id, struct.pack("<i", -1))  # INVALID_ARG
            return
        if self.in_flight >= self.max_slots:
            self._send(RESP_STATUS, request_id, struct.pack("<i", -2))  # BUSY
            return
        self.in_flight += 1
        if self.mute_submit:
            # Stay open and stay silent: the request is accepted by the socket but never
            # acknowledged, so the supervisor's ack timeout is the only thing it sees.
            time.sleep(0.5)
            self.in_flight -= 1
            return
        self._send(RESP_STATUS, request_id, struct.pack("<i", 0))
        # A real worker runs each request on its own engine thread. The mock has to do the
        # same or "saturated slot" cannot be observed: a single-threaded mock finishes the
        # first request before the second frame is even read.
        threading.Thread(target=self._run_request, args=(request_id,), daemon=True).start()

    def _run_request(self, request_id: int) -> None:
        request_start = time.monotonic()
        # Two separate delays, because the wire reports two separate windows. `hold_s` is
        # the queue gate (counted in ttft, not in prefill); `prefill_s` is prompt
        # evaluation (counted in both). A fixture with a single sleep cannot tell the
        # difference, and neither could the tests.
        if self.hold_s:
            time.sleep(self.hold_s)
        scheduled_at = time.monotonic()
        if self.prefill_s:
            time.sleep(self.prefill_s)
        prefill_ms = int((time.monotonic() - scheduled_at) * 1000)
        if self.link_closed.is_set():
            self.in_flight -= 1
            return

        # The generation window opens here, before any draft is built. Everything from
        # this point to the terminal frame is decode work, including the first
        # speculative cycle - see decision 1 in the README.
        decode_start = time.monotonic()
        if self.draft_s:
            time.sleep(self.draft_s)

        produced = 0
        ttft_ms = 0
        for i in range(self.tokens):
            # The emit loop checks the cancel flag on every token, so a stream that is
            # stopped mid-way terminates with CANCEL and reports only what it delivered.
            if request_id in self._cancelled or self.link_closed.is_set():
                break
            if produced == 0:
                # Stamped at the moment the first token reaches the socket, exactly as the
                # real worker does. A fixture that guessed TTFT from prefill_s would be
                # testing a number nothing in production produces.
                ttft_ms = int((time.monotonic() - request_start) * 1000)
            self._send(4, request_id, struct.pack("<iI", self.token_start + i, self.prompt_reused))
            produced += 1
            time.sleep(0.001)

        stop = STOP_CANCEL if request_id in self._cancelled else STOP_LIMIT
        decode_ms = int((time.monotonic() - decode_start) * 1000) if produced else 0
        self._send_terminal(request_id, produced=produced, stop=stop,
                            ttft_ms=ttft_ms, prefill_ms=prefill_ms, decode_ms=decode_ms)
        self.in_flight -= 1

    def _handle_cancel(self, request_id: int) -> None:
        # Recording the flag is the whole job: the emit loop sees it and closes the stream
        # at the next token. Emitting a second terminal here would give a stream two ends.
        self._cancelled.add(request_id)

    def _handle_caps(self, request_id: int) -> None:
        payload = CAPS_REPLY.pack(
            self.backend, self.max_slots, self.max_context, self.pool_workers, 0,
            self.instance_pcie_frac, self.instance_spec_min_p,
            self.expert_slots, self.expert_slots_full, self.expert_cache_mib,
            self.expert_cache_full_mib, self.vram_free_mib, self.expert_source,
            self.prompt_chunk,
        )
        self._send(RESP_CAPS, request_id, payload)

    def _send_terminal(self, request_id: int, produced: int, stop: int,
                      ttft_ms: int = 0, prefill_ms: int = 0, decode_ms: int = 0) -> None:
        # Built from TERMINAL_WIRE, the same Struct the parser reads with. The fixture and
        # the decoder cannot drift apart, which is the point of freezing the layout.
        def wire(value: int | None) -> int:
            return NOT_MEASURED if value is None else int(value)

        self._send(
            STREAM_TERMINAL,
            request_id,
            TERMINAL_WIRE.pack(self.prompt_reused, SUMMARY_WIRE_SIZE, produced, prefill_ms, decode_ms,
                              0, 0, stop, ttft_ms,
                              wire(self.cache_hits), wire(self.cache_lookups), wire(self.offloaded),
                              wire(self.prompt_read), wire(self.ram_blobs), wire(self.file_blobs),
                              wire(self.file_kb)),
        )
