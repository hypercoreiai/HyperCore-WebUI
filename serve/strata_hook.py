"""serve/strata_hook.py - run Strata's own setup.py from the Config view.

The Config view has text boxes for the model paths; it cannot fetch a model. Upstream
Strata ships a setup.py that checks the machine, downloads the packs and prepares them
(PLAN section 10, decision 1). This is the hook to it: the request starts that script as
a detached subprocess and returns at once, so a ten-minute download never blocks the
HTTP server. stdout and stderr go to logs/strata-hook.log; the page polls
GET /config/strata-hook and reads the tail from a byte offset, so it appends lines
instead of re-fetching the whole log.

tmp/ is scratch: it holds the clone a build is using and is deleted when the build
finishes. The hook therefore refuses a checkout inside it - a download that has to
survive has to live somewhere the user keeps.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
SCRATCH = ROOT / "tmp"
#: The log lives in logs/ next to the engine logs. HYPERCORE_HOOK_LOG moves it, which
#: is what a test uses so it does not write into the repository's own log file.
LOG_PATH = Path(os.environ.get("HYPERCORE_HOOK_LOG") or (LOGS / "strata-hook.log"))

#: Where a Strata checkout is looked for, in order. An explicit path in the request
#: or the environment wins; the rest are the places it plausibly already is.
SEARCH_ENV = ("HYPERCORE_STRATA_SRC", "STRATA_SRC")
SEARCH_DIRS = ("strata", "Strata", "strata-main")


def _under_scratch(path: Path) -> bool:
    """True when path is inside tmp/, which is deleted when a build finishes."""
    try:
        return path.resolve().is_relative_to(SCRATCH.resolve())
    except (ValueError, OSError):
        return False


def find_checkout(explicit: str | None = None) -> tuple[Path | None, str]:
    """The setup.py of a Strata checkout this machine has, or a message saying why not.

    Returns (path, note). path is None when nothing was found, and note is the sentence
    the page shows instead of a stack trace.
    """
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        if _under_scratch(p):
            return None, (f"{p} is inside tmp/, which is deleted when a build finishes. "
                         "Point the hook at a checkout you keep.")
        setup = p if p.is_file() else p / "setup.py"
        if setup.is_file():
            return setup, ""
        return None, f"no setup.py at {p}"

    for name in SEARCH_ENV:
        value = os.environ.get(name)
        if value:
            found, note = find_checkout(value)
            if found:
                return found, note

    for name in SEARCH_DIRS:
        candidate = ROOT / name
        if (candidate / "setup.py").is_file() and not _under_scratch(candidate):
            return candidate / "setup.py", ""

    return None, ("no Strata checkout with a setup.py was found. Put one next to this "
                 "project (strata/), or set HYPERCORE_STRATA_SRC to its path.")


class StrataHook:
    """One detached run of the upstream setup script, with its log on disk."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"          # idle | running | done | failed | cancelled
        self.proc: subprocess.Popen | None = None
        self.command: list[str] = []
        self.started: float | None = None
        self.exit_code: int | None = None
        self.log_path = LOG_PATH
        self.note = ""

    def start(self, path: str | None = None, args: list[str] | None = None) -> dict[str, Any]:
        """Spawn setup.py and return at once. Nothing here waits for the download."""
        with self.lock:
            if self.state == "running":
                return {"error": "the setup hook is already running", "state": "running"}
            setup, note = find_checkout(path)
            if setup is None:
                self.state, self.note = "idle", note
                return {"error": note, "state": "idle"}
            LOGS.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, str(setup), *(args or ["--yes", "--no-start"])]
            try:
                log = open(self.log_path, "wb")  # noqa: SIM115 - the file is closed by _finish
            except OSError as exc:
                self.state, self.note = "failed", f"the log file could not be opened: {exc}"
                return {"error": self.note, "state": "failed"}
            proc = subprocess.Popen(command, cwd=str(setup.parent), stdout=log, stderr=log)
            self.proc, self.command, self.started = proc, command, time.time()
            self.state, self.exit_code, self.note = "running", None, ""
            threading.Thread(target=self._finish, args=(proc, log), daemon=True).start()
            return {"state": "running", "command": command, "log": str(self.log_path),
                    "pid": proc.pid}

    def _finish(self, proc: subprocess.Popen, log) -> None:
        """Record the exit code when the subprocess ends. The thread is a daemon: a
        server that shuts down does not wait for a download it no longer needs."""
        try:
            code = proc.wait()
        except Exception:  # noqa: BLE001 - a subprocess that will not wait is reported, not raised
            code = -1
        finally:
            log.close()
        with self.lock:
            self.exit_code = code
            if self.state == "running":
                self.state = "done" if code == 0 else "failed"

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            if self.proc and self.state == "running":
                self.proc.terminate()
                self.state = "cancelled"
                return {"cancelled": True}
            return {"cancelled": False}

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"state": self.state, "command": self.command,
                    "pid": self.proc.pid if self.proc else None,
                    "exit_code": self.exit_code,
                    "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
                    "log": str(self.log_path), "note": self.note}

    def tail(self, offset: int = 0) -> dict[str, Any]:
        """The log from a byte offset onward, so the page appends instead of re-reading.

        A log that is mid-write can end in a partial line; the offset stops at the last
        newline so the next read starts at the beginning of a real line.
        """
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return {"lines": [], "offset": 0}
        offset = max(0, min(int(offset), len(data)))
        chunk = data[offset:]
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            chunk = chunk[:cut + 1]
        lines = [line.decode("utf-8", errors="replace").rstrip()
                 for line in chunk.splitlines() if line.strip()]
        return {"lines": lines, "offset": offset + len(chunk), "bytes": len(data)}


JOB = StrataHook()
