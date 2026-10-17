"""serve/terminal_mcp.py - a shell, a pipeline, background jobs, a REPL, and the code tools.

HyperCore starts this from mcp.yaml as the `terminal` server; the Monitor lists it as terminal__run,
terminal__pipe, terminal__process, terminal__repl, terminal__symbols, terminal__check, terminal__install
and terminal__status.  Standard library only, like the other servers here.

Tier 1 is the four primitives: run executes one shell command, pipe runs a pipeline stage by stage so every
stage's exit code is reported, process starts and supervises background jobs, and repl keeps a Python or bash
session open between calls.

Tier 2 is the object-oriented layer: symbols reads a file or package with ast and reports the classes, their
bases and their methods; check runs whatever of ruff, mypy, pylint and pytest is installed and says plainly
which are not; install adds the missing ones; status reports which tools exist and which interpreters are
available.

Tier 3 is the compiled and native layer: build configures and builds a CMake project, compile checks one C or
C++ file (using the flags of the real build when a compile_commands.json is given), tidy runs clang-tidy, and
debug runs gdb, valgrind, nm or objdump, or traces a command with strace, ltrace or time.

Tier 4 is data and orchestration: query reads JSON with jq, YAML with yq, TOML with tomllib and a SQLite
database read-only with sqlite3; compare diffs two files, or a file against its committed version in git.
Every one of these returns the exit code of the tool it ran, and states how much of the output it left out.

Guardrails, all settable from mcp.yaml:
  TERMINAL_ROOT          the directory commands may work in (default /home)
  TERMINAL_STATE_DIR     where job logs and the REPL driver live (default ~/.terminal)
  TERMINAL_TIMEOUT_S     default per-call timeout (default 30, hard maximum 300)
  TERMINAL_MAX_OUT       characters of stdout or stderr returned (default 20000; the rest is counted, not lost)
  TERMINAL_ALLOW_EXTRA   comma-separated binaries added to the allowlist
  TERMINAL_ALLOW_ALL=1   drop the allowlist and the cd/redirect root check. check_command keeps
                         both for when this is off (the later /home/ai-agent/ pass).
  TERMINAL_RESULT_CAP    serialized tool result must fit in this many characters (default 6000,
                         the hub cap). stdout shrinks before stderr.

A command is run by /bin/sh, so pipes, redirection and globs work.  The program at each command position must
be on the allowlist; shell keywords (if, for, cd, export) are not programs and are permitted.  cwd, save_to
and any file named after a redirect must resolve inside TERMINAL_ROOT, with one exception: the /dev
device files (/dev/null and friends) are always allowed.  This is a guard against typos and stray binaries,
not a shell parser: a program hidden inside an argument (xargs -I{} lmnope) is not seen.  Both forms of
command substitution, `$(...)` and backticks, are checked as commands in their own right.
Output over TERMINAL_MAX_OUT is truncated with the missing character count stated; pass save_to to write the
whole output to a file under the root, which the document tools can then read.

    python serve/mcp/terminal_mcp.py          stdio, JSON-RPC one message per line
"""
from __future__ import annotations

import ast
import atexit
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repo root, so serve.doc_text resolves
from serve.doc_text import coerce_sequence

ROOT = Path(os.environ.get("TERMINAL_ROOT") or "/home").expanduser().resolve()
STATE_DIR = Path(os.environ.get("TERMINAL_STATE_DIR") or (Path.home() / ".terminal")).expanduser().resolve()
RUN_TIMEOUT = float(os.environ.get("TERMINAL_TIMEOUT_S") or 30.0)
MAX_TIMEOUT = float(os.environ.get("TERMINAL_MAX_TIMEOUT_S") or 300.0)
MAX_OUT = int(os.environ.get("TERMINAL_MAX_OUT") or 20000)
# The hub reads max_result_chars of the JSON, not of stdout. A 6000-character stdout
# becomes more than 6000 once it is wrapped, and the cut then lands inside the string
# and drops stderr. This cap is the whole JSON document.
RESULT_CAP = int(os.environ.get("TERMINAL_RESULT_CAP") or 6000)
MAX_PIPE_BYTES = int(os.environ.get("TERMINAL_MAX_PIPE_BYTES") or 2_000_000)
ALLOW_ALL = os.environ.get("TERMINAL_ALLOW_ALL", "").strip().lower() in {"1", "true", "yes", "on"}

_BASE = {
    "sh", "bash", "dash", "env", "echo", "printf", "cat", "head", "tail", "nl", "tac", "tr", "wc", "sort",
    "uniq", "grep", "egrep", "fgrep", "rg", "find", "fd", "sed", "awk", "cut", "paste", "comm", "diff", "cmp",
    "patch", "xargs", "tee", "seq", "yes", "base64", "od", "hexdump", "strings", "ls", "tree", "stat", "du",
    "df", "file", "basename", "dirname", "realpath", "mkdir", "touch", "cp", "mv", "rm", "ln", "chmod", "chown",
    "tar", "gzip", "gunzip", "bzip2", "unzip", "zip", "md5sum", "sha256sum", "sleep", "date",
    "pwd", "ps", "kill", "killall", "top", "uptime", "uname", "id", "whoami", "bc", "expr",
    "ninja", "ar", "strip", "nm", "objdump", "xxd", "clang-tidy", "lldb",
    "strace", "ltrace", "time", "python", "python3", "pip", "pip3", "pyenv", "uv", "poetry", "virtualenv",
    "git", "make", "cmake", "gcc", "g++", "cc", "clang", "clang++", "gdb", "valgrind",
    "ruff", "black", "mypy", "pyright", "pylint", "flake8", "pytest", "tox", "nox", "coverage", "vulture",
    "jq", "yq", "sqlite3", "docker", "podman", "curl", "wget", "nvidia-smi", "ffprobe", "ffmpeg",
}
# shell builtins and control words: they are not programs, so the allowlist does not apply to them
_KEYWORDS = {
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "select", "in",
    "function", "time", "return", "break", "continue", "exit", ":", "cd", "read", "export", "unset", "local",
    "shift", "trap", "eval", "alias", "source", "set", "readonly", "jobs", "bg", "fg", "wait", "times", "hash",
    "getopts", "history", "true", "false", "test", "[", "[["
}
ALLOWED = _BASE | {x.strip() for x in os.environ.get("TERMINAL_ALLOW_EXTRA", "").split(",") if x.strip()}

# Tier 2: the tools an agent needs to build and maintain object-oriented code.
TIER2 = ["git", "ruff", "mypy", "pyright", "pylint", "black", "vulture", "gdb", "valgrind",
         "cmake", "make", "gcc", "g++", "jq", "sqlite3", "ffprobe", "docker", "rg", "pytest"]
# Tier 3 is the compiled and native layer: build, compile, static analysis, debugging, memory checks.
TIER3 = ["cmake", "make", "ninja", "g++", "gcc", "clang", "clang++", "clang-tidy", "gdb", "lldb",
         "valgrind", "nm", "objdump", "ffprobe", "ffmpeg"]
# Tier 4 is data and orchestration: structured queries, diffs, containers, fetching, tracing.
TIER4 = ["jq", "yq", "sqlite3", "rg", "diff", "patch", "docker", "curl", "wget", "strace", "ltrace", "time"]

# "g++" and "clang++" are programs, so the name pattern has to allow a plus. A leading
# "-" is still an argument, never a command.
_NAME = re.compile(r"[./_A-Za-z][A-Za-z0-9_./+-]*")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUB = re.compile(r"\$\(([^()]*)\)")
_SUB_BACKTICK = re.compile(r"`([^`]*)`")
_ARITH = re.compile(r"\$\(\([^()]*\)\)")          # $((a + b)) is arithmetic, not a command
_PAREN = re.compile(r"[()]")
_REDIRECT_TARGET = re.compile(r"(?:^|\s)\d*(?:>>|>|<)\s*([^\s;|&)]+)")
_HEREDOC = re.compile(r"<<-?\s*(?:(['\"])([A-Za-z_][A-Za-z0-9_]*)\1|([A-Za-z_][A-Za-z0-9_]*))")
# builtins whose following tokens are arguments, not programs
_BUILTIN = {"cd", "echo", "printf", "read", "test", "[", "[[", ":", "return", "exit", "export", "unset",
            "local", "shift", "trap", "eval", "alias", "source", "set", "true", "false", "wait", "jobs",
            "bg", "fg", "times", "hash", "getopts", "history", "type", "command", "fc", "ulimit", "pwd",
            "break", "continue", "declare", "typeset", "readonly", "help", "kill"}
# a header names variables, so the segment has no command to check
_HEADER = {"for", "while", "until", "if", "case", "select"}
# headers that name variables rather than run a command: nothing in them is a program
_WORD_LIST_HEADER = {"for", "case", "select"}
# control words that are followed by a command: keep scanning past these
_FOLLOW = {"do", "then", "else", "elif", "begin", "done", "fi", "esac", "in"}
# control words (if, while, do, then, ...): never programs, and usually followed by one
_CONTROL = _FOLLOW | _HEADER
_CD_TARGET = re.compile(r"(?:^|\s)cd\s+([^\s;|&)]+)")
_TOKEN = re.compile(r"'[^']*'|\"[^\"]*|[^\s|;&]+")
_REDIRECT_STRIP = re.compile(r"\d*(?:>>|>|<)\s*[^\s;|&)]+")
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00]')
_VERSION_FLAG = {"git": "--version", "rg": "--version", "jq": "--version", "docker": "--version",
                 "cmake": "--version", "make": "--version", "gcc": "--version", "g++": "--version",
                 "python3": "--version", "python": "--version", "pyenv": "--version",
                 "clang": "--version", "clang++": "--version", "clang-tidy": "--version",
                 "gdb": "--version", "lldb": "--version", "valgrind": "--version", "yq": "--version",
                 "sqlite3": "-version", "strace": "--version", "ltrace": "--version",
                 "ffprobe": "-version", "ffmpeg": "-version", "ninja": "--version"}

_version_cache: dict[str, str] = {}
_jobs: dict[str, dict] = {}
_jobs_mu = threading.Lock()
_sessions: dict[str, dict] = {}
_sessions_mu = threading.Lock()

REPL_DRIVER = r"""
import ast, os, sys, traceback
MARK = os.environ.get("REPL_MARKER", "__REPL_END__")
ns = {"__name__": "__repl__"}
while True:
    lines = []
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        line = line.rstrip("\n")
        if line == MARK:
            break
        lines.append(line)
    src = "\n".join(lines)
    if src:
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            print("SyntaxError: " + str(exc))
        else:
            try:
                if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr):
                    # a bare expression: echo its value, as an interactive session would
                    value = eval(compile(ast.Expression(tree.body[0].value), "<repl>", "eval"), ns)
                    if value is not None:
                        print(repr(value))
                else:
                    exec(compile(tree, "<repl>", "exec"), ns)
            except SystemExit:
                raise
            except Exception:
                traceback.print_exc()
    print(MARK)
    sys.stdout.flush()
"""


# the only paths outside the root the guard lets through: > /dev/null is a shell idiom, not a stray path
DEV_OK = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"})

def inside(path: str | Path) -> Path:
    """Resolve path under TERMINAL_ROOT; anything that lands outside it is refused.

    The /dev device files are the one exception.  Redirecting to /dev/null is the commonest shell idiom
    there is, and a guard that refuses it reads as arbitrary rather than protective.  They are character
    devices, not project paths, so the guard's purpose - keeping stray paths out of the work - is not
    weakened.  Only the named device entries are let through: /dev/random and a /dev/x write are still
    refused.  /dev/stdin, /dev/stdout and /dev/stderr are checked before resolving, because on Linux they
    are symlinks into /proc/self/fd and resolve() would turn them into something the list does not name.
    """
    raw = str(path or "").strip()
    if not raw:
        return ROOT
    if "\x00" in raw:
        raise ValueError("path contains a null byte")
    candidate = Path(raw)
    target = candidate if candidate.is_absolute() else ROOT / candidate
    if str(target) in DEV_OK:
        return target
    resolved = target.resolve()
    if str(resolved) in DEV_OK:
        return resolved
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f"{path} is outside the working root {ROOT}")
    return resolved


def safe_name(name: str) -> str:
    normalized = str(name or "").replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("a name may not contain '..'")
    cleaned = _ILLEGAL.sub("_", normalized).strip().strip(".")
    if not cleaned:
        raise ValueError("the name is empty after cleaning")
    return cleaned


def _strip_heredocs(text: str) -> str:
    """Remove heredoc bodies: they are data, not commands to check."""
    out = text
    while True:
        match = _HEREDOC.search(out)
        if match is None:
            return out
        word = match.group(2) or match.group(3)
        rest = out[match.end():]
        stop = re.search(r"^" + re.escape(word) + r"$", rest, re.MULTILINE)
        if stop is None:
            return out
        out = out[:match.start()] + rest[stop.end():]


def _segments(text: str) -> list[str]:
    """Split on | ; && || and newlines, but only outside quotes."""
    parts: list[str] = []
    buffer: list[str] = []
    quote = None
    index = 0
    while index < len(text):
        if text[index:index + 2] in ("&&", "||") and quote is None:
            parts.append("".join(buffer))
            buffer = []
            index += 2
            continue
        char = text[index]
        if quote:
            if char == quote:
                quote = None
            buffer.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char in "|;\n":
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return parts


def _first_command(segment: str) -> str | None:
    """The program in command position of a segment, or None if there is none.

    Quoted runs stay one token, so a printf with newlines inside quotes is not read as three commands.  Control
    words are skipped: an `if` or `while` condition is a command, so scanning continues past those, while a
    `for`, `case` or `select` header names variables and has no command to check.  A builtin (echo, cd, test)
    is followed by arguments, not by commands, so the segment ends there.  An absolute token that is not a file
    is data - a directory, a glob - and is skipped.

    This is a guard against typos and stray binaries, not a shell parser: a program hidden inside an
    argument (xargs -I{} lmnope, or python3 -c 'import lmnope') is not seen.
    """
    tokens = _TOKEN.findall(segment)
    if not tokens or tokens[0] in _WORD_LIST_HEADER:
        return None
    for token in tokens:
        if token in _WORD_LIST_HEADER:
            return None                       # a for/case header names variables, not programs
        if _ASSIGN.match(token) or token in _CONTROL:
            continue
        if _NAME.fullmatch(token) is None:
            continue
        if token in _BUILTIN:
            return None                       # everything after a builtin is an argument
        if token.startswith("/") and not Path(token).is_file():
            continue
        return token
    return None
def check_command(command: str) -> None:
    """The program at each command position must be allowed. Unbalanced quotes fail before a shell sees them."""
    text = str(command or "")
    if not text.strip():
        raise ValueError("the command is empty")
    try:
        shlex.split(text)
    except ValueError as e:
        raise ValueError(f"unbalanced quotes in the command: {e}") from None
    # This phase runs what the account can run. The checks below stay for the pass
    # that turns TERMINAL_ALLOW_ALL off.
    if ALLOW_ALL:
        return
    scanned = _strip_heredocs(text)
    for match in _REDIRECT_TARGET.finditer(scanned):
        inside(match.group(1))                       # a redirect may not write outside the root
    for match in _CD_TARGET.finditer(scanned):
        inside(match.group(1))                       # cd may not leave the root
    # a parenthesised subshell is a command position too, so the parentheses are opened up;
    # arithmetic expansion is removed first so $((total + n)) does not leave a bare word behind
    opened = _PAREN.sub(" ", _ARITH.sub(" ", _REDIRECT_STRIP.sub(" ", scanned)))
    for segment in _segments(opened):
        binary = _first_command(segment)
        if binary is None or binary in ALLOWED:
            continue
        # an absolute path that exists is a program: it must be under the root
        if binary.startswith("/") and inside(binary) == Path(binary).resolve():
            continue
        raise ValueError(f"{binary!r} is not on the allowlist. "
                        "Set TERMINAL_ALLOW_EXTRA or TERMINAL_ALLOW_ALL=1 to permit it.")
    # both forms of command substitution run a command, so they are checked the same way
    for inner in _SUB.findall(scanned) + _SUB_BACKTICK.findall(scanned):
        if inner.strip():
            check_command(inner)


def merged_env(env: dict | None) -> dict:
    merged = dict(os.environ)
    merged.update({str(k): str(v) for k, v in (env or {}).items()})
    return merged


def _finish(proc, timeout: float, stdin: bytes | None) -> tuple[bytes, bytes, bool]:
    """(stdout, stderr, timed_out). A timeout keeps the bytes the process already wrote."""
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
        return out or b"", err or b"", False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate()
        except (OSError, subprocess.TimeoutExpired):
            out, err = b"", b""
        return out or b"", err or b"", True


def kill_tree(proc) -> None:
    """End a process and everything it started, via its own process group."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def clip(text: str, limit: int | None = None) -> tuple[str, int]:
    """Return (text, remaining). Truncation is always stated, never silent. The limit is read when called."""
    cap = MAX_OUT if limit is None else int(limit)
    if len(text) <= cap:
        return text, 0
    return text[:cap], len(text) - cap


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(command: str, cwd: str = "", env: dict | None = None, timeout_s: float = RUN_TIMEOUT,
       stdin: str = "", save_to: str = "") -> dict:
    """Run one shell command. Returns the exit code and both streams."""
    workdir = inside(cwd)
    check_command(command)
    if not workdir.is_dir():
        raise ValueError(f"{workdir} is not a directory")
    limit = max(1.0, min(float(timeout_s or RUN_TIMEOUT), MAX_TIMEOUT))
    started = time.monotonic()
    proc = subprocess.Popen(["/bin/sh", "-c", str(command)], cwd=str(workdir), env=merged_env(env),
                            stdin=subprocess.PIPE if stdin else None, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    # communicate() owns the handle: writing then closing by hand leaves it flushed twice,
    # which Python 3.12 rejects and 3.14 tolerates. A timeout keeps what was already written.
    out, err, timed_out = _finish(proc, limit, stdin.encode("utf-8") if stdin else None)
    elapsed = time.monotonic() - started
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    out_text = out.decode("utf-8", "replace") if isinstance(out, bytes) else ""
    err_text = err.decode("utf-8", "replace") if isinstance(err, bytes) else ""
    body_out, left_out = clip(out_text)
    body_err, left_err = clip(err_text)
    result = {"command": command, "cwd": str(workdir), "exit_code": proc.returncode,
             "elapsed_ms": int(elapsed * 1000), "timed_out": timed_out,
             "stdout": body_out, "stderr": body_err}
    if left_out or left_err:
        result["truncated"] = {"stdout_chars": left_out, "stderr_chars": left_err,
                              "note": "use save_to to keep the whole output, or pipe through head"}
    if save_to:
        dest = inside(save_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out_text + ("\n" + err_text if err_text else ""), encoding="utf-8")
        result["saved_to"] = str(dest)
        result["saved_bytes"] = dest.stat().st_size
    return result


def pipe(stages: list[str], cwd: str = "", env: dict | None = None, timeout_s: float = RUN_TIMEOUT) -> dict:
    """A pipeline run stage by stage, so every stage's exit code and stderr are reported, not just the last."""
    wanted = [str(s) for s in (stages or []) if str(s).strip()]
    if not wanted:
        raise ValueError("a pipeline needs at least one stage")
    argvs = []
    for stage in wanted:
        argv = shlex.split(stage)
        if not argv:
            raise ValueError(f"stage {stage!r} has no command")
        check_command(stage)
        argvs.append(argv)
    workdir = inside(cwd)
    if not workdir.is_dir():
        raise ValueError(f"{workdir} is not a directory")
    limit = max(1.0, min(float(timeout_s or RUN_TIMEOUT), MAX_TIMEOUT))
    started = time.monotonic()
    data = b""
    report = []
    failed = None
    for argv in argvs:
        proc = subprocess.Popen(argv, cwd=str(workdir), env=merged_env(env),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        try:
            out, err, timed_out = _finish(proc, limit, data)
        except BrokenPipeError:
            out, err, timed_out = b"", b"", False
        if timed_out:
            err = (err + b"\n" if err else b"") + b"timed out"
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
        entry = {"stage": " ".join(argv), "exit_code": proc.returncode,
                 "out_bytes": len(out) if isinstance(out, bytes) else 0,
                 "stderr": clip(err.decode("utf-8", "replace"), 1000)[0]}
        if timed_out:
            entry["timed_out"] = True
        if isinstance(out, bytes) and len(out) > MAX_PIPE_BYTES:
            entry["note"] = f"output cut at {MAX_PIPE_BYTES} bytes before the next stage"
        report.append(entry)
        if proc.returncode != 0:
            failed = entry
            data = b""
            break
        data = (out or b"")[:MAX_PIPE_BYTES]
    return {"cwd": str(workdir), "stages": report, "exit_code": failed["exit_code"] if failed else 0,
           "failed_stage": failed["stage"] if failed else None,
           "output": clip(data.decode("utf-8", "replace"))[0],
           "elapsed_ms": int((time.monotonic() - started) * 1000)}


def process(action: str = "list", name: str = "", command: str = "", cwd: str = "", env: dict | None = None,
           sig: str = "TERM", timeout_s: float = 10.0, lines: int = 40) -> dict:
    """Background jobs: start, status, log, signal, kill, wait, list. Output goes to a log under STATE_DIR."""
    action = str(action or "list").lower()
    if action == "list":
        with _jobs_mu:
            rows = []
            for key, job in _jobs.items():
                code = job["proc"].poll()
                rows.append({"name": key, "command": job["command"], "pid": job["pid"],
                            "running": code is None, "exit_code": code,
                            "started": job["started"], "log": job["log"]})
            return {"jobs": rows}
    if not name:
        raise ValueError("name is required")
    key = safe_name(name)
    if action == "start":
        if not command:
            raise ValueError("command is required")
        check_command(command)
        workdir = inside(cwd)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "jobs" / key
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # held open for the job lifetime; closed by close_all() or when the job is replaced
        handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(["/bin/sh", "-c", str(command)], cwd=str(workdir), env=merged_env(env),
                                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        with _jobs_mu:
            old = _jobs.get(key)
            if old is not None:
                kill_tree(old["proc"])
                try:
                    old["log_handle"].close()
                except OSError:
                    pass
            _jobs[key] = {"proc": proc, "pid": proc.pid, "command": command, "log": str(log_path),
                         "log_handle": handle, "started": _iso(time.time())}
        return {"started": key, "pid": proc.pid, "log": str(log_path), "cwd": str(workdir)}
    with _jobs_mu:
        entry = _jobs.get(key)
    if entry is None:
        raise ValueError(f"no job named {key}. Call process with action=list to see the running ones.")
    if action == "status":
        code = entry["proc"].poll()
        return {"name": key, "pid": entry["pid"], "running": code is None, "exit_code": code,
              "started": entry["started"], "log": entry["log"]}
    if action == "log":
        count = max(1, min(int(lines or 40), 500))
        body = Path(entry["log"]).read_text(encoding="utf-8", errors="replace") if Path(entry["log"]).exists() else ""
        kept, left = clip("\n".join(body.splitlines()[-count:]))
        return {"name": key, "log": entry["log"], "lines": kept, "omitted": left}
    if action in {"signal", "kill"}:
        code = entry["proc"].poll()
        if code is not None:
            return {"name": key, "already_stopped": True, "exit_code": code}
        number = {"TERM": signal.SIGTERM, "INT": signal.SIGINT, "HUP": signal.SIGHUP,
                  "KILL": signal.SIGKILL}.get(str(sig or "TERM").upper(), signal.SIGTERM)
        try:
            os.killpg(entry["pid"], number)
        except OSError as e:
            raise ValueError(f"could not signal {key}: {e}") from None
        # report after the process has had a moment to exit, not the instant after the signal was posted
        try:
            code = entry["proc"].wait(timeout=2)
        except subprocess.TimeoutExpired:
            return {"name": key, "signal": number.name, "running": True,
                    "note": "it did not exit within 2 s; try kill with SIGKILL"}
        return {"name": key, "signal": number.name, "running": False, "exit_code": code}
    if action == "wait":
        try:
            code = entry["proc"].wait(timeout=max(0.1, float(timeout_s or 10)))
        except subprocess.TimeoutExpired:
            return {"name": key, "running": True, "note": f"still running after {timeout_s:g} s"}
        return {"name": key, "exit_code": code, "running": False}
    raise ValueError(f"unknown action {action}. Use start, status, log, signal, kill, wait or list.")


def _driver_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "repl_driver.py"
    path.write_text(REPL_DRIVER, encoding="utf-8")
    return path


def read_until_marker(proc, marker: str, deadline: float) -> str:
    """Read the session's output until the marker, the pipe closes, or the deadline passes.

    The raw descriptor is read directly. select on a buffered pipe reports "ready" only when the kernel
    buffer still has data, so a chunk holding several lines would be taken once into Python's buffer and the
    rest would stay invisible to select until the deadline expired.
    """
    fd = proc.stdout.fileno()
    needle = marker.encode()
    buffer = b""
    while needle not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("no answer in time")
        try:
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        except (OSError, ValueError):
            break
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
    out = []
    for line in buffer.decode("utf-8", "replace").splitlines(keepends=True):
        if line.strip() in (marker, f": {marker}"):
            break
        out.append(line)
    return "".join(out)


def repl(session: str = "python", code: str = "", action: str = "eval", timeout_s: float = RUN_TIMEOUT) -> dict:
    """A session that keeps its state between calls: define a class in one call, use it in the next."""
    kind = str(session or "python").lower()
    if kind not in {"python", "bash"}:
        raise ValueError("session must be python or bash")
    action = str(action or "eval").lower()
    if action not in {"eval", "start", "status", "reset", "close"}:
        raise ValueError(f"unknown action {action}. Use eval, start, status, reset or close.")
    with _sessions_mu:
        existing = _sessions.get(kind)
        if action in {"close", "reset"}:
            if existing is not None:
                kill_tree(existing["proc"])
                for stream in (existing["proc"].stdin, existing["proc"].stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass
                _sessions.pop(kind, None)
            if action == "close":
                return {"session": kind, "closed": True}
            existing = None
        if action == "status":
            return {"session": kind, "running": existing is not None and existing["proc"].poll() is None}
        if action == "start" and existing is not None and existing["proc"].poll() is None:
            return {"session": kind, "running": True, "pid": existing["proc"].pid}
        if existing is None or existing["proc"].poll() is not None:
            marker = f"__REPL_END_{os.urandom(6).hex()}__"
            if kind == "python":
                argv = [sys.executable or "python3", str(_driver_path())]
                env = merged_env({"REPL_MARKER": marker})
            else:
                argv = ["/bin/bash", "-s"]
                env = merged_env({})
            proc = subprocess.Popen(argv, cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=env, start_new_session=True)
            _sessions[kind] = {"proc": proc, "marker": marker}
            existing = _sessions[kind]
        body = str(code or "")
        if not body.strip():
            return {"session": kind, "output": ""}
        marker = existing["marker"]
        # bash needs a command that prints the marker; the python driver prints it itself
        terminator = f"echo {marker}" if kind == "bash" else marker
        try:
            existing["proc"].stdin.write((body.rstrip("\n") + "\n" + terminator + "\n").encode("utf-8"))
            existing["proc"].stdin.flush()
        except (OSError, ValueError) as e:
            _sessions.pop(kind, None)
            raise ValueError(f"the session stopped: {e}") from None
        try:
            output = read_until_marker(existing["proc"], marker,
                                      time.monotonic() + max(1.0, min(float(timeout_s or RUN_TIMEOUT), MAX_TIMEOUT)))
        except ValueError as e:
            # a session that never returns is not recoverable: end it so the next call starts clean
            kill_tree(existing["proc"])
            for stream in (existing["proc"].stdin, existing["proc"].stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            _sessions.pop(kind, None)
            raise ValueError(f"{e}; the session was ended and a new one will start on the next call") from None
        return {"session": kind, "output": clip(output)[0]}


def _describe_file(path: Path) -> dict:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"file": str(path), "error": f"could not parse: {e}"}
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append({"kind": "function", "name": node.name, "line": node.lineno,
                         "decorators": [ast.unparse(d) for d in node.decorator_list]})
        elif isinstance(node, ast.ClassDef):
            members = []
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.append({"name": member.name, "line": member.lineno,
                                   "public": not member.name.startswith("_")})
                elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    members.append({"name": member.target.id, "line": member.lineno, "public": True})
            found.append({"kind": "class", "name": node.name, "line": node.lineno,
                         "bases": [ast.unparse(b) for b in node.bases], "members": members})
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(f"{node.module} ({', '.join(a.name for a in node.names)})")
    doc = ast.get_docstring(tree) or ""
    return {"file": str(path), "lines": source.count("\n") + 1,
           "doc": doc.splitlines()[0] if doc else "", "imports": imports, "symbols": found}


def symbols(path: str = "", limit: int = 20) -> dict:
    """The classes, bases and methods in a Python file or package, read with ast. Nothing is executed."""
    target = inside(path)
    if not target.exists():
        raise ValueError(f"{target} does not exist")
    count = max(1, min(int(limit or 20), 100))
    if target.is_file():
        files = [target]
    else:
        files = sorted(p for p in target.rglob("*.py") if p.is_file() and "__pycache__" not in p.parts)[:count]
        if not files:
            return {"path": str(target), "files": 0, "note": "no .py files under that path"}
    detail = [_describe_file(f) for f in files]
    total = sum(1 for d in detail for s in d.get("symbols", []) if s["kind"] == "class")
    return {"path": str(target), "files": len(detail), "classes": total, "detail": detail}


def check(path: str = "", cwd: str = "", tests: bool = True, python: str = "") -> dict:
    """Lint, type check and test a path with whatever is installed. Missing tools are named, not skipped.

    A relative `path` is read under `cwd`, so `check("serve", cwd="/home/ai-agent/HyperCore")`
    means what it looks like. An absolute path is still jailed to the root.
    """
    workdir = inside(cwd)
    target = inside(path) if str(path or "").startswith("/") else inside(workdir / str(path or ""))
    report = {}
    for tool, argv in [("ruff", ["ruff", "check", str(target)]),
                       ("mypy", ["mypy", str(target)]),
                       ("pylint", ["pylint", str(target)])]:
        binary = shutil.which(tool)
        if not binary:
            report[tool] = {"available": False, "note": f"not installed; install with: {tool}"}
            continue
        try:
            proc = subprocess.run(argv, cwd=str(workdir), capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            report[tool] = {"available": True, "error": str(e)}
            continue
        body, left = clip(proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))
        report[tool] = {"available": True, "exit_code": proc.returncode, "result": body, "omitted": left}
    if tests:
        interp = python or os.environ.get("TERMINAL_PYTHON") or "python3"
        if interp.startswith("/"):
            interp = str(inside(interp))
        probe = subprocess.run([interp, "-c", "import pytest"], capture_output=True, text=True,
                               timeout=20, check=False)
        if probe.returncode != 0:
            report["pytest"] = {"available": False,
                               "note": f"pytest is not in {interp}; install with: install {interp} pytest"}
        else:
            try:
                proc = subprocess.run([interp, "-m", "pytest", "-q", str(target)], cwd=str(workdir),
                                      capture_output=True, text=True, timeout=180, check=False)
            except (OSError, subprocess.TimeoutExpired) as e:
                report["pytest"] = {"available": True, "error": str(e)}
            else:
                body, left = clip(proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))
                report["pytest"] = {"available": True, "exit_code": proc.returncode, "result": body, "omitted": left}
    return {"path": str(target), "report": report,
           "missing_tools": [name for name, entry in report.items() if not entry.get("available")]}


def install(packages: list[str], python: str = "python3", cwd: str = "") -> dict:
    """pip install into whichever interpreter you name. An absolute pyenv path chooses a specific version."""
    pkgs = [str(p).strip() for p in (packages or []) if str(p).strip()]
    if not pkgs:
        raise ValueError("packages is empty")
    interp = str(python or "python3")
    if interp.startswith("/"):
        target = inside(interp)
        if not target.is_file():
            raise ValueError(f"{interp} is not an interpreter under {ROOT}")
        interp = str(target)
    elif not ALLOW_ALL and interp not in ALLOWED:
        raise ValueError(f"{interp} is not on the allowlist")
    workdir = inside(cwd)
    try:
        proc = subprocess.run([interp, "-m", "pip", "install", *pkgs], cwd=str(workdir),
                              capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ValueError(f"install failed: {e}") from None
    body, _ = clip(proc.stdout + ("\n" + proc.stderr if proc.stderr else ""), 4000)
    return {"python": interp, "packages": pkgs, "exit_code": proc.returncode, "output": body}


def available_interpreters() -> list[str]:
    found = []
    for name in ("python3", "python"):
        binary = shutil.which(name)
        if binary:
            found.append(binary)
    pyenv = Path(os.environ.get("PYENV_ROOT") or (Path.home() / ".pyenv"))
    versions = pyenv / "versions"
    if versions.is_dir():
        for entry in sorted(versions.iterdir()):
            interp = entry / "bin" / "python3"
            if interp.is_file():
                found.append(str(interp))
    venv = ROOT / ".venv" / "bin" / "python3"
    if venv.is_file():
        found.append(str(venv))
    return found


def tool_version(binary: str) -> str:
    if binary in _version_cache:
        return _version_cache[binary]
    flag = _VERSION_FLAG.get(os.path.basename(binary), "--version")
    try:
        proc = subprocess.run([binary, flag], capture_output=True, text=True, timeout=8, check=False)
        lines = (proc.stdout or proc.stderr).strip().splitlines()
        value = lines[0][:120] if lines else ""
    except (OSError, subprocess.SubprocessError):
        value = ""
    _version_cache[binary] = value
    return value


def _probe(names: list[str]) -> dict:
    found = {}
    for name in names:
        binary = shutil.which(name)
        found[name] = {"path": binary, "version": tool_version(binary) if binary else None}
    return found


def status() -> dict:
    """What this server can do, and which of the Tier-2, Tier-3 and Tier-4 tools are installed."""
    tier2, tier3, tier4 = _probe(TIER2), _probe(TIER3), _probe(TIER4)
    missing = [name for table in (tier2, tier3, tier4) for name, entry in table.items() if not entry["path"]]
    return {"root": str(ROOT), "state_dir": str(STATE_DIR), "timeout_s": RUN_TIMEOUT,
           "max_out_chars": MAX_OUT, "allow_all": ALLOW_ALL, "allowed_binaries": len(ALLOWED),
           "interpreters": available_interpreters(), "tier2": tier2, "tier3": tier3, "tier4": tier4,
           "missing_tools": missing, "jobs": len(_jobs),
           "note": "TERMINAL_ALLOW_EXTRA adds binaries; TERMINAL_ALLOW_ALL=1 removes the check"}


# ---------------------------------------------------------------- Tier 3: compiled and native work

def _limit(value: float) -> float:
    """Every native tool gets its own timeout, bounded by the same hard maximum as run."""
    return max(1.0, min(float(value or RUN_TIMEOUT), MAX_TIMEOUT))


def _run_argv(argv: list[str], cwd: Path, timeout_s: float, env: dict | None = None, stdin: str = "") -> dict:
    """Run argv directly, with no shell in between. The exit code is reported whatever it is."""
    proc = subprocess.Popen(argv, cwd=str(cwd), env=merged_env(env),
                            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    out, err, timed_out = _finish(proc, timeout_s, stdin.encode("utf-8") if stdin else None)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    return {"argv": argv, "exit_code": proc.returncode, "timed_out": timed_out,
            "stdout": out.decode("utf-8", "replace") if isinstance(out, bytes) else "",
            "stderr": err.decode("utf-8", "replace") if isinstance(err, bytes) else ""}


def _tail(text: str, limit: int) -> tuple[str, int]:
    """Keep the end of text. Compiler and linker errors are at the end, so the head is what gets dropped."""
    if len(text) <= limit:
        return text, 0
    return text[-limit:], len(text) - limit


def _report(result: dict, keep: int = 4000) -> dict:
    """Turn a raw _run_argv result into a tool answer: exit code first, output stated as shown or cut."""
    body, dropped = _tail(result["stdout"] + ("\n" + result["stderr"] if result["stderr"] else ""), keep)
    answer = {"exit_code": result["exit_code"], "output": body}
    if result["timed_out"]:
        answer["timed_out"] = True
        answer["note"] = "the tool was killed at the timeout; raise timeout_s or narrow the work"
    if dropped:
        answer["truncated"] = {"shown_chars": len(body), "omitted_chars": dropped,
                              "note": "the beginning was dropped; use save_to with run for the whole thing"}
    return answer


def _path_translation(recorded: str, target: Path) -> tuple[str, str] | None:
    """The (recorded_root, real_root) pair for a path that exists under a different root.

    A build directory can outlive the tree it was built from: the entries in these
    compile_commands.json files still name a checkout that no longer exists. Matching from the
    last path segment inward recovers the recorded root exactly, so the flags of the real build
    can be replayed against the files that are actually on disk.
    """
    a, b = Path(recorded).parts, Path(str(target)).parts
    shared = 0
    while shared < min(len(a), len(b)) and a[-1 - shared] == b[-1 - shared]:
        shared += 1
    if shared == 0 or len(a) == shared or len(b) == shared:
        return None
    # parts of an absolute path start with "/", so rejoin the rest under it
    return "/" + "/".join(a[1:len(a) - shared]), "/" + "/".join(b[1:len(b) - shared])


def _entry_for(commands: Path, target: Path) -> dict | None:
    try:
        entries = json.loads(commands.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{commands} is not a compile_commands.json: {e}") from None
    if not isinstance(entries, list):
        raise TypeError(f"{commands} is not a compile_commands.json (expected a list of entries)")
    wanted = str(target)
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("command"):
            continue
        recorded = entry.get("file") or ""
        pair = _path_translation(recorded, target)
        if pair is None:
            continue
        if recorded.replace(pair[0], pair[1], 1) == wanted:
            return entry
    return None


def _replay(command: str, entry_file: str, target: Path, syntax_only: bool) -> list[str]:
    """The recorded command, with its paths pointed at the file that is really on disk."""
    tokens = shlex.split(command)
    pair = _path_translation(entry_file, target)
    if pair:
        recorded_root, real_root = pair
        tokens = [t.replace(recorded_root, real_root) if recorded_root in t else t for t in tokens]
    argv, skip = [], False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token == "-o":
            skip = True
            continue
        if token == "-c":
            continue
        argv.append(token)
    if not argv:
        return []
    compiler = shutil.which(os.path.basename(argv[0])) or argv[0]
    argv[0] = compiler
    if syntax_only:
        argv.append("-fsyntax-only")
    else:
        argv.append("-c")
        argv.append("-o")
        argv.append(str(inside(target.with_suffix(".o"))))
    return argv


def build(path: str, build_dir: str = "build", configure: bool = False, target: str = "",
          jobs: int = 0, timeout_s: float = 120.0) -> dict:
    """Configure and build a CMake project. A build directory that already exists is reused.

    configure=True forces a fresh configure; otherwise it runs only when CMakeCache.txt is absent.
    Each step's exit code is reported separately, so a failing configure is not mistaken for a
    failing build.
    """
    project = inside(path)
    if not project.is_dir():
        raise ValueError(f"{project} is not a directory")
    if not (project / "CMakeLists.txt").is_file():
        raise ValueError(f"{project} has no CMakeLists.txt")
    # a bare build_dir is relative to the project, an absolute one is taken as given
    out = inside(build_dir) if str(build_dir).startswith("/") else project / (build_dir or "build")
    out = inside(out)
    cmake = shutil.which("cmake")
    if not cmake:
        raise ValueError("cmake is not installed; install with: install cmake")
    limit = _limit(timeout_s)
    steps = []
    if configure or not (out / "CMakeCache.txt").is_file():
        configure_argv = [cmake, "-S", str(project), "-B", str(out),
                         "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]
        result = _run_argv(configure_argv, project, limit)
        steps.append({"step": "configure", "argv": configure_argv,
                     **_report(result, 2000)})
        if result["exit_code"] != 0:
            return {"project": str(project), "build_dir": str(out), "steps": steps,
                    "exit_code": result["exit_code"], "note": "configure failed, so nothing was built"}
    workers = max(1, min(int(jobs or 0) or min(4, os.cpu_count() or 1), 16))
    argv = [cmake, "--build", str(out), "-j", str(workers)]
    if target:
        argv += ["--target", target]
    result = _run_argv(argv, project, limit)
    steps.append({"step": "build", "argv": argv, "jobs": workers, **_report(result, 4000)})
    candidates = [out / "bin", out]
    artifacts = []
    for directory in candidates:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.stat().st_mode & 0o111 and entry.suffix not in (".sh", ".py"):
                artifacts.append(str(entry))
    artifacts = artifacts[:20]
    return {"project": str(project), "build_dir": str(out), "steps": steps,
            "exit_code": result["exit_code"],
            "artifacts": artifacts}


def compile_source(path: str, compiler: str = "g++", flags: str = "", syntax_only: bool = True,
                   compile_commands: str = "", timeout_s: float = 60.0) -> dict:
    """Compile one C or C++ file, and say plainly whether it compiled.

    syntax_only is the default: it checks a header or a source file for errors without producing an
    object, which is what "do these headers still parse" needs. Pass compile_commands (a path to a
    compile_commands.json) to replay the exact flags the real build used - include paths, defines,
    -std. That is the honest way to verify a header, because a hand-written flag list is not the
    build's flag list.
    """
    target = inside(path)
    if not target.is_file():
        raise ValueError(f"{target} is not a file")
    limit = _limit(timeout_s)
    argv, source = [], ""
    if compile_commands:
        commands = inside(compile_commands)
        entry = _entry_for(commands, target)
        if entry is None:
            raise ValueError(f"no entry for {target} in {commands}; pass compiler and flags instead")
        argv = _replay(entry["command"], entry["file"], target, syntax_only)
        source = str(commands)
    if not argv:
        binary = shutil.which(compiler) or shutil.which("g++") or shutil.which("clang")
        if not binary:
            raise ValueError("no C or C++ compiler found; install g++ or clang")
        argv = [binary] + shlex.split(flags)
        if syntax_only:
            argv.append("-fsyntax-only")
        else:
            argv += ["-c", "-o", str(inside(target.with_suffix(".o")))]
        argv.append(str(target))
    result = _run_argv(argv, target.parent, limit)
    answer: dict = {"file": str(target), "argv": argv[:6] + ["..."] if len(argv) > 6 else argv}
    if source:
        answer["flags_from"] = source
    answer.update(_report(result, 4000))
    answer["compiled"] = result["exit_code"] == 0
    return answer


def tidy(path: str, build_dir: str = "", checks: str = "", limit_files: int = 10,
         timeout_s: float = 120.0) -> dict:
    """Static analysis with clang-tidy, driven by a build directory's compile_commands.json.

    Without build_dir the file is checked with no compilation database, which still finds most
    style and correctness issues but cannot resolve the project's includes.
    """
    target = inside(path)
    if not target.exists():
        raise ValueError(f"{target} does not exist")
    binary = shutil.which("clang-tidy")
    if not binary:
        raise ValueError("clang-tidy is not installed; install with: apt-get install clang-tidy")
    files = [target] if target.is_file() else sorted(
        p for p in target.rglob("*") if p.suffix.lower() in (".c", ".cc", ".cpp", ".hpp", ".h"))[:max(1, min(int(limit_files or 10), 50))]
    if not files:
        return {"path": str(target), "files": 0, "note": "no C or C++ files under that path"}
    argv = [binary]
    if build_dir:
        db = inside(build_dir)
        if not (db / "compile_commands.json").is_file():
            raise ValueError(f"{db} has no compile_commands.json; run build first")
        argv += ["-p", str(db)]
    if checks:
        argv += ["-checks=" + str(checks)]
    argv += [str(f) for f in files]
    result = _run_argv(argv, target.parent if target.parent.is_dir() else ROOT, _limit(timeout_s))
    body, dropped = _tail(result["stdout"] + ("\n" + result["stderr"] if result["stderr"] else ""), 6000)
    warnings = sum(1 for line in result["stdout"].splitlines() if ": warning:" in line)
    errors = sum(1 for line in result["stdout"].splitlines() if ": error:" in line)
    return {"files": len(files), "warnings": warnings, "errors": errors,
            "exit_code": result["exit_code"], "output": body,
            "truncated": {"shown_chars": len(body), "omitted_chars": dropped} if dropped else None}


def debug(action: str = "run", path: str = "", command: str = "", args: str = "",
          commands: str = "", core: str = "", tracer: str = "strace", timeout_s: float = 60.0) -> dict:
    """Look at a native binary: run it under gdb, read a core dump, check for leaks, list its
    symbols, or trace what it calls.

    run    gdb --batch on the binary; commands are gdb lines, one per line, default "run"
    core   gdb --batch on the binary plus a core file
    leak   valgrind --leak-check=full; the summary is parsed, not just dumped
    symbols  nm and objdump on the binary
    trace  the command under strace, ltrace or /usr/bin/time -v
    """
    action = str(action or "run").lower()
    if action not in {"run", "core", "leak", "symbols", "trace"}:
        raise ValueError("action must be run, core, leak, symbols or trace")
    limit = _limit(timeout_s)
    if action == "trace":
        if not command:
            raise ValueError("command is required for trace")
        check_command(command)
        tool = str(tracer or "strace").lower()
        if tool not in {"strace", "ltrace", "time"}:
            raise ValueError("tracer must be strace, ltrace or time")
        binary = shutil.which(tool)
        if not binary:
            raise ValueError(f"{tool} is not installed")
        argv = ([binary, "-v"] if tool == "time" else [binary]) + ["/bin/sh", "-c", command]
        result = _run_argv(argv, inside(""), limit)
        answer: dict = {"tracer": tool, "command": command}
        answer.update(_report(result, 4000))
        if tool == "time":
            stats = {}
            for line in result["stderr"].splitlines():
                key, _, value = line.rpartition(": ")   # the label itself contains colons
                key = key.strip().lower()
                if any(label in key for label in ("user time", "system time", "wall clock",
                                                   "maximum resident", "exit status", "page faults",
                                                   "context switches")):
                    stats[key] = value.strip()
            answer["stats"] = stats
        return answer
    target = inside(path)
    if not target.is_file():
        raise ValueError(f"{target} is not a file")
    if action == "symbols":
        nm = shutil.which("nm")
        objdump = shutil.which("objdump")
        rows: dict = {}
        if nm:
            result = _run_argv([nm, "-C", str(target)], target.parent, limit)
            names = [line for line in result["stdout"].splitlines() if line.strip()]
            rows = {"symbols": names[:200], "total": len(names)}
        if objdump:
            result = _run_argv([objdump, "-n", str(target)], target.parent, limit)
            body, dropped = _tail(result["stdout"], 1500)
            rows["dynamic_symbols"] = body
            if dropped:
                rows["dynamic_omitted"] = dropped
        return {"binary": str(target), "exit_code": 0, **rows}
    if action == "leak":
        binary = shutil.which("valgrind")
        if not binary:
            raise ValueError("valgrind is not installed")
        argv = [binary, "--leak-check=full", "--show-leak-kinds=all", "--error-exitcode=1", str(target)]
        if args:
            argv += shlex.split(args)
        result = _run_argv(argv, target.parent, limit)
        summary = {}
        for line in result["stderr"].splitlines():
            for key in ("definitely lost", "indirectly lost", "possibly lost", "still reachable",
                        "in use at exit", "total heap usage"):
                if key in line:
                    summary[key] = line.split(key, 1)[1].strip().lstrip(":").strip()
        return {"binary": str(target), "exit_code": result["exit_code"], "leaks": summary,
              **({"output": _tail(result["stderr"], 3000)[0]} if summary == {} else {})}
    gdb = shutil.which("lldb" if action == "core" and not shutil.which("gdb") else "gdb")
    if not gdb:
        raise ValueError("gdb is not installed; install with: apt-get install gdb")
    lines = [line.strip() for line in str(commands or "").splitlines() if line.strip()]
    if not lines:
        lines = ["run"] if action == "run" else []
    argv = [gdb, "--batch"]
    for line in lines:
        argv += ["-ex", line]
    argv.append(str(target))
    if action == "core":
        core_path = inside(core)
        if not core_path.is_file():
            raise ValueError(f"{core} is not a core file")
        argv.append(str(core_path))
    if args:
        argv += shlex.split(args)
    result = _run_argv(argv, target.parent, limit)
    return {"binary": str(target), "gdb": os.path.basename(gdb), **_report(result, 4000)}



# ---------------------------------------------------------------- Tier 4: data, inspection, orchestration

def _shape(value, depth: int = 0) -> dict:
    """A description of a JSON or TOML value: keys, types, counts. Not the contents."""
    if isinstance(value, dict):
        return {"type": "object", "keys": {k: _shape(v, depth + 1) if depth < 2 else type(v).__name__
                                          for k, v in list(value.items())[:60]},
                "count": len(value)}
    if isinstance(value, list):
        return {"type": "array", "count": len(value),
                "first": _shape(value[0], depth + 1) if value and depth < 2 else None}
    if isinstance(value, str):
        return {"type": "string", "value": value, "length": len(value), "preview": value[:120]}
    return {"type": type(value).__name__, "value": value}


# a statement that already bounds its own rows needs no LIMIT added to it
_SQL_LIMIT = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


def query(path: str, expression: str = "", action: str = "",
          limit: int = 50, timeout_s: float = 30.0) -> dict:
    """Read structured data without dumping it: JSON with jq, YAML with yq, TOML with tomllib,
    SQLite with sqlite3.

    json     no expression gives the shape of the document; an expression is a jq filter
    yaml     a yq filter, or the top-level keys when there is no filter
    toml     a dotted key path, or the table names when there is no path
    sqlite   action=schema lists tables and columns; action=sql runs a read-only query, and
            `limit` becomes a LIMIT when the statement has none

    A result longer than TERMINAL_MAX_OUT keeps its beginning: rows and filter output are read
    top-down, so the first part is the useful part, and the number of characters left out is
    stated rather than dropped silently.
    """
    target = inside(path)
    if not target.is_file():
        raise ValueError(f"{target} is not a file")
    deadline = _limit(timeout_s)
    suffix = target.suffix.lower()
    action = str(action or "").lower()
    if not action:
        action = {"json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml"}.get(
            suffix.lstrip("."), "sqlite" if suffix in (".db", ".sqlite", ".sqlite3") else "")
        if not action:
            raise ValueError(f"cannot query a {suffix or 'unknown'} file; use run with rg or head")
    if action == "json":
        binary = shutil.which("jq")
        if not binary:
            raise ValueError("jq is not installed")
        if expression:
            result = _run_argv([binary, "-r", str(expression), str(target)], target.parent, deadline)
            body, dropped = clip(result["stdout"], MAX_OUT)
            return {"file": str(target), "filter": expression, "exit_code": result["exit_code"],
                    "result": body,
                    "truncated": {"shown_chars": len(body), "omitted_chars": dropped,
                                 "note": "the end of the filter output is not shown; narrow the "
                                         "filter, or run jq with run and save_to"} if dropped else None}
        try:
            data = json.loads(target.read_text(encoding="utf-8", errors="replace"))
        except ValueError as e:
            raise ValueError(f"{target} is not valid JSON: {e}") from None
        return {"file": str(target), "bytes": target.stat().st_size, "shape": _shape(data)}
    if action == "yaml":
        binary = shutil.which("yq")
        if not binary:
            raise ValueError("yq is not installed; install with: apt-get install yq")
        filter_ = str(expression or "keys")
        result = _run_argv([binary, filter_, str(target)], target.parent, deadline)
        body, dropped = clip(result["stdout"], MAX_OUT)
        return {"file": str(target), "filter": filter_, "exit_code": result["exit_code"],
                "result": body,
                "truncated": {"shown_chars": len(body), "omitted_chars": dropped,
                             "note": "the end of the filter output is not shown; narrow the filter"}
                if dropped else None}
    if action == "toml":
        try:
            import tomllib
        except ImportError:
            raise ValueError("tomllib needs Python 3.11 or newer") from None
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001 - a malformed config is a tool result, not a crash
            raise ValueError(f"{target} is not valid TOML: {e}") from None
        node = data
        for part in [p for p in str(expression or "").split(".") if p]:
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"no key {part!r} in {target}")
            node = node[part]
        return {"file": str(target), "path": expression or "root", "result": _shape(node)}
    if action == "sqlite":
        binary = shutil.which("sqlite3")
        if not binary:
            raise ValueError("sqlite3 is not installed")
        count = max(1, min(int(limit or 50), 500))
        if not expression:
            result = _run_argv([binary, "-json", f"file:{target}?mode=ro",
                               "SELECT type, name FROM sqlite_master ORDER BY name"], target.parent, deadline)
            if result["exit_code"] != 0:
                return {"database": str(target), "exit_code": result["exit_code"],
                        "error": result["stderr"][:1000]}
            try:
                tables = json.loads(result["stdout"] or "[]")
            except ValueError:
                tables = []
            columns = {}
            for row in tables:
                if row.get("type") != "table":
                    continue
                probe = _run_argv([binary, "-json", f"file:{target}?mode=ro",
                                  f"PRAGMA table_info({row['name']})"], target.parent, deadline)
                try:
                    columns[row["name"]] = [c.get("name") for c in json.loads(probe["stdout"] or "[]")]
                except ValueError:
                    columns[row["name"]] = []
            return {"database": str(target), "tables": tables, "columns": columns,
                    "note": "open with action=sql and a SELECT; the database is opened read-only"}
        sql = str(expression).strip()
        if not sql.lower().startswith(("select", "with", "pragma")):
            raise ValueError("only SELECT, WITH or PRAGMA statements are allowed on this database")
        # A PRAGMA has no LIMIT clause. Otherwise the limit argument is applied, not just
        # suggested: a statement with no LIMIT of its own gets one appended.
        added_limit = False
        if sql.lower().startswith(("select", "with")) and not _SQL_LIMIT.search(sql):
            sql = sql.rstrip(";").rstrip() + f" LIMIT {count}"
            added_limit = True
        result = _run_argv([binary, "-json", f"file:{target}?mode=ro", sql], target.parent, deadline)
        body, dropped = clip(result["stdout"], MAX_OUT)
        answer = {"database": str(target), "sql": sql, "exit_code": result["exit_code"],
                  "rows": body, "stderr": result["stderr"][:1000]}
        if added_limit:
            answer["row_limit"] = count
        if dropped:
            answer["truncated"] = {"shown_chars": len(body), "omitted_chars": dropped,
                                   "note": "the end of the result is not shown; narrow the query or "
                                           "run sqlite3 with run and save_to"}
        return answer
    raise ValueError(f"unknown action {action}")


def compare(path: str, other: str = "", git_ref: str = "", context: int = 3,
            timeout_s: float = 30.0) -> dict:
    """Show what changed. Two files, or a file against its committed version in git.

    The diff is unified and the number of changed lines is counted, so a large difference is
    summarised before it is truncated.
    """
    left = inside(path)
    if not left.is_file():
        raise ValueError(f"{left} is not a file")
    limit = _limit(timeout_s)
    if other:
        right = inside(other)
        if not right.is_file():
            raise ValueError(f"{other} is not a file")
        result = _run_argv(["diff", "-u", "-U", str(max(0, int(context))), str(left), str(right)],
                           left.parent, limit)
        body, dropped = _tail(result["stdout"], MAX_OUT)
        added = sum(1 for line in result["stdout"].splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in result["stdout"].splitlines() if line.startswith("-") and not line.startswith("---"))
        return {"left": str(left), "right": str(right), "identical": result["exit_code"] == 0,
                "added_lines": added, "removed_lines": removed, "exit_code": result["exit_code"],
                "diff": body,
                "truncated": {"shown_chars": len(body), "omitted_chars": dropped} if dropped else None}
    if not git_ref:
        raise ValueError("give another file to compare with, or a git_ref such as HEAD")
    git = shutil.which("git")
    if not git:
        raise ValueError("git is not installed")
    probe = _run_argv([git, "rev-parse", "--show-toplevel"], left.parent, limit)
    repo = probe["stdout"].strip()
    if probe["exit_code"] != 0 or not repo:
        raise ValueError(f"{left} is not inside a git repository")
    try:
        relative = str(Path(left).resolve().relative_to(Path(repo)))
    except ValueError:
        raise ValueError(f"{left} is not inside the repository at {repo}") from None
    blob = _run_argv([git, "show", f"{git_ref}:{relative}"], Path(repo), limit)
    if blob["exit_code"] != 0:
        raise ValueError(f"git has no {git_ref} for {relative}: {blob['stderr'][:300]}")
    import difflib
    current = left.read_text(encoding="utf-8", errors="replace").splitlines()
    previous = blob["stdout"].splitlines()
    diff = list(difflib.unified_diff(previous, current, str(left) + "@" + git_ref, str(left),
                                    n=max(0, int(context))))
    body, dropped = _tail("\n".join(diff), MAX_OUT)
    return {"left": f"{left}@{git_ref}", "right": str(left), "identical": not diff,
            "added_lines": sum(1 for line in diff if line.startswith("+") and not line.startswith("+++")),
            "removed_lines": sum(1 for line in diff if line.startswith("-") and not line.startswith("---")),
            "diff": body,
            "truncated": {"shown_chars": len(body), "omitted_chars": dropped} if dropped else None}



TOOLS = [
    {"name": "run",
     "description": "Run one shell command with /bin/sh. Pipes, redirection and globs work; every binary must be "
                    "on the allowlist. cwd must stay inside the working root. Each call is a new shell: nothing "
                    "persists between calls except files you wrote. Output over the limit is truncated with the "
                    "missing character count stated; use save_to to keep it all. Pass timeout_s for a build, a test "
                    "run or a server - the default is 30 s.",
     "inputSchema": {"type": "object", "properties": {
         "command": {"type": "string", "description": "The command to run."},
         "cwd": {"type": "string", "description": "Directory to run in, inside the root. Default the root."},
         "env": {"type": "object", "description": "Extra environment variables."},
         "timeout_s": {"type": "number", "description": "Seconds before the command is killed. Default 30."},
         "stdin": {"type": "string", "description": "Text to feed the command on stdin."},
         "save_to": {"type": "string", "description": "Write the full output to this file under the root."}},
         "required": ["command"]}},
    {"name": "pipe",
     "description": "Run a pipeline as separate stages so each stage's exit code and stderr are reported, not just "
                    "the last one. Stages are given as a list of commands; a single string with one command per "
                    "line is accepted too.",
     "inputSchema": {"type": "object", "properties": {
         "stages": {"type": "array", "items": {"type": "string"}, "description": "One command per stage."},
         "cwd": {"type": "string", "description": "Directory to run in, inside the root."},
         "env": {"type": "object", "description": "Extra environment variables for every stage."},
         "timeout_s": {"type": "number", "description": "Seconds per stage."}}, "required": ["stages"]}},
    {"name": "process",
     "description": "Background jobs: start, status, log, signal, kill, wait, list. Output goes to a log file that "
                    "the docs tools can read.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["start", "status", "log", "signal", "kill", "wait", "list"]},
         "name": {"type": "string", "description": "Job name."},
         "command": {"type": "string", "description": "Command for start."},
         "cwd": {"type": "string", "description": "Working directory for start."},
         "env": {"type": "object", "description": "Extra environment variables for the job."},
         "sig": {"type": "string", "enum": ["TERM", "INT", "HUP", "KILL"], "description": "Default TERM."},
         "timeout_s": {"type": "number", "description": "Seconds for wait."},
         "lines": {"type": "integer", "description": "Log lines for log. Default 40."}}}},
    {"name": "repl",
     "description": "A python or bash session that keeps its state between calls: define a class in one call, "
                    "instantiate it in the next. Use reset to clear it, close to end it.",
     "inputSchema": {"type": "object", "properties": {
         "session": {"type": "string", "enum": ["python", "bash"], "description": "Default python."},
         "code": {"type": "string", "description": "The code to run."},
         "action": {"type": "string", "enum": ["eval", "start", "status", "reset", "close"],
                    "description": "Default eval."},
         "timeout_s": {"type": "number", "description": "Seconds before the call gives up."}}}},
    {"name": "symbols",
     "description": "Read a Python file or package with ast and report the classes, their bases, their methods and "
                    "the top-level functions. Nothing is executed.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A .py file or a directory under the root."},
         "limit": {"type": "integer", "description": "Maximum files to read, 1 to 100. Default 20."}},
         "required": ["path"]}},
    {"name": "check",
     "description": "Lint, type check and test a path with ruff, mypy, pylint and pytest - whichever are installed. "
                    "Tools that are not installed are named so they can be installed.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A file or directory under the root."},
         "cwd": {"type": "string", "description": "Where to run from, for pytest and config discovery."},
         "tests": {"type": "boolean", "description": "Run pytest too. Default true."},
         "python": {"type": "string", "description": "Interpreter for pytest. Default python3."}}, "required": ["path"]}},
    {"name": "install",
     "description": "pip install packages into a named interpreter. Pass an absolute pyenv interpreter path to "
                    "choose a specific version.",
     "inputSchema": {"type": "object", "properties": {
         "packages": {"type": "array", "items": {"type": "string"}},
         "python": {"type": "string", "description": "Interpreter to install into. Default python3."},
         "cwd": {"type": "string", "description": "Where to run pip from."}}, "required": ["packages"]}},
    {"name": "build",
     "description": "Configure and build a CMake project with cmake and make or ninja. Reuses an existing build "
                    "directory unless configure is true. Each step reports its own exit code.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The project directory under the root."},
         "build_dir": {"type": "string", "description": "Build directory inside the project. Default build."},
         "configure": {"type": "boolean", "description": "Force a fresh configure. Default false."},
         "target": {"type": "string", "description": "Build only this target."},
         "jobs": {"type": "integer", "description": "Parallel jobs. Default min(4, cores)."},
         "timeout_s": {"type": "number", "description": "Seconds per step. Default 120."}}, "required": ["path"]}},
    {"name": "compile",
     "description": "Compile one C or C++ file and say whether it compiled. Default is -fsyntax-only, which "
                    "checks a header or source without producing an object. Give a compile_commands.json to use "
                    "the exact flags the real build used.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The .c, .cpp or .hpp file under the root."},
         "compiler": {"type": "string", "description": "g++ or clang++. Default g++."},
         "flags": {"type": "string", "description": "Extra flags, e.g. -std=c++17 -Wall."},
         "syntax_only": {"type": "boolean", "description": "Check without writing an object. Default true."},
         "compile_commands": {"type": "string", "description": "A compile_commands.json to take flags from."},
         "timeout_s": {"type": "number", "description": "Seconds. Default 60."}}, "required": ["path"]}},
    {"name": "tidy",
     "description": "Run clang-tidy over C or C++ files. Pass a build directory to use its "
                    "compile_commands.json, so the includes of the project resolve. Reports warning and error "
                    "counts.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A file or directory under the root."},
         "build_dir": {"type": "string", "description": "A build directory with compile_commands.json."},
         "checks": {"type": "string", "description": "A clang-tidy check filter, e.g. clang-analyzer-*."},
         "limit_files": {"type": "integer", "description": "Maximum files to analyze. Default 10."},
         "timeout_s": {"type": "number", "description": "Seconds. Default 120."}}, "required": ["path"]}},
    {"name": "debug",
     "description": "Inspect a native binary: run it under gdb (action=run), read a core dump (core), check for "
                    "memory leaks with valgrind (leak), list its symbols (symbols), or trace a command with "
                    "strace, ltrace or time (trace).",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["run", "core", "leak", "symbols", "trace"]},
         "path": {"type": "string", "description": "The binary or core file under the root."},
         "command": {"type": "string", "description": "The command to trace (action=trace)."},
         "args": {"type": "string", "description": "Arguments for the binary."},
         "commands": {"type": "string", "description": "gdb commands, one per line. Default run."},
         "core": {"type": "string", "description": "Core file for action=core."},
         "tracer": {"type": "string", "enum": ["strace", "ltrace", "time"], "description": "Default strace."},
         "timeout_s": {"type": "number", "description": "Seconds. Default 60."}}, "required": ["action"]}},
    {"name": "query",
     "description": "Read structured data without dumping it: jq for JSON, yq for YAML, tomllib for TOML, "
                    "sqlite3 for a database (schema, or a read-only SELECT). Use this instead of reading a "
                    "whole config file.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The file under the root."},
         "action": {"type": "string", "enum": ["json", "yaml", "toml", "sqlite"],
                    "description": "Inferred from the extension if omitted."},
         "expression": {"type": "string", "description": "jq/yq filter, dotted TOML key path, or SQL statement."},
         "limit": {"type": "integer", "description": "Rows for a SQL query: added as a LIMIT when "
                     "the statement has none. Default 50."},
         "timeout_s": {"type": "number", "description": "Seconds. Default 30."}}, "required": ["path"]}},
    {"name": "compare",
     "description": "Diff two files, or a file against its committed version in git (git_ref=HEAD). Reports the "
                    "number of added and removed lines before the diff is truncated.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The first file under the root."},
         "other": {"type": "string", "description": "The second file to diff against."},
         "git_ref": {"type": "string", "description": "A git revision such as HEAD or a commit hash."},
         "context": {"type": "integer", "description": "Context lines in the diff. Default 3."},
         "timeout_s": {"type": "number", "description": "Seconds. Default 30."}}, "required": ["path"]}},
    {"name": "status",
     "description": "The working root, the limits, the interpreters available, and which of the Tier-2, Tier-3 "
                    "and Tier-4 tools (git, ruff, mypy, pytest, cmake, clang-tidy, gdb, valgrind, jq, yq, "
                    "sqlite3, docker, strace...) are installed and their versions.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def fit_json(payload: dict, limit: int) -> str:
    """JSON of payload that fits in limit characters.

    Large bodies shrink from the end first. stderr is last, and it shrinks from the
    front, so an error at the end of stderr is what remains when the cap hits.
    """
    data = dict(payload)

    def dump() -> str:
        return json.dumps(data, indent=1)

    text = dump()
    if limit <= 0 or len(text) <= limit:
        return text
    for key in ("stdout", "output", "diff", "result", "rows", "dynamic_symbols", "stderr"):
        value = data.get(key)
        if not isinstance(value, str) or not value or len(text) <= limit:
            continue
        keep_tail = key == "stderr"
        while data[key] and len(text) > limit:
            cut = max(1, len(text) - limit)
            data[key] = data[key][cut:] if keep_tail else data[key][:-cut]
            text = dump()
        if data[key] == value:
            continue
        note = dict(data["truncated"]) if isinstance(data.get("truncated"), dict) else {}
        note["result_cap"] = (f"{key} shortened by {len(value) - len(data[key])} characters "
                              "so the result fits the hub cap")
        data["truncated"] = note
        text = dump()
        while data[key] and len(text) > limit:
            cut = max(1, len(text) - limit)
            data[key] = data[key][cut:] if keep_tail else data[key][:-cut]
            text = dump()
    return text


def text(body: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": error}


def dumped(payload: dict) -> str:
    return fit_json(payload, RESULT_CAP)


def call(name: str, args: dict) -> dict:
    try:
        if name == "run":
            return text(dumped(run(str(args.get("command") or ""), str(args.get("cwd") or ""),
                                    args.get("env") or {}, float(args.get("timeout_s") or RUN_TIMEOUT),
                                    str(args.get("stdin") or ""), str(args.get("save_to") or ""))))
        if name == "pipe":
            return text(dumped(pipe(coerce_sequence(args.get("stages")), str(args.get("cwd") or ""),
                                       args.get("env") or {}, float(args.get("timeout_s") or RUN_TIMEOUT))))
        if name == "process":
            return text(dumped(process(str(args.get("action") or "list"), str(args.get("name") or ""),
                                         str(args.get("command") or ""), str(args.get("cwd") or ""),
                                         args.get("env") or {}, str(args.get("sig") or "TERM"),
                                         float(args.get("timeout_s") or 10.0), int(args.get("lines") or 40))))
        if name == "repl":
            return text(dumped(repl(str(args.get("session") or "python"), str(args.get("code") or ""),
                                       str(args.get("action") or "eval"),
                                       float(args.get("timeout_s") or RUN_TIMEOUT))))
        if name == "symbols":
            return text(dumped(symbols(str(args.get("path") or ""), int(args.get("limit") or 20))))
        if name == "check":
            return text(dumped(check(str(args.get("path") or ""), str(args.get("cwd") or ""),
                                        bool(args.get("tests", True)), str(args.get("python") or ""))))
        if name == "install":
            return text(dumped(install(coerce_sequence(args.get("packages")), str(args.get("python") or "python3"),
                                         str(args.get("cwd") or ""))))
        if name == "build":
            return text(dumped(build(str(args.get("path") or ""), str(args.get("build_dir") or "build"),
                                       bool(args.get("configure", False)), str(args.get("target") or ""),
                                       int(args.get("jobs") or 0), float(args.get("timeout_s") or 120.0))))
        if name == "compile":
            return text(dumped(compile_source(str(args.get("path") or ""), str(args.get("compiler") or "g++"),
                                                 str(args.get("flags") or ""), bool(args.get("syntax_only", True)),
                                                 str(args.get("compile_commands") or ""),
                                                 float(args.get("timeout_s") or 60.0))))
        if name == "tidy":
            return text(dumped(tidy(str(args.get("path") or ""), str(args.get("build_dir") or ""),
                                       str(args.get("checks") or ""), int(args.get("limit_files") or 10),
                                       float(args.get("timeout_s") or 120.0))))
        if name == "debug":
            return text(dumped(debug(str(args.get("action") or "run"), str(args.get("path") or ""),
                                        str(args.get("command") or ""), str(args.get("args") or ""),
                                        str(args.get("commands") or ""), str(args.get("core") or ""),
                                        str(args.get("tracer") or "strace"),
                                        float(args.get("timeout_s") or 60.0))))
        if name == "query":
            return text(dumped(query(str(args.get("path") or ""), str(args.get("expression") or ""),
                                        str(args.get("action") or ""),
                                        int(args.get("limit") or 50), float(args.get("timeout_s") or 30.0))))
        if name == "compare":
            return text(dumped(compare(str(args.get("path") or ""), str(args.get("other") or ""),
                                         str(args.get("git_ref") or ""), int(args.get("context") or 3),
                                         float(args.get("timeout_s") or 30.0))))
        if name == "status":
            return text(dumped(status()))
    except Exception as e:  # noqa: BLE001 - the model reads the failure as a tool result
        return text(str(e), error=True)
    return text(f"unknown tool {name}", error=True)


def handle(msg: dict):
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "terminal", "title": "Shell, processes and code tools", "version": "0.1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": call(str(params.get("name") or ""),
                                    params.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def close_all() -> None:
    for job in list(_jobs.values()):
        kill_tree(job["proc"])
        try:
            job["log_handle"].close()
        except OSError:
            pass
    for session in list(_sessions.values()):
        kill_tree(session["proc"])
        for stream in (session["proc"].stdin, session["proc"].stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


atexit.register(close_all)


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    out = threading.Lock()

    def answer(msg):
        reply = handle(msg)
        if reply is None:
            return
        with out:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("method") == "tools/call":
            threading.Thread(target=answer, args=(msg,), daemon=True).start()
        else:
            answer(msg)


if __name__ == "__main__":
    main()
