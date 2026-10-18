#!/usr/bin/env python3
"""serve/hypercore_supervisor.py - owns one engine process and the link to it.

Split deliberately: serve/ipc_parser.py knows the wire, this module knows the process. A
request thread in serve/server.py calls `send_ipc_message()` and then reads from the
queue.Queue the listener attached to its request id. It never touches a socket, never
spawns a process, and never parses a line of text.

Why a separate process: the boundary is crash-on-error by design, so an engine abort takes
down the process it runs in. Inside the server that would be every client; in a worker it is
one worker, and the engine threads never enter the interpreter, so the GIL is not on their path.

    from serve.engine_supervisor import HyperCoreProcessSupervisor
    sup = HyperCoreProcessSupervisor(
        worker_binary="build-shared/hypercore-worker",
        socket_path="/tmp/hypercore.sock",
        config='{"batch": 4}',   # inline JSON, or a path to a JSON config file
    )
    sup.start()
    for token_id, reused, summary in sup.stream([1, 2, 3, 4], max_new_tokens=64):
        ...

`config` is handed to the worker unchanged. The worker resolves it by its first non-whitespace
character: '{' is inline JSON, anything else is a file path. A path that does not exist, an
empty file, or a payload over 10 MB is an error, not "no config" - see
hypercore/hypercore_api.h for the rule. A config too large for a single argv entry is written
to a file beside the socket and passed by path; it is rewritten on every start, so a restart
can never read a stale configuration.
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from serve.ipc_parser import (
    CMD_CANCEL,
    CMD_CAPS,
    CMD_PING,
    CMD_SUBMIT,
    MONITOR_FIELDS,
    STREAM_TERMINAL,
    STREAM_TOKEN,
    HyperCoreIPCListener,
    build_frame,
    build_submit_payload,
    decode_caps,
    decode_status,
    derive_monitor_field,
    expected_reply,
)


class WorkerUnreachable(RuntimeError):
    """The worker process is gone or the link is broken."""


class WorkerBusy(WorkerUnreachable):
    """The worker's own slot gate refused the request (HYPERCORE_STATUS_BUSY).

    This is the authoritative answer. The host-side count is a snapshot taken at
    startup and can disagree with the worker after a config change, so the web
    layer never pre-checks it: a 503 here is the worker saying its lanes are full.
    """


class WorkerRejected(WorkerUnreachable):
    """The worker accepted the frame but rejected the configuration (invalid arg)."""


class StreamStalled(WorkerUnreachable):
    """No frame arrived within the silence window; the request was cancelled."""


ARGV_SAFE_CONFIG_BYTES = 128 * 1024 - 1   # Linux MAX_ARG_STRLEN: one argv entry cannot be larger


# ---------------------------------------------------------------------- how long a worker may take to start


def model_startup_s(cfg: dict) -> float:
    """Seconds a worker is allowed to take before it answers the READY handshake.

    A worker with no model binds its socket in milliseconds. One that has to load weights -
    the pack, the expert arena, the VRAM tier - takes the "minute or two" the run script
    warns about, so the 30 s default would kill a healthy load. The config can set it; a
    config with an engine gets a minute and a half. The server and the calibrator both use
    this, so a tuning run is not stopped by a timeout the server would not use.
    """
    v = cfg.get("startup_timeout_s") or cfg.get("model_startup_s")
    if v is not None:
        try:
            return max(1.0, float(v))
        except (TypeError, ValueError):
            raise ValueError(f'"startup_timeout_s" must be a number of seconds, not {v!r}')
    return 180.0 if cfg.get("exe") else 30.0


# ---------------------------------------------------------------------- socket hygiene
# A Unix socket name outlives the process that bound it: the file stays in /tmp after the
# worker exits, and the next bind on that name fails with EADDRINUSE. Two supervisors that
# share one name also share the "{socket}.config.json" side file, so a collision is not
# only a bind failure - it is a worker reading another run's config.
#
# Two rules follow, and both are enforced here rather than in each caller:
#   1. every socket path is namespaced by the process that owns it;
#   2. whatever sits at the path is removed before the worker is spawned, and the name is
#      removed again when the supervisor stops.
def unique_socket_path(prefix: str = "hypercore") -> str:
    """A socket path no other process on this machine can already be holding.

    /tmp is used on purpose: this is a live IPC endpoint, not a file a build produces,
    and the runtime modules are contractually not allowed to read the project's tmp/,
    which is deleted when a build finishes. The socket and its side file are unlinked
    when the supervisor stops.
    """
    return f"/tmp/{prefix}-{os.getpid()}-{os.urandom(4).hex()}.sock"


def evict_stale_socket(path: str) -> tuple[bool, str]:
    """Remove whatever is at `path`. Returns (cleared, why it could not be cleared).

    A plain unlink covers the ordinary leftover. The two that are not ordinary are a
    directory left where a socket should be, and a socket owned by another user under the
    sticky bit on /tmp. Neither can be removed by this process, and both make bind() fail
    with EADDRINUSE - which is the failure a shared /tmp produces on a CI runner.
    """
    if not os.path.exists(path):
        return True, ""
    try:
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.unlink(path)
        return True, ""
    except OSError as exc:
        return False, str(exc)


class HyperCoreProcessSupervisor:
    """Start, stop, restart and talk to one engine worker.

    This class owns the lifecycle. The web server does not run its own liveness loop or its
    own restart budget: two restart policies on one process is how a worker gets resurrected
    twice. The server observes `watchdog_state()` and acts on `start_worker()` returning False.
    """

    def __init__(
        self,
        worker_binary: str = "",
        socket_path: str = "",
        config: str = "{}",
        stderr_log: str | None = None,
        max_restarts: int = 3,
        backoff_s: tuple[float, ...] = (0.5, 1.0, 2.0),
        startup_timeout_s: float = 30.0,
        silence_s: float = 0.0,
        env: dict | None = None,
        logger: logging.Logger | None = None,
        transport: Callable[[], socket.socket] | None = None,
    ) -> None:
        """`transport` injects a connection instead of spawning a worker. Production passes
        nothing; the integration tests pass a ScriptedWorker so the edge cases (slot drain,
        cancel during prefill, link death) can be exercised without a GPU or a subprocess.
        The protocol under test is the same one the real worker speaks."""
        self.worker_binary = worker_binary
        # No path given means no caller cares which name is used, so pick one this process
        # owns outright. An empty path is not a valid socket: the worker would bind "" and
        # fail with EINVAL, which reads as a mystery rather than as a missing argument.
        self.socket_path = socket_path or unique_socket_path("hypercore-supervisor")
        self.config = config
        self.stderr_log = stderr_log
        self.max_restarts = max_restarts
        self.backoff_s = backoff_s
        # How long the worker is allowed to take before it binds its socket. With no model
        # that is milliseconds; with a real pack it is the weights, the expert arena and the
        # VRAM tier loading, which is the "minute or two" the run script warns about. A model
        # config must raise this, or a slow-but-healthy load is killed as a failure.
        self.startup_timeout_s = startup_timeout_s
        # #481: how long a running request may go without a frame before it is called stalled.
        # 0 means no wall: a prompt that takes minutes to read is allowed to take minutes. This
        # is NOT the startup timeout - a 116k-token prefill is ~31 s on this machine, and a wall
        # borrowed from the 30 s handshake kills a healthy request mid-prefill.
        self.silence_s = float(silence_s)
        # #1733: the model config's own engine settings (HYPERCORE_ROUTE_TAIL_SKIP, the hipBLASLt
        # tuning table, the resident-pin arm). Calibration writes them into the config; without this
        # the worker would inherit only the shell the server was started from.
        self.env = {str(k): str(v) for k, v in (env or {}).items() if v is not None}
        self.log = logger or logging.getLogger("hypercore.supervisor")

        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._listener: HyperCoreIPCListener | None = None
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._in_flight: set[int] = set()
        self._flight_lock = threading.Lock()
        # Per-request timestamps for slots_view(): when a request was dispatched and when
        # its first token arrived. A slot has no state without them, and /metrics reads
        # x["state"] on every entry.
        self._flight_started: dict[int, float] = {}
        self._flight_first: dict[int, float] = {}
        self._restarts = 0
        self._lifecycle_lock = threading.Lock()
        self.caps: dict[str, int | float] = {}
        self.last_summary: dict[str, object] | None = None
        self.started_at: float = 0.0
        self.last_frame_at: float = 0.0
        self.max_batch_slots: int = 0
        self.max_context_len: int = 0
        self.instance_pcie_frac: float = -1.0
        self.instance_spec_min_p: float = -1.0
        self._transport = transport
        self._transport_obj = transport if hasattr(transport, "connect") else None
        self._stderr_handle = None
        # Two supervisors in one process are a real case (a restart path, a test harness),
        # so the side file is keyed on the instance, not just the pid.
        self._instance_id = f"{os.getpid()}-{os.urandom(4).hex()}"
        self._config_side_files: list[str] = []

    # ---------------------------------------------------------------- lifecycle
    def _config_argument(self) -> str:
        """The config in a form the worker can actually receive.

        Anything up to the kernel's per-argument limit is passed inline, byte for byte.
        A larger one would fail exec outright, so it is written to a file next to the
        socket and passed by path. The file is rewritten on every start, so a restart
        reads the config that was resolved for this process, not an older one.

        The side file carries this supervisor's identity. Two supervisors handed the same
        socket name would otherwise write to one file, and whichever started last would
        silently set the other's configuration.
        """
        if len(self.config.encode("utf-8")) <= ARGV_SAFE_CONFIG_BYTES:
            return self.config
        path = f"{self.socket_path}.{self._instance_id}.config.json"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.config)
        self._config_side_files.append(path)
        return path

    def allow_deliberate_restart(self) -> None:
        """Make room for one restart the user asked for.

        max_restarts is a crash budget: it stops a worker that keeps dying from being respawned forever. A Stop and
        Start from the page is not a crash, and three of them must not use the budget up and leave every later
        request running against a worker that is no longer there.
        """
        self.max_restarts = max(self.max_restarts, self._restarts + 1)

    def start_worker(self, timeout_s: float | None = None) -> bool:
        """Start the worker, or bring it back if it died. False means the budget is spent.
        This is the only lifecycle entry point a server should use.
        """
        timeout_s = self.startup_timeout_s if timeout_s is None else timeout_s
        with self._lifecycle_lock:
            if self.alive():
                return True
            if self._restarts >= self.max_restarts:
                self.log.warning("restart budget exhausted (%d/%d)", self._restarts, self.max_restarts)
                return False
            if self._restarts > 0:
                time.sleep(self.backoff_s[min(self._restarts - 1, len(self.backoff_s) - 1)])
            self._restarts += 1
            self.stop()
            try:
                self.start(timeout_s)
                return True
            except WorkerUnreachable as exc:
                self.log.warning("worker did not come back: %s", exc)
                return False

    def restart(self) -> bool:
        """Alias for start_worker(); kept so existing callers do not break."""
        return self.start_worker()

    def child_environment(self) -> dict:
        """The environment the worker process gets: this server's, with the config's engine settings on top."""
        return {**os.environ, **self.env}

    def start(self, timeout_s: float | None = None) -> None:
        # The allowance is the supervisor's own (model_startup_s for a config with an engine),
        # not a 30 s constant: a caller that just says start() means "start this worker", and
        # a worker that has to load weights needs minutes. tools/calibrate.py calls this
        # directly, which is why the tuning run stopped at "within 30.0s".
        timeout_s = self.startup_timeout_s if timeout_s is None else float(timeout_s)
        if self._listener is not None and self.alive():
            return

        if self._transport is not None:
            self._sock = self._transport() if callable(self._transport) else self._transport.connect()
        else:
            # Sanitation gate. The worker also unlinks its own path, but it cannot remove
            # a name it does not own, and it cannot tell a stale file from a live listener.
            # Doing it here lets a collision be resolved by choosing a different name instead
            # of dying on bind.
            cleared, why = evict_stale_socket(self.socket_path)
            if not cleared:
                self.log.warning(
                    "socket %s could not be cleared (%s); moving to a process-private path",
                    self.socket_path, why,
                )
                self.socket_path = unique_socket_path("hypercore-supervisor")

            stderr_target = open(self.stderr_log, "w") if self.stderr_log else subprocess.PIPE  # noqa: SIM115 - the handle is kept so last_stderr() can read it
            self._proc = subprocess.Popen(
                [self.worker_binary, self.socket_path, self._config_argument()],
                stdout=subprocess.DEVNULL,
                stderr=stderr_target,
                env=self.child_environment() if self.env else None,
            )
            self._stderr_handle = stderr_target if self.stderr_log else None

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    raise WorkerUnreachable(
                        f"worker exited during startup ({self._proc.returncode}): {self.last_stderr()}"
                    )
                try:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._sock.connect(self.socket_path)
                    break
                except OSError:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                    self._sock = None
                    time.sleep(0.1)
            else:
                raise WorkerUnreachable(f"worker did not accept a connection within {timeout_s}s")

        self._listener = HyperCoreIPCListener(self._sock)
        self._listener.start()
        self.last_frame_at = time.monotonic()
        self.caps = self.get_caps()
        self.started_at = time.monotonic()
        self.log.info(
            "HyperCore instance caps synced: slots=%d pcie_baseline=%s spec_baseline=%s",
            self.max_batch_slots, self.instance_pcie_frac, self.instance_spec_min_p,
        )

    def watchdog_state(self) -> dict[str, object]:
        """What a passive observer needs: is it up, how many times it has been restarted,
        and when the last frame arrived. The server logs this; it never acts on it."""
        return {
            "alive": self.alive(),
            "restarts": self._restarts,
            "restart_budget": self.max_restarts,
            "budget_exhausted": self._restarts >= self.max_restarts,
            "frames_read": self._listener.frames_read if self._listener else 0,
            "bytes_read": self._listener.bytes_read if self._listener else 0,
            "link_dead": self._listener.dead.is_set() if self._listener else True,
            "seconds_since_last_frame": (time.monotonic() - self.last_frame_at) if self.last_frame_at else None,
            "uptime_s": (time.monotonic() - self.started_at) if self.started_at else 0.0,
        }

    def shutdown(self) -> None:
        """Alias for stop(): the name callers reach for at process exit."""
        self.stop()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
            if self._proc.stderr is not None:
                try:
                    self._proc.stderr.close()
                except OSError:
                    pass
            self._proc = None
        # The socket name survives the worker's exit. Leaving it in /tmp is exactly how a
        # later run on the same path finds a file it cannot bind over, so the supervisor
        # takes the name back when it is done with it.
        if self._transport is None and self.socket_path:
            evict_stale_socket(self.socket_path)
        for side in getattr(self, "_config_side_files", ()):  # the config file we wrote for this worker
            evict_stale_socket(side)
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
            self._stderr_handle = None
        if self._transport_obj is not None:
            try:
                self._transport_obj.close()
            except OSError:
                self.log.debug("mock transport close failed")

    def alive(self) -> bool:
        if self._sock is None or self._listener is None or self._listener.dead.is_set():
            return False
        return self._proc is None or self._proc.poll() is None

    def last_stderr(self) -> str:
        """Why the worker went away. The server logs this instead of 'the engine stopped'."""
        if self.stderr_log:
            try:
                with open(self.stderr_log, "r", errors="replace") as f:
                    return f.read()[-2000:]
            except OSError:
                return ""
        if self._proc is not None and self._proc.stderr is not None:
            try:
                return self._proc.stderr.read().decode(errors="replace")[-2000:]
            except (OSError, ValueError):
                return ""
        return ""

    # ---------------------------------------------------------------- the wire
    def next_request_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def send_ipc_message(self, msg_type: int, request_id: int, payload: bytes = b"") -> bool:
        """Write one framed message. True on success, False if the link is gone.

        A single sendall on one frame: the worker's reader takes a whole header and then
        exactly payload_length bytes, so a partial write would desynchronise it.
        """
        if self._sock is None or (self._listener is not None and self._listener.dead.is_set()):
            return False
        frame = build_frame(msg_type, request_id, payload)
        with self._write_lock:
            try:
                self._sock.sendall(frame)
            except OSError:
                if self._listener is not None:
                    self._listener.dead.set()
                return False
        return True

    def request_once(self, msg_type: int, payload: bytes = b"", timeout_s: float = 10.0) -> bytes:
        """Attach, send, and wait for one reply. Attaching before sending matters: the
        mock and the real worker both answer immediately, and a frame routed to a queue
        that does not exist yet is dropped."""
        listener = self._listener
        if listener is None:
            raise WorkerUnreachable("no link to the worker")
        request_id = self.next_request_id()
        reply = listener.attach(request_id)
        try:
            if not self.send_ipc_message(msg_type, request_id, payload):
                raise WorkerUnreachable("link closed before the request was sent")
            item = reply.get(timeout=timeout_s)
            if item[0] is None:
                raise WorkerUnreachable("worker died while waiting for a reply")
            want = expected_reply(msg_type)
            if want is None:
                raise WorkerUnreachable(f"{msg_type} has no reply in the message map")
            if item[0] != want:
                raise WorkerUnreachable(f"expected frame type {want} for {msg_type}, got {item[0]}")
            return item[2]
        except queue.Empty:
            raise WorkerUnreachable(f"worker did not answer within {timeout_s}s") from None
        finally:
            listener.detach(request_id)

    def submit(self, prompt_tokens: list[int], **tuning) -> tuple[int, int]:
        """Send a request and return (request_id, status). 0 means scheduled, -2 BUSY.
        The caller keeps the id, which is what makes cancellation possible."""
        listener = self._listener
        if listener is None:
            raise WorkerUnreachable("no link to the worker")
        request_id = self.next_request_id()
        reply = listener.attach(request_id)
        try:
            payload = build_submit_payload(prompt_tokens=prompt_tokens, **tuning)
            if not self.send_ipc_message(CMD_SUBMIT, request_id, payload):
                return request_id, -1
            item = reply.get(timeout=10.0)
            if item[0] is None:
                raise WorkerUnreachable("worker died while scheduling the request")
            return request_id, decode_status(item[2])
        except queue.Empty:
            raise WorkerUnreachable("worker did not acknowledge the request") from None
        finally:
            listener.detach(request_id)

    def get_caps(self) -> dict[str, int | float]:
        caps = decode_caps(self.request_once(CMD_CAPS, timeout_s=10.0))
        if caps["status"] != 0:
            raise WorkerUnreachable(f"caps request failed with status {caps['status']}")
        # Keep the resolved baseline on the instance so a request handler can read the
        # starting point of a sweep without going back to the worker for it.
        self.max_batch_slots = caps["max_batch_slots"]
        self.max_context_len = caps["max_context_len"]
        self.instance_pcie_frac = caps["instance_pcie_frac"]
        self.instance_spec_min_p = caps["instance_spec_min_p"]
        return caps

    def ping(self) -> bool:
        try:
            self.request_once(CMD_PING, timeout_s=5.0)
            return True
        except WorkerUnreachable:
            return False

    def cancel(self, request_id: int) -> bool:
        return self.send_ipc_message(CMD_CANCEL, request_id)

    def open_stream(self, request_id: int, maxsize: int = 1024) -> queue.Queue:
        """The queue a request's frames arrive on. For callers that build the request
        frame themselves instead of going through stream()."""
        if self._listener is None:
            raise WorkerUnreachable("no link to the worker")
        q = self._listener.attach(request_id, maxsize)
        with self._flight_lock:
            self._in_flight.add(request_id)
            self._flight_started[request_id] = time.monotonic()
            self._flight_first.pop(request_id, None)
        return q

    def close_stream(self, request_id: int) -> None:
        if self._listener is not None:
            self._listener.detach(request_id)
        with self._flight_lock:
            self._in_flight.discard(request_id)
            self._flight_started.pop(request_id, None)
            self._flight_first.pop(request_id, None)

    def note_frame(self, summary: dict[str, Any] | None = None) -> None:
        """Record that a frame arrived, and the terminal summary if there is one.

        Callers that build their own request frames (the HTTP handler) must call this,
        or the Monitor sees an empty telemetry panel and the watchdog sees a link that
        has gone quiet while requests are running.
        """
        self.last_frame_at = time.monotonic()
        if summary is not None:
            self.last_summary = summary

    def in_flight(self) -> int:
        """Requests the host has dispatched and not yet reaped. Load shedding reads this;
        it is not the engine's batch-slot count, and the two disagree whenever the worker
        is mid-restart."""
        with self._flight_lock:
            return len(self._in_flight)

    def monitor_snapshot(self) -> dict[str, object]:
        """The fields the Monitor dashboard and the alert rules were built on.

        Every key comes from MONITOR_FIELDS in serve/ipc_parser.py. Adding a metric is a
        row in that table; this function does not change. A field the worker never sent is
        None, never 0: a zero is a measurement and a None is the absence of one, and a
        dashboard that reads 0 for an unmeasured field will alert on a machine that was
        instantaneous.
        """
        summary = self.last_summary or {}
        watchdog = self.watchdog_state()
        proc = self._proc
        exit_code = proc.returncode if proc is not None else None
        died = proc is not None and proc.poll() is not None
        # Lifecycle never travels on the wire; the process is the only source for these.
        process_state = {
            "exit_code": exit_code,
            # A pager-duty panic is a worker that is gone and came back nonzero. The
            # restart counter counts every bring-up, including the first, so it is a
            # lifecycle signal, not a fault signal, and must not drive the alert.
            "panic": died and exit_code not in (0, None),
        }
        snapshot: dict[str, object] = {}
        for key, (source, field) in MONITOR_FIELDS.items():
            if source == "summary":
                snapshot[key] = summary.get(field)
            elif source == "process":
                snapshot[key] = process_state.get(field)
            elif source == "watchdog":
                snapshot[key] = watchdog.get(field)
            else:
                snapshot[key] = derive_monitor_field(field, summary, watchdog)
        # Not Monitor keys, but the load-shedding path reads them from the same snapshot.
        snapshot["in_flight"] = self.in_flight()
        snapshot["max_batch_slots"] = self.max_batch_slots
        return snapshot

    @property
    def process(self) -> subprocess.Popen | None:
        """The worker process, for a caller that needs to inspect or signal it."""
        return self._proc

    # ---------------------------------------------------------------- streaming
    def stream(
        self,
        prompt_tokens: list[int],
        temperature: float = 0.0,
        top_p: float = 1.0,
        top_k: int = 1,
        max_new_tokens: int = 64,
        pcie_frac: float = -1.0,
        spec_min_p: float = -1.0,
        timeout_s: float = 30.0,
        silence_s: float | None = None,
        request_id: int | None = None,
    ) -> Iterator[tuple[int, int, dict[str, Any]]]:
        """Yield (msg_type, token_id, metrics) for one request, ack to terminal.

        msg_type is STREAM_TOKEN or STREAM_TERMINAL. A token frame carries the
        prompt-reuse count in metrics; the terminal frame carries the decoded
        summary. This is the only place a frame tuple becomes an application
        message: the HTTP layer, the calibration adapter and generate() all read
        through it, so a wire change lands in one loop instead of three.

        `timeout_s` is the wall for the worker's ack to a submit, not for the stream.
        `silence_s` is how long the stream may go without a frame; None takes the
        supervisor's own wall (self.silence_s, from the config's "engine_silence_s"),
        and 0 waits as long as the request needs. Reading a large prompt emits no
        token frames at all, so a frame wall taken from the startup timeout would
        stall a healthy request in the middle of its prefill.

        The timeout walls and the CANCEL frame live here. A caller that stops
        consuming early - a dropped client - does not have to know about
        CMD_CANCEL: the finally block sends it, so slot reclamation is never the
        caller's job.

        Raises WorkerBusy when the worker's slot gate refuses the request,
        WorkerRejected when the configuration is invalid, StreamStalled when the
        stream goes silent, WorkerUnreachable when the link itself dies.
        """
        wall = self.silence_s if silence_s is None else float(silence_s)
        request_id = request_id or self.next_request_id()
        listener = self._listener
        if listener is None:
            raise WorkerUnreachable("no link to the worker")

        frames = self.open_stream(request_id)
        terminal_seen = False
        accepted = False
        link_dead = False          # the drain below is only for a link that is still there
        try:
            payload = build_submit_payload(
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_new_tokens=max_new_tokens,
                pcie_frac=pcie_frac,
                spec_min_p=spec_min_p,
                prompt_tokens=prompt_tokens,
            )
            if not self.send_ipc_message(CMD_SUBMIT, request_id, payload):
                link_dead = True
                raise WorkerUnreachable("link closed before the request was sent")

            try:
                item = frames.get(timeout=timeout_s)
            except queue.Empty:
                # The same rule as the frame loop below: a link that answers a ping is
                # slow, a link that does not is gone. Without this guard an ack timeout
                # escapes as a bare queue.Empty, which the server turns into a 500 with
                # no cause - the one case where "the worker never answered" is exactly
                # the answer the caller needs.
                if not self.ping():
                    link_dead = True
                    raise WorkerUnreachable("worker never acknowledged the request") from None
                raise StreamStalled(f"no ack for {timeout_s}s") from None
            if item[0] is None:
                link_dead = True
                raise WorkerUnreachable("worker died while scheduling the request")
            if item[0] != expected_reply(CMD_SUBMIT):
                link_dead = True
                raise WorkerUnreachable(f"expected an ack frame, got {item[0]}")
            status = decode_status(item[2])
            if status == -2:
                raise WorkerBusy("the worker has no free batch slot")
            if status != 0:
                raise WorkerRejected(f"engine rejected the request: status {status}")
            accepted = True
            self.last_frame_at = time.monotonic()

            while True:
                # The wall is the silence limit measured from the last frame, not the
                # startup timeout: a prefill that needs 31 s gets 31 s.
                wait = None if wall <= 0 else max(0.1, wall - (time.monotonic() - self.last_frame_at))
                try:
                    tag, token_id, frame_payload = frames.get(timeout=wait)
                except queue.Empty:
                    # There is no heartbeat frame on the stream, so a silent link is
                    # tested out-of-band before the request is called stalled: a worker
                    # that answers a ping is slow, a worker that does not is gone.
                    # The finally block sends the CANCEL; doing it here too would put
                    # two cancel frames on the wire for one dead stream.
                    if not self.ping():
                        link_dead = True
                        raise WorkerUnreachable("worker stopped answering") from None
                    raise StreamStalled(f"no frame for {wait}s") from None
                if tag is None:
                    link_dead = True
                    # The reader records why the link closed (EOF, a truncated payload,
                    # a bad header). Carrying it here is the difference between a 500
                    # that can be traced and one that cannot.
                    why = frame_payload.get("error") if isinstance(frame_payload, dict) else None
                    raise WorkerUnreachable(
                        "worker died mid-stream" + (f": {why}" if why else "")
                    )
                self.last_frame_at = time.monotonic()
                if tag == STREAM_TOKEN:
                    self.note_frame()
                    if token_id >= 0:
                        with self._flight_lock:
                            self._flight_first.setdefault(request_id, time.monotonic())
                        reused = frame_payload if isinstance(frame_payload, int) else 0
                        yield (STREAM_TOKEN, token_id, {"prompt_reused": reused})
                    continue
                if tag == STREAM_TERMINAL:
                    summary = frame_payload if isinstance(frame_payload, dict) else {}
                    self.note_frame(summary)
                    terminal_seen = True
                    yield (STREAM_TERMINAL, -1, summary)
                    return
                link_dead = True
                raise WorkerUnreachable(f"unexpected frame type {tag} on a stream queue")
        finally:
            # A request the worker never accepted has no slot to reclaim;
            # a stalled or abandoned one does.
            if accepted and not terminal_seen:
                self.cancel(request_id)
                # The worker answers a cancel with its own terminal frame, and this is the
                # only place it can be read: a caller that stopped consuming early (a turn
                # that ended on the end-of-turn token, a stop string, a thinking budget) has
                # closed the generator before the stream's end, and without this the Monitor
                # has no Reused, Tok/s or hit rate for that request. A dead or silent link is
                # not drained: there is nothing on it to read, and waiting would hold up the
                # recovery that the caller is about to do.
                if not link_dead:
                    try:
                        tag, _, summary = frames.get(timeout=min(5.0, timeout_s))
                    except queue.Empty:
                        pass
                    else:
                        if tag == STREAM_TERMINAL:
                            self.note_frame(summary if isinstance(summary, dict) else {})
            self.close_stream(request_id)
    # ---------------------------------------------------------------- the Engine surface
    def generate(
        self,
        ids: list[int],
        max_new: int,
        sampling: dict[str, float],
        cancel: threading.Event,
        embeddings: list[float] | None = None,
    ) -> Iterator[int]:
        """Token ids only, the way serve/server.py's Engine protocol asks for it.

        A dropped client sets `cancel`; the supervisor turns that into a CANCEL frame and
        stops yielding. The terminal summary is kept in self.last_summary so the Monitor can
        report drafts and stop reason without parsing a line of text.
        """
        request_id = self.next_request_id()
        self.last_summary = None
        try:
            for msg_type, token_id, metrics in self.stream(
                ids,
                temperature=float(sampling.get("temperature", 1.0)),
                top_p=float(sampling.get("top_p", 1.0)),
                top_k=int(sampling.get("top_k", 8)),
                max_new_tokens=max_new,
                pcie_frac=float(sampling.get("pcie_frac", -1.0)),
                spec_min_p=float(sampling.get("spec_min_p", -1.0)),
                request_id=request_id,
            ):
                if msg_type == STREAM_TERMINAL:
                    self.last_summary = metrics
                    return
                if cancel.is_set():
                    self.cancel(request_id)
                    continue
                yield token_id
        finally:
            if self.last_summary is None:
                self.cancel(request_id)

    def slots_view(self) -> list[dict[str, object]]:
        """Batch slots, in the shape /metrics and the Monitor expect.

        The worker's CAPS reply says how many slots an instance has and never which
        request took which one - the choice is made inside the engine - so the host can
        report how many slots are busy and what each busy one is doing, not their
        identity. That is the same rule serve/flood_stub.py's /slots already follows
        (`is_processing: i < in_flight`); the difference is that every entry here carries
        a state, because serve/server.py sums on it.
        """
        total = int(self.caps.get("max_batch_slots", 0))
        with self._flight_lock:
            live = [(rid, self._flight_started.get(rid), self._flight_first.get(rid))
                    for rid in self._in_flight]
        now = time.monotonic()
        view: list[dict[str, object]] = []
        for i in range(total):
            if i < len(live):
                rid, started, first = live[i]
                # No token yet means the prompt is still being read: the worker accepted
                # the request but has not produced anything.
                view.append({"slot": i, "state": "generating" if first else "reading",
                            "request_id": rid,
                            "max_context": int(self.caps.get("max_context_len", 0)),
                            "elapsed_s": round(now - started, 1) if started else None})
            else:
                view.append({"slot": i, "state": "idle",
                            "max_context": int(self.caps.get("max_context_len", 0))})
        return view
