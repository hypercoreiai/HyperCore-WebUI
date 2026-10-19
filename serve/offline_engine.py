"""serve/offline_engine.py - the web app answers when no engine is running.

The HTTP server and the engine are one process, and the engine is started before the
port is bound: if the GPU is held by a tune sweep, a build, or another server holding
the card, the process exits and the browser gets nothing. This is the stand-in that lets
the server bind, serve the UI, and answer everything that does not need weights - and
say plainly which request does.

Two pieces:

  OfflineEngine   the Engine protocol with no model behind it. The page, /health, /mcp,
                  /prompts, the Monitor, the Tune and Build tabs all work; a request that
                  needs the model raises EngineUnavailable, which the handler answers 503
                  with Retry-After.
  EngineRetry     a background thread that keeps trying to bring the engine back while
                  the UI stays up, so a card freed by a finished sweep is picked up
                  without restarting the WebUI. It holds off while a tune sweep or a build
                  is using the card: an attempt then would fail and would disturb what the
                  sweep is measuring.

Nothing here reads a GPU. The whole module is testable without one.
"""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any

#: How long the server waits before it tries the engine again, and what it puts in
#: Retry-After. A model load takes minutes, so a short interval just adds noise.
DEFAULT_RETRY_S = 30.0


class EngineUnavailable(RuntimeError):
    """No engine is running, and this request needs one."""

    def __init__(self, reason: str, retry_after_s: float = DEFAULT_RETRY_S):
        super().__init__(reason)
        self.retry_after = float(retry_after_s)


#: A machine-readable cause of an offline engine. The banner and the 503 body carry the
#: sentence a person can act on; a client that branches on the reason reads this instead.
#: "unloaded_or_compiling" is the catch-all: the engine was asked for and is not here yet.
UNLOADED_CODE = "unloaded_or_compiling"


def reason_code(detail: str | None) -> str:
    """Classify an engine's failure in one token, from the words the engine itself used."""
    low = (detail or "").lower()
    if "ui-only" in low or "no engine was asked for" in low:
        return "ui_only"
    if "gpu is held" in low or "busy" in low or "card is in use" in low:
        return "gpu_busy"
    if "stopped" in low:
        return "stopped"
    if "no run config" in low or "no model" in low or "missing model" in low:
        return "no_model"
    return UNLOADED_CODE


def _brief(text: str) -> str:
    """The last line of an engine's error, which is where engines put their conclusion
    ("the machine has no room for a second engine: stop the other one"). The full text
    is kept in detail for the Monitor; the banner and the 503 body carry the one line a
    person can act on.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "the engine is not running"


class OfflineEngine:
    """The Engine protocol, with no weights behind it.

    `max_context` is 0 until the real engine is read: a request that reaches the model
    is refused before the context is ever measured, so the value is only ever reported,
    never used to decide.
    """

    def __init__(self, reason: str, model: str = "no model loaded",
                 max_context: int = 0, retry_after_s: float = DEFAULT_RETRY_S):
        self.detail = str(reason or "")
        self.reason = _brief(reason) or "the engine is not running"
        self.model = model or "no model loaded"
        self.max_context = int(max_context)
        self.retry_after_s = float(retry_after_s)
        #: What the most recent attempt said, in the engine's own words. Empty until an
        #: attempt has been made: until then the only reason there is is the one the
        #: server started with.
        self.last_error: str = ""
        self.info: dict[str, Any] = {}
        self.started_at = time.time()

    def report(self) -> str:
        """The reason to show a person: what the last attempt said, or the startup reason.

        "started with --ui-only" is true at the moment the server binds, but it stops
        being the useful sentence the moment an attempt has failed for a specific reason:
        the banner, the 503 body and the Monitor all read this instead of `reason`.
        """
        return self.last_error or self.reason

    # ---------------------------------------------------------------- Engine protocol
    def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise EngineUnavailable(self.reason, self.retry_after_s)

    def alive(self) -> bool:
        return False

    def restart(self) -> bool:
        """A stand-in cannot restart itself; the retry thread holds the real engine."""
        raise EngineUnavailable(self.reason, self.retry_after_s)

    def unload(self) -> bool:
        return False

    def shutdown(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {"state": "offline", "code": reason_code(self.detail),
                "reason": self.reason, "detail": self.detail,
                "model": self.model, "retry_after_s": self.retry_after_s}


class DeferredEngine(OfflineEngine):
    """An engine that does not exist yet, and can still be had.

    A server started engineless has no engine to name: at startup nothing is installed,
    so there is nothing to load, and `OfflineEngine` can only refuse forever. A build
    changes that - it puts an engine in `engine/engine_<quant>/`, writes the run config
    that names it, and makes that config the active one. That is the one case where an
    engine can appear after the server is running, so this is the candidate
    `EngineRetry` can be given for it: each attempt asks the factory for the engine the
    run config names now, and once one is there it is started and this object delegates
    to it.

    It is an `OfflineEngine`, so every check the server makes for "no engine is running"
    still reads correctly, and the routes that do not need weights keep answering. Only
    `generate`, `alive`, `restart` and `status` change once a real engine is behind it.
    The tokenizer and the chat template are the ones the server started with; they are
    not re-read here.
    """

    def __init__(self, factory: Callable[[], Any | None], reason: str,
                 model: str = "no model loaded", retry_after_s: float = DEFAULT_RETRY_S):
        super().__init__(reason, model=model, retry_after_s=retry_after_s)
        self.factory = factory
        self.engine: Any = None
        #: True only while an engine behind this object has been started and is up. An
        #: attempt that got as far as an engine object but could not start it leaves this
        #: False, so a chat is refused with that attempt's reason rather than reaching a
        #: half-built engine.
        self.loaded = False

    def build(self) -> Any:
        """One attempt: take the engine the run config names now, and start it.

        Every failure is written to `last_error`, so the banner and the 503 body say what
        the engine actually said instead of the reason the server started with.

        An attempt that got an engine up keeps it: the next attempt starts that one again
        rather than building a second copy. An attempt that did not get one up is dropped.
        That is the difference between an engine that is temporarily down and an engine
        built from a run config that is no longer the one to run: a build writes a new
        config, and the supervisor behind the old one has spent its restart budget on
        paths that do not exist, so restarting it repeats the same failure forever.
        """
        if self.engine is not None and not self.loaded:
            with contextlib.suppress(Exception):  # a dead engine's own error must not hide the reason
                shutdown = getattr(self.engine, "shutdown", None)
                if callable(shutdown):
                    shutdown()
            self.engine = None
        if self.engine is None:
            try:
                self.engine = self.factory()
            except Exception as exc:  # a failed read is an attempt, not a crash
                self.last_error = str(exc) or "the run config could not be read"
                self.detail = self.last_error
                raise EngineUnavailable(self.last_error, self.retry_after_s) from exc
            if self.engine is None:
                self.last_error = ("nothing has been built yet: build an engine, or start "
                                   "this server again with a run config")
                self.detail = self.reason
                raise EngineUnavailable(self.last_error, self.retry_after_s)
        # Which way an engine is brought up depends on which engine the run config names:
        # serve/direct_engine.py exposes start(), while the worker adapter
        # HyperCoreRequestHandler exposes restart() and does the READY handshake inside it.
        begin = getattr(self.engine, "start", None) or getattr(self.engine, "restart", None)
        if not callable(begin):
            self.last_error = "the engine the run config names cannot be started"
            raise EngineUnavailable(self.last_error, self.retry_after_s)
        try:
            begin()
        except Exception as exc:  # the engine's own reason is what to report
            self.loaded = False
            self.last_error = str(exc) or "the engine did not come up"
            said = getattr(self.engine, "detail", None) or getattr(self.engine, "death_note", None)
            self.detail = str(said) or self.last_error
            raise EngineUnavailable(self.last_error, self.retry_after_s) from exc
        self.loaded = True
        self.model = getattr(self.engine, "model", self.model) or self.model
        self.max_context = int(getattr(self.engine, "max_context", 0) or 0)
        return self.engine

    def start(self) -> None:
        self.build()

    def alive(self) -> bool:
        return bool(self.engine and self.engine.alive())

    def restart(self) -> bool:
        try:
            engine = self.build()
        except EngineUnavailable:
            return False
        return bool(engine.alive())

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        if self.engine is None or not self.loaded:
            # The reason an attempt failed is more use than the reason the server started
            # with: a chat refused with "no engine was asked for" after a build has just
            # produced one sends the user to the wrong place.
            raise EngineUnavailable(self.report(), self.retry_after_s)
        return self.engine.generate(*args, **kwargs)

    def unload(self) -> bool:
        return bool(self.engine and self.engine.unload())

    def shutdown(self) -> None:
        if self.engine is not None:
            self.engine.shutdown()

    def status(self) -> dict[str, Any]:
        """What the engine behind it can say, in the shape the health route reads.

        `OfflineEngine.status()` is the shape the banner and `/health` read, and a real
        engine is not obliged to answer in it: the worker supervisor and the request handler
        over it have no `status()` at all - only `alive()`. So whatever the delegate can say
        is merged into this shape, and the state is taken from `alive()` when it cannot say
        anything. A delegate that cannot describe itself is not an error: the health route
        must still answer.
        """
        base = super().status()
        #: The reason the health route and the banner show is the reason this object has to
        #: give: the startup sentence until an attempt has been made, then whatever that
        #: attempt said. "no engine was asked for" is not the fact once one has been asked
        #: for and failed.
        base["reason"] = self.report()
        base["code"] = reason_code(base["reason"])
        if self.engine is None:
            return base
        reported = self._delegate_words()
        base.update(reported)
        base["max_context"] = self.max_context      # what the delegate was read as at build()
        state = reported.get("state") or ("running" if self.alive() else "offline")
        base["state"] = state
        if state != "offline":
            # The stand-in's own cause is not the engine's: once weights are behind it the
            # reason is the engine, in its own words if it has them.
            base["code"] = reported.get("code") or "running"
            base["reason"] = reported.get("reason") or "the engine the build produced is running"
            return base
        # An engine has been taken and it is not up. The sentence this object was built
        # with - "no engine was asked for" - is no longer the fact: an engine was asked
        # for, and it is not running. Say what that engine says; keep this object's own
        # reason only for the case where it cannot say anything, which is the state the
        # banner and the tab mapping still describe.
        note = self._delegate_note()
        if note:
            base["detail"] = note
            base["reason"] = _brief(note)
            base["code"] = reported.get("code") or reason_code(note)
        return base

    def _delegate_note(self) -> str:
        """The last thing the engine said, for a delegate that has no `status()`.

        The worker adapter and the supervisor behind it expose `death_note()`/`last_stderr()`
        instead of a status, and that is the only account a process that exited during
        startup can give. Read in that order, and never raises: a note that cannot be read
        is skipped, and the health route still answers with what it has.
        """
        for name in ("death_note", "last_stderr", "last_error"):
            said = self._read_words(getattr(self.engine, name, None))
            if said:
                return said
        return ""

    @staticmethod
    def _read_words(source: Any) -> str:
        """Call it if it can be called, read it if it is a string. A note that cannot be
        read is skipped, not an error: the health route must still answer."""
        try:
            value = source() if callable(source) else source
        except Exception:  # noqa: BLE001 - an unreadable note is not a dead engine either
            return ""
        return str(value or "")

    def _delegate_words(self) -> dict[str, Any]:
        """The delegate's own status, if it has one. Never raises: a description that fails
        must not take the health route down with it, and the state is still known from
        `alive()`.
        """
        speak = getattr(self.engine, "status", None)
        if not callable(speak):
            return {}
        try:
            said = speak()
        except Exception as exc:  # noqa: BLE001 - a delegate that cannot describe itself
            self.last_error = f"the engine could not describe itself: {exc}"
            return {}
        return dict(said) if isinstance(said, dict) else {}


class EngineRetry:
    """Bring an engine back while the UI stays up.

    candidate   the engine object that failed to start (it has start()/restart())
    adopt       called with the engine once it is alive; it is what puts it into the Service
    interval_s  how long to wait between attempts
    busy        optional callable returning a reason string while something else holds the
                GPU (a sweep, a build), or None. No attempt is made while it answers.

    The thread is a daemon, so it never keeps the process alive on its own, and it stops
    as soon as an engine is adopted or stop() is called.
    """

    def __init__(self, candidate: Any, adopt: Callable[[Any], None], reason: str,
                 interval_s: float = DEFAULT_RETRY_S, busy: Callable[[], str | None] | None = None):
        self.candidate = candidate
        self.adopt = adopt
        self.interval_s = max(1.0, float(interval_s))
        self.busy = busy
        self.paused_reason = ""
        self.reason = _brief(reason)
        self.detail = str(reason or "")
        self.attempts = 0
        self.last_error = self.reason
        self.running = False
        self.succeeded = False
        self._stop = threading.Event()
        self._next = time.time() + self.interval_s
        self.thread = threading.Thread(target=self._run, daemon=True, name="engine-retry")

    def start(self) -> EngineRetry:
        self.running = True
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _held(self) -> str:
        """Why the card is not free right now, or "" when it is. A broken busy-check never
        stops a retry: it is a courtesy, not a requirement."""
        if not callable(self.busy):
            return ""
        try:
            return str(self.busy() or "")
        except Exception:  # noqa: BLE001 - a bad busy-check must not take the retry with it
            return ""

    def _ensure_thread(self) -> None:
        """A failed manual attempt schedules the next one, so pressing Load once does not
        leave the engine unattended."""
        if not self.running and not self.succeeded and self.interval_s > 0:            
            restart = getattr(self.candidate, "restart", None) or getattr(self.candidate, "start", None)
            if restart is not None:
                self.running = True
                self.thread.start()

    def try_now(self) -> dict[str, Any]:
        """One attempt, now, in the calling thread. Used by the Load button: a person who
        pressed it wants an answer, not a wait for the next scheduled attempt. While a
        sweep or a build holds the card the attempt is refused with that reason.
        """
        held = self._held()
        if held:
            self.paused_reason = held
            self.last_error = held
            return self.status()
        self.paused_reason = ""
        self.attempts += 1
        try:
            restart = getattr(self.candidate, "restart", None) or getattr(self.candidate, "start", None)
            if restart is None:
                self.last_error = "the engine cannot be restarted from here; restart the WebUI"
                return self.status()
            if restart():
                self.adopt(self.candidate)
                self.succeeded = True
                self.running = False
                self._stop.set()
            else:
                self.last_error = _brief(getattr(self.candidate, "last_error", None)
                                        or "the engine did not come up")
                said = getattr(self.candidate, "detail", None)
                if said:
                    self.detail = str(said)
        except Exception as exc:  # noqa: BLE001 - a retry must never take the server with it
            self.detail = str(exc)
            self.last_error = _brief(str(exc))
        self._next = time.time() + self.interval_s
        self._ensure_thread()
        return self.status()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            held = self._held()
            if held:
                # Waiting is the correct action while something else has the GPU.
                self.paused_reason = held
                self.last_error = held
                self._next = time.time() + self.interval_s
                continue
            self.paused_reason = ""
            self.attempts += 1
            self._next = time.time() + self.interval_s
            try:
                restart = getattr(self.candidate, "restart", None) or getattr(self.candidate, "start", None)
                if restart is None:
                    self.last_error = "the engine cannot be restarted from here; restart the WebUI"
                    self.stop()
                    return
                if restart():
                    self.adopt(self.candidate)
                    self.succeeded = True
                    self.running = False
                    return
                self.last_error = _brief(getattr(self.candidate, "last_error", None)
                                        or "the engine did not come up")
                said = getattr(self.candidate, "detail", None)
                if said:
                    # The engine's own words, not the reason this server started with.
                    self.detail = str(said)
            except Exception as exc:  # noqa: BLE001 - a retry must never take the server with it
                self.detail = str(exc)
                self.last_error = _brief(str(exc))

    def status(self) -> dict[str, Any]:
        state = ("paused" if self.paused_reason else
                "running" if self.succeeded else
                "retrying" if self.running else "stopped")
        return {"state": state, "code": reason_code(self.last_error),
               "reason": self.last_error,
               "detail": self.detail,
               "paused": bool(self.paused_reason),
               "paused_reason": self.paused_reason,
               "attempts": self.attempts,
               "interval_s": self.interval_s,
               "next_attempt_s": max(0.0, round(self._next - time.time(), 1))}
