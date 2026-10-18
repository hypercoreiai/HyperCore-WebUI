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

import json
import os
import re
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

#: Where a Strata checkout is looked for, in order: the path the request names, the
#: environment, the directory the run config starts the engine from (that is the
#: checkout this machine actually runs), a checkout inside this project, and a checkout
#: beside it - the layout of a PC that has the WebUI folder next to the Strata folder
#: it drives.
SEARCH_ENV = ("HYPERCORE_STRATA_SRC", "STRATA_SRC")
SEARCH_DIRS = ("strata", "Strata", "strata-main")
SIBLING_DIRS = ("Strata", "strata", "strata-main")
#: A run config with no --config is picked from config/, so this is the fallback for
#: the cwd lookup when the request does not name one.
RUN_CONFIG_DIR = ROOT / "config"

#: A detached subprocess has no terminal to answer with. setup.py asks its questions
#: through input(), so started from the server it either hangs on whatever stdin the
#: server inherited or fails on EOF. --yes takes the recommended answer for every
#: question the caller did not name a flag for (setup.py reads a flag before it asks,
#: so an explicit choice always wins), and --no-start keeps a setup from starting an
#: engine on a card the WebUI or a tune is holding.
NON_INTERACTIVE = ("--yes", "--no-start")

#: setup.py's own progress markers, read from the log it writes. The Build tab shows
#: the stage the setup is on from these, so a release that renumbers or renames its
#: steps still reports correctly - there is no list of stages maintained here.
STEP_RE = re.compile(r"^\s*===\s*Step\s+(\d+)\s*:\s*(.+?)\s*===\s*$")
#: setup.py's own output helpers: [X] is where it stops, [!] is a warning it carried on
#: past. A Python traceback and the exception line that follows it are errors too.
FAIL_RE = re.compile(r"^\s*\[X\]\s*(.*)$")
WARN_RE = re.compile(r"^\s*\[!\]\s*(.*)$")
EXCEPT_RE = re.compile(r"^\s*([A-Za-z][\w.]*Error)(:.*)?$")
#: a download line as the progress writer emits it: "<file>: 12.34 / 39.23 GB (31%)"
DOWNLOAD_RE = re.compile(r"^\s*(.+?):\s*([\d.]+)\s*/\s*([\d.]+)\s*GB\s*\((\d+)%\)")
#: The status card is a summary: a wall of compiler output is on the log, not here.
MAX_PROBLEMS = 20
#: Lines kept when a run exits non-zero without printing an [X] line at all.
LAST_LINES = 10


def build_command(setup: Path, args: list[str] | None) -> list[str]:
    """setup.py with the caller's flags, always non-interactive.

    A flag the caller already passed is not repeated, so a caller that sends its own
    --no-start gets exactly one. Returns the list the hook will exec, which is also what
    the Build tab shows under the choice boxes.
    """
    given = [str(a) for a in (args or [])]
    extra = [flag for flag in NON_INTERACTIVE if flag not in given]
    return [sys.executable, str(setup), *given, *extra]


def _under_scratch(path: Path) -> bool:
    """True when path is inside tmp/, which is deleted when a build finishes."""
    try:
        return path.resolve().is_relative_to(SCRATCH.resolve())
    except (ValueError, OSError):
        return False


def _setup_in(path: Path) -> Path | None:
    """setup.py inside path, or path itself when the path is the script. None if absent."""
    setup = path if path.is_file() else path / "setup.py"
    return setup if setup.is_file() else None


def _run_config_cwd(run_config: str | Path | None) -> Path | None:
    """The cwd of a run config.

    The engine is started from its Strata folder, so that folder is the checkout this
    machine runs and the right place to run setup.py. A config that cannot be read is
    simply not a candidate: the search continues with the other places.
    """
    if not run_config:
        return None
    try:
        cfg = json.loads(Path(run_config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cwd = cfg.get("cwd") if isinstance(cfg, dict) else None
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    path = Path(cwd.strip())
    return path if path.is_absolute() else ROOT / path


def _searched(run_config: str | Path | None) -> list[Path]:
    """Every place a checkout could be, in the order they are tried."""
    found: list[Path] = []
    for name in SEARCH_ENV:
        value = os.environ.get(name)
        if value:
            p = Path(value)
            found.append(p if p.is_absolute() else ROOT / p)
    found.append(_run_config_cwd(run_config))
    found.extend(ROOT / name for name in SEARCH_DIRS)
    found.extend(ROOT.parent / name for name in SIBLING_DIRS)
    return [p for p in found if p is not None]


def _not_found_note() -> str:
    """Why the hook has nothing to run, naming the closest thing it did see."""
    note = ("no Strata checkout with a setup.py was found. Set HYPERCORE_STRATA_SRC to "
           "the folder of a Strata you keep, or put one next to this project (strata/).")
    clone = None
    if SCRATCH.is_dir():
        clone = next((p for p in SCRATCH.iterdir()
                      if p.is_dir() and (p / "setup.py").is_file()), None)
    if clone is not None:
        note += (f" The only setup.py on this machine is {clone}, the clone a build uses: "
                "tmp/ is deleted when a build finishes, so the hook will not run there.")
    return note


def find_checkout(explicit: str | None = None,
                  run_config: str | Path | None = None) -> tuple[Path | None, str]:
    """The setup.py of a Strata checkout this machine has, or a message saying why not.

    Returns (path, note). path is None when nothing was found, and note is the sentence
    the page shows instead of a stack trace. tmp/ is never a candidate: it is deleted
    when a build finishes, and a model download that lives there goes with it.
    """
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        if _under_scratch(p):
            return None, (f"{p} is inside tmp/, which is deleted when a build finishes. "
                         "Point the hook at a checkout you keep.")
        setup = _setup_in(p)
        if setup is not None:
            return setup, ""
        return None, f"no setup.py at {p}"

    for candidate in _searched(run_config):
        if _under_scratch(candidate):
            continue
        setup = _setup_in(candidate)
        if setup is not None:
            return setup, ""

    return None, _not_found_note()


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
        self.checkout = ""          # the Strata folder the last run used, for the page
        #: What the Build tab's status card shows: the step setup.py has reached, the
        #: percent of the download in progress, and everything it has complained about.
        self.step_no = 0
        self.stage = ""
        self.percent: int | None = None
        self.download = ""
        self.problems: list[dict[str, str]] = []
        self.scanned = 0               # how far _scan has read into the log

    def start(self, path: str | None = None, args: list[str] | None = None,
              run_config: str | Path | None = None) -> dict[str, Any]:
        """Spawn setup.py and return at once. Nothing here waits for the download.

        run_config is the run config the server is using: its cwd is the checkout this
        machine runs, and is the fallback when the request names no path.
        """
        with self.lock:
            if self.state == "running":
                return {"error": "the setup hook is already running", "state": "running"}
            setup, note = find_checkout(path, run_config)
            if setup is None:
                self.state, self.note, self.checkout = "idle", note, ""
                return {"error": note, "state": "idle"}
            LOGS.mkdir(parents=True, exist_ok=True)
            command = build_command(setup, args)
            try:
                log = open(self.log_path, "wb")  # noqa: SIM115 - the file is closed by _finish
            except OSError as exc:
                self.state, self.note = "failed", f"the log file could not be opened: {exc}"
                return {"error": self.note, "state": "failed"}
            # stdin is closed on purpose: a prompt that cannot be answered fails fast
            # (setup.py says "run setup in a terminal, or pass --yes") instead of
            # waiting on the terminal the server was started from.
            proc = subprocess.Popen(command, cwd=str(setup.parent), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log)
            self.proc, self.command, self.started = proc, command, time.time()
            self.state, self.exit_code, self.note, self.checkout = "running", None, "", str(setup.parent)
            self.step_no, self.stage, self.percent, self.download, self.problems = 0, "", None, "", []
            self.scanned = 0
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
        # Read the log before deciding what to report: the stage and the problems come
        # from the lines themselves, and only a run that printed none of setup.py's own
        # markers falls back to the tail.
        self._scan()
        with self.lock:
            self.exit_code = code
            if self.state == "running":
                self.state = "done" if code == 0 else "failed"
            if self.state == "failed" and not self.problems:
                # setup.py exited non-zero without printing an [X] line: the tail of the
                # log is the error, and the status card needs something to show.
                self.problems = [{"kind": "error", "text": line}
                                for line in self._last_lines(LAST_LINES)]

    def _last_lines(self, count: int) -> list[str]:
        """The end of the log, skipping the download progress lines."""
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return []
        lines = [line.decode("utf-8", errors="replace").strip() for line in data.splitlines()]
        lines = [line for line in lines if line and not DOWNLOAD_RE.search(line)]
        return lines[-count:]

    def _scan(self) -> None:
        """Fold everything written to the log since the last scan into the stage.

        The page polls the tail with its own byte offset; this is the server-side view
        of the same log, so a status request reports the stage and the problems even
        when no client has read the log yet. Lines are only ever folded once.
        """
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return
        chunk = data[self.scanned:]
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return                      # a mid-write chunk: wait for a whole line
        chunk = chunk[:cut + 1]
        self.scanned += len(chunk)
        self._note([line.decode("utf-8", errors="replace").rstrip()
                    for line in chunk.splitlines() if line.strip()])

    def _note(self, lines: list[str]) -> None:
        """Fold a chunk of the log into the stage, the download percent and the problems."""
        with self.lock:
            for line in lines:
                step = STEP_RE.match(line)
                if step:
                    n, title = int(step.group(1)), step.group(2).strip()
                    if n >= self.step_no:
                        self.step_no, self.stage = n, f"Step {n}: {title}"
                    continue
                download = DOWNLOAD_RE.search(line)
                if download:
                    self.percent, self.download = int(download.group(4)), download.group(1).strip()
                    continue
                kind, text = "", ""
                fail, warn = FAIL_RE.match(line), WARN_RE.match(line)
                if fail:
                    kind, text = "error", fail.group(1)
                elif warn:
                    kind, text = "warning", warn.group(1)
                elif line.startswith("Traceback (most recent call last):"):
                    kind, text = "error", "a Python traceback was printed"
                else:
                    exc = EXCEPT_RE.match(line)
                    if exc:
                        kind, text = "error", f"{exc.group(1)}{exc.group(2) or ''}"
                if not text or text in {p["text"] for p in self.problems}:
                    continue
                self.problems.append({"kind": kind, "text": text})
                if len(self.problems) > MAX_PROBLEMS:
                    self.problems = self.problems[-MAX_PROBLEMS:]

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            if self.proc and self.state == "running":
                self.proc.terminate()
                self.state = "cancelled"
                return {"cancelled": True}
            return {"cancelled": False}

    def status(self) -> dict[str, Any]:
        self._scan()
        with self.lock:
            return {"state": self.state, "command": self.command,
                    "checkout": self.checkout,
                    "pid": self.proc.pid if self.proc else None,
                    "exit_code": self.exit_code,
                    "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
                    "log": str(self.log_path), "note": self.note,
                    "stage": self.stage, "percent": self.percent,
                    "download": self.download, "problems": list(self.problems)}

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
        self._scan()
        return {"lines": lines, "offset": offset + len(chunk), "bytes": len(data)}


JOB = StrataHook()
