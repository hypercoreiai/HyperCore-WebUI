"""serve/strata_hook.py - run Strata's own setup.py from the Config view.

The Config view has text boxes for the model paths; it cannot fetch a model. Upstream
Strata ships a setup.py that checks the machine, downloads the packs and prepares them
(PLAN section 10, decision 1). This is the hook to it: the request starts that script as
a detached subprocess and returns at once, so a ten-minute download never blocks the
HTTP server. stdout and stderr go to logs/strata-hook.log; the page polls
GET /config/strata-hook and reads the tail from a byte offset, so it appends lines
instead of re-fetching the whole log.

tmp/ holds the clone a build uses. A build no longer deletes it (loop 41: the clone's
data/ is what a run config points at), so on many machines that clone is the only
Strata with a setup.py on the box and the hook runs it there. The download is the risk:
setup.py's default data folder is "Strata-data next to this folder", which for a clone
in tmp/ is tmp/Strata-data, and the next build removes the clone with it. A setup
started from a clone therefore gets its --data-dir pinned to a folder outside tmp/, so
the packs a machine paid for survive the next build.
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

from serve import quant_store

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
#: Where a setup started from a build clone puts its model files. setup.py's own default
#: is "Strata-data next to this folder"; for a clone that is tmp/Strata-data, which the
#: next build deletes. The folder beside this project is the one this machine already
#: uses (the run configs' --pack and --native paths live in it). HYPERCORE_STRATA_DATA
#: or STRATA_DATA_DIR overrides it.
DATA_ENV = ("HYPERCORE_STRATA_DATA", "STRATA_DATA_DIR")

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
#: `project(strata VERSION 0.1.45.3)` in a clone's CMakeLists.txt. A folder in tmp/ is
#: named for the tag it was cloned at, which is not always the version that tree declares,
#: so the file is read first and the folder name is the fallback for the store entry.
PROJECT_VERSION_RE = re.compile(r"project\(\s*\w+\s+VERSION\s+([0-9.]+)", re.IGNORECASE)


#: Every ``--flag`` a setup.py declares, read from its own source. The Build tab's boxes
#: come from the release selected in the dropdown, but the hook runs the setup.py of the
#: checkout this machine has - not always that release. A flag the script never declared
#: is an argparse error before setup.py does any work at all, so the hook drops it and
#: says so instead of failing the run.
FLAG_RE = re.compile(r"add_argument\(\s*(?:\"|')(--\w[\w-]*)")
_FLAG_CACHE: dict[tuple[str, float], set[str]] = {}
FLAG_CACHE_LIMIT = 24


def accepted_flags(setup: Path) -> set[str] | None:
    """Every ``--flag`` the setup.py at path declares, or None when that is not knowable.

    None means unknown, not empty: a script with no ``add_argument`` at all (a stub, a
    wrapper, a release that builds its parser in a helper) is sent exactly what the
    caller asked for rather than guessed at. Cached per path and mtime, so a clone a
    build rewrites is re-read and a stable checkout is read once.
    """
    try:
        stat = setup.stat()
    except OSError:
        return None
    key = (str(setup), stat.st_mtime)
    cached = _FLAG_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        text = setup.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    flags = set(FLAG_RE.findall(text))
    if not flags:
        return None                       # nothing declared: the parser is not readable
    if len(_FLAG_CACHE) >= FLAG_CACHE_LIMIT:
        _FLAG_CACHE.clear()
    _FLAG_CACHE[key] = flags
    return flags


def strip_unaccepted(setup: Path, args: list[str] | None) -> tuple[list[str], list[str]]:
    """(the flags setup.py can parse, the flags it cannot).

    A dropped flag goes with its value: ``--images no`` is removed as a pair, so the
    stray ``no`` never reaches argparse as a positional. ``--data-dir=/path`` is one
    token and is dropped whole. Order of what is kept is unchanged.
    """
    accepted = accepted_flags(setup)
    given = [str(a) for a in (args or [])]
    if accepted is None:
        return given, []
    kept: list[str] = []
    dropped: list[str] = []
    i = 0
    while i < len(given):
        token = given[i]
        if token.startswith("--") and token.split("=", 1)[0] not in accepted:
            dropped.append(token)
            i += 1
            if i < len(given) and not given[i].startswith("--"):
                dropped.append(given[i])                # the value that belonged to it
                i += 1
            continue
        kept.append(token)
        i += 1
    return kept, dropped


def build_command(setup: Path, args: list[str] | None,
                  data_dir: Path | None = None) -> list[str]:
    """setup.py with the caller's flags, always non-interactive.

    A flag the caller already passed is not repeated, so a caller that sends its own
    --no-start gets exactly one. A flag this setup.py does not declare is dropped (see
    strip_unaccepted): the boxes above were read from a different release than the
    checkout the hook is about to run. data_dir pins where the model packs go; it is
    passed for a clone in tmp/ and is dropped when the caller named --data-dir or
    --models-dir, since that is the caller telling setup.py where its models live.
    Returns the list the hook will exec, which is also what the Build tab shows under
    the choice boxes.
    """
    given, _ = strip_unaccepted(setup, args)
    extra = [flag for flag in NON_INTERACTIVE if flag not in given]
    names_data_dir = any(flag in ("--data-dir", "--models-dir")
                         or flag.startswith(("--data-dir=", "--models-dir="))
                         for flag in given)
    if data_dir is not None and not names_data_dir:
        extra = ["--data-dir", str(data_dir)] + extra
    return [sys.executable, str(setup), *given, *extra]


def _under_scratch(path: Path) -> bool:
    """True when path is inside tmp/, the folder a build works in."""
    try:
        return path.resolve().is_relative_to(SCRATCH.resolve())
    except (ValueError, OSError):
        return False


def durable_data_dir() -> Path:
    """The folder a setup run from a build clone writes its packs to.

    Never inside tmp/: the point of pinning it is that the next build can delete the
    clone without taking a 70-120 GB download with it.
    """
    for name in DATA_ENV:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return ROOT.parent / "Strata-data"


def _scratch_checkout() -> Path | None:
    """The Strata clone a build left in tmp/ that has a setup.py, newest first.

    None when tmp/ has no clone. This is the last resort in the search: a checkout the
    user keeps is always better than one the next build removes.
    """
    if not SCRATCH.is_dir():
        return None
    try:
        entries = sorted((p for p in SCRATCH.iterdir() if p.is_dir()),
                         key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    for entry in entries:
        if _setup_in(entry) is not None:
            return entry
    return None


def _setup_in(path: Path) -> Path | None:
    """setup.py inside path, or path itself when the path is the script. None if absent."""
    setup = path if path.is_file() else path / "setup.py"
    return setup if setup.is_file() else None


def _version_of(checkout: str | Path) -> str:
    """The version a checkout declares, or the tag its folder was named for.

    Only used to label a store entry, so an unreadable file is not an error: the folder
    name is what the Build tab already showed the user.
    """
    folder = Path(checkout)
    try:
        match = PROJECT_VERSION_RE.search(folder.joinpath("CMakeLists.txt")
                                         .read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1)
    except OSError:
        pass
    return folder.name.removeprefix("strata-")


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
    cwd = _run_config_cwd(run_config)
    if cwd is not None:
        found.append(cwd)
    found.extend(ROOT / name for name in SEARCH_DIRS)
    # The folder beside the project, then the folders above it: a WebUI nested one or two
    # levels down (~/test_webUI/HyperCore-WebUI driving ~/Strata) still has the checkout
    # it drives two levels up. Bounded, so the search never walks to the filesystem root.
    for parent in list(ROOT.parents)[:3]:
        found.extend(parent / name for name in SIBLING_DIRS)
    return [p for p in found if p is not None]


def search_candidates(run_config: str | Path | None = None) -> list[Path]:
    """Every folder a Strata checkout could be in, in the order find_checkout tries them.

    Public for serve/build_api.py's release scan: the Build tab has to offer the same
    trees the hook would run setup.py from, otherwise the card names one release and a
    build uses another. It returns the search itself, so the scan and the hook cannot
    drift apart.
    """
    return _searched(run_config)


def _not_found_note() -> str:
    """Why the hook has nothing to run, naming the folders it looked in.

    A clone in tmp/ is a candidate now, so this note only appears when there is genuinely
    no setup.py anywhere the search reaches - naming the folders is what makes the next
    step obvious rather than a guess.
    """
    looked = ", ".join(str(p) for p in _searched(None) if p)
    return ("no Strata checkout with a setup.py was found. Set HYPERCORE_STRATA_SRC to "
            "the folder of a Strata you keep, put one next to this project (strata/), or "
            "send its path with the request. Looked in: " + (looked or "nothing"))


def find_checkout(explicit: str | None = None,
                  run_config: str | Path | None = None) -> tuple[Path | None, str]:
    """The setup.py of a Strata checkout this machine has, or a message saying why not.

    Returns (path, note). path is None when nothing was found, and note is the sentence
    the page shows instead of a stack trace. A checkout the user keeps beats a clone in
    tmp/, which the next build removes - so the clone is tried last, and a setup started
    from it gets a --data-dir outside tmp/ (see build_command).
    """
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
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

    clone = _scratch_checkout()
    if clone is not None:
        return clone / "setup.py", ""

    return None, _not_found_note()


def _build_busy() -> bool:
    """True when the Build tab has a build in progress in the clone.

    Imported lazily on purpose: serve/build_api.py owns that state, and a hook that
    cannot read it (the build module is not importable, or has no job yet) is treated as
    idle rather than refusing a setup the user asked for.
    """
    try:
        from serve import build_api
        state = getattr(build_api.JOB, "state", "idle")
    except Exception:  # noqa: BLE001 - a build state we cannot read is not a reason to fail
        return False
    return state not in ("idle", "done", "failed", "cancelled")


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
        #: Flags the caller asked for that this setup.py does not declare, and so were
        #: not sent. The status card names them; nothing is silently discarded.
        self.dropped: list[str] = []
        #: True when that folder is the clone a build keeps in tmp/, and the folder its
        #: packs go to - outside tmp/, so the next build cannot delete a download.
        self.scratch = False
        self.data_dir = ""
        #: What the Build tab's status card shows: the step setup.py has reached, the
        #: percent of the download in progress, and everything it has complained about.
        self.step_no = 0
        self.stage = ""
        self.percent: int | None = None
        self.download = ""
        self.problems: list[dict[str, str]] = []
        self.scanned = 0               # how far _scan has read into the log
        #: What a run in the clone left behind, moved into the project: the report from
        #: quant_store.harvest_setup_run. Empty for a run in a checkout the user keeps,
        #: which is theirs, and for a run that did not finish.
        self.harvest: dict[str, Any] = {}

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
            checkout = setup.parent
            scratch = _under_scratch(checkout)
            if scratch and _build_busy():
                note = (f"{checkout} is the clone a build is using right now. Wait for the "
                        "build to finish, or point the hook at a checkout you keep.")
                self.state, self.note, self.checkout = "idle", note, ""
                return {"error": note, "state": "idle"}
            data_dir = durable_data_dir() if scratch else None
            if data_dir is not None and _under_scratch(data_dir):
                data_dir = None      # the override would put the packs in tmp/ after all
            # The boxes above were read from the release in the dropdown; the script about
            # to run is the checkout this machine has. Anything it never declared is
            # dropped and reported, rather than ending the run in an argparse error.
            args, dropped = strip_unaccepted(setup, args)
            LOGS.mkdir(parents=True, exist_ok=True)
            command = build_command(setup, args, data_dir)
            try:
                log = open(self.log_path, "wb")  # noqa: SIM115 - the file is closed by _finish
            except OSError as exc:
                self.state, self.note = "failed", f"the log file could not be opened: {exc}"
                return {"error": self.note, "state": "failed"}
            if dropped:
                log.write((f"[!] setup.py in {checkout} does not accept "
                           f"{' '.join(dropped)}: dropped. The choices were read from a "
                           "different release than this checkout.\n").encode())
                log.flush()
            # stdin is closed on purpose: a prompt that cannot be answered fails fast
            # (setup.py says "run setup in a terminal, or pass --yes") instead of
            # waiting on the terminal the server was started from.
            proc = subprocess.Popen(command, cwd=str(setup.parent), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log)
            self.proc, self.command, self.started = proc, command, time.time()
            self.state, self.exit_code, self.note, self.checkout = "running", None, "", str(setup.parent)
            self.dropped = dropped
            self.step_no, self.stage, self.percent, self.download, self.problems = 0, "", None, "", []
            self.scanned = 0
            self.harvest = {}
            self.scratch, self.data_dir = scratch, str(data_dir) if data_dir else ""
            threading.Thread(target=self._finish, args=(proc, log), daemon=True).start()
            #: The whole status, not just the pid: the Build tab prints the checkout it
            #: picked and where the packs are going, straight from this response.
            return self._snapshot()

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
        # A run in the clone wrote its files inside tmp/, where the next build can delete
        # them. Harvest after the scan so the lines it reports are not read as setup.py's
        # own output, and before the state is set so a poll that sees "done" can see where
        # the files went.
        harvested = self._harvest(code)
        with self.lock:
            self.exit_code = code
            self.harvest = harvested
            if self.state == "running":
                self.state = "done" if code == 0 else "failed"
            if self.state == "failed" and not self.problems:
                # setup.py exited non-zero without printing an [X] line: the tail of the
                # log is the error, and the status card needs something to show.
                self.problems = [{"kind": "error", "text": line}
                                for line in self._last_lines(LAST_LINES)]

    def _append(self, line: str) -> None:
        """Add one line to the log after the script itself has finished.

        The page reads the log from a byte offset, so an appended line shows up in the next
        poll. A log that cannot be written is not worth failing a finished run over.
        """
        try:
            with open(self.log_path, "ab") as handle:
                handle.write((line + "\n").encode())
        except OSError:
            pass

    def _harvest(self, code: int) -> dict[str, Any]:
        """Move what the run wrote inside the clone into the project's store.

        Only a run in tmp/ is harvested: a checkout the user keeps is theirs, and setup.py
        writing into it is what they asked for. A run that did not finish is not harvested,
        since a half-built binary is not an engine. The quant is the one the run was sent
        with, read from the command actually executed: a flag this release does not declare
        was dropped before the run started, so the command is the truth about what ran.
        When the command names a model rather than a size, or names nothing because the box
        was left unset, the quant is read from the run config setup.py wrote inside the
        clone - see quant_store.quant_from_checkout.
        """
        if not self.scratch or code != 0:
            return {}
        sent = quant_store.choice_map(self.command)
        quant = sent.get("--model") or sent.get("--quant")
        try:
            report = quant_store.harvest_setup_run(
                Path(self.checkout), quant, sent,
                {"source": "setup-hook", "version": _version_of(self.checkout),
                 "label": quant, "options": {}})
        except Exception as exc:  # noqa: BLE001 - the run succeeded; the store step is reported, not raised
            note = f"the run finished but its files could not be stored: {exc}"
            self._append(note)
            return {"quant": None, "stored": [], "config": None, "changes": [],
                    "data": 0, "note": note}
        key = report.get("quant")
        if not key:
            self._append(report.get("note") or "nothing was stored")
            return report
        folder = f"engine/engine_{key}"
        source = report.get("quant_source") or ""
        if source and source != "the card":
            # The card did not name this quant, so say where it came from: without the
            # line a store folder that appeared from a run config looks unexplained.
            self._append(f"quant {key} read from {source}")
        if report.get("stored"):
            self._append(f"stored {folder}: {', '.join(report['stored'])}")
        if report.get("data"):
            self._append(f"{report['data']} runtime file(s) stored in {folder}/data")
        if report.get("config"):
            name = Path(report["config"]).name
            self._append(f"run config: config/{name}")
            if report.get("changes"):
                self._append(f"setup choices written to config/{name}: "
                             f"{', '.join(report['changes'])}")
        else:
            self._append("no run config was written for this quant")
        if report.get("note"):
            self._append(report["note"])
        if report.get("active"):
            self._append(f"active engine: {report['active_note']}")
        return report

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
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        """The status fields, read without taking the lock.

        start() returns this while it still holds the lock, so it cannot call status().
        """
        return {"state": self.state, "command": self.command,
                "checkout": self.checkout, "dropped": list(self.dropped),
                "scratch": self.scratch, "data_dir": self.data_dir,
                "pid": self.proc.pid if self.proc else None,
                "exit_code": self.exit_code,
                "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
                "log": str(self.log_path), "note": self.note,
                "harvest": dict(self.harvest),
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
