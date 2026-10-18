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
        self.info: dict[str, Any] = {}
        self.started_at = time.time()

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
        return {"state": "offline", "reason": self.reason, "detail": self.detail,
                "model": self.model, "retry_after_s": self.retry_after_s}


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
        except Exception as exc:  # noqa: BLE001 - a retry must never take the server with it
            self.detail = str(exc)
            self.last_error = _brief(exc)
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
            except Exception as exc:  # noqa: BLE001 - a retry must never take the server with it
                self.detail = str(exc)
                self.last_error = _brief(exc)

    def status(self) -> dict[str, Any]:
        state = ("paused" if self.paused_reason else
                "running" if self.succeeded else
                "retrying" if self.running else "stopped")
        return {"state": state,
               "reason": self.last_error,
               "detail": self.detail,
               "paused": bool(self.paused_reason),
               "paused_reason": self.paused_reason,
               "attempts": self.attempts,
               "interval_s": self.interval_s,
               "next_attempt_s": max(0.0, round(self._next - time.time(), 1))}
