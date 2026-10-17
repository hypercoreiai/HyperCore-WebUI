"""The terminal server: run, pipe, process, repl, symbols, check, install, status, build,
compile, tidy, debug, query, compare.

    python3 -m unittest serve.mcp.tests.test_terminal

The server is started with TERMINAL_ROOT pointed at a temporary directory, so nothing
here writes to the real /home.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from serve.mcp.tests.harness import McpServer, case, entry, require, write

SERVER = "terminal"
ADVERTISED = ["build", "check", "compile", "compare", "debug", "install", "pipe", "process",
              "query", "repl", "run", "symbols", "status", "tidy"]

NOTES_PY = '''"""A small object graph, for the symbols reader."""
import json


class Ledger:
    def __init__(self, rows):
        self.rows = list(rows)

    def total(self):
        return sum(r["amount"] for r in self.rows)

    def as_json(self):
        return json.dumps(self.rows)


def load(path):
    return Ledger(json.load(open(path, encoding="utf-8")))
'''

BAD_PY = "import os\nx = 1\n"
GOOD_PY = "def double(n: int) -> int:\n    return n * 2\n"
OK_TEST = '''import unittest


class Smoke(unittest.TestCase):
    def test_math(self):
        self.assertEqual(2 + 2, 4)
'''

CMAKE = '''cmake_minimum_required(VERSION 3.16)
project(smoke CXX)
add_executable(smoke main.cpp)
'''
MAIN_CPP = '''#include <iostream>
int main() {
    int a[3] = {1, 2, 3};
    int s = 0;
    for (int i = 0; i < 3; ++i) s += a[i];
    std::cout << "sum=" << s << "\\n";
    return 0;
}
'''
BROKEN_CPP = "int main( { return 0;\n"
JSON_FIXTURE = '{"terminal": {"timeout_s": 90, "max_result_chars": 6000}, "servers": ["web", "home"]}'
YAML_FIXTURE = "terminal:\n  timeout_s: 30\n  allow_extra: [sudo, nproc]\n"
TOML_FIXTURE = '[engine]\nmax_context = 8192\ncache = "iq3"\n'
A_TXT = "line one\nshared\n"
B_TXT = "line one\nshared\nline three\n"


class Terminal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        write(cls.root, "fx/notes.py", NOTES_PY)
        write(cls.root, "fx/bad.py", BAD_PY)
        write(cls.root, "fx/good.py", GOOD_PY)
        write(cls.root, "fx/test_ok.py", OK_TEST)
        write(cls.root, "fx/records.json", JSON_FIXTURE)
        write(cls.root, "fx/config.yaml", YAML_FIXTURE)
        write(cls.root, "fx/settings.toml", TOML_FIXTURE)
        write(cls.root, "fx/a.txt", A_TXT)
        write(cls.root, "fx/b.txt", B_TXT)
        con = sqlite3.connect(cls.root / "fx/index.sqlite")
        con.execute("CREATE TABLE tools (name TEXT, calls INTEGER)")
        con.executemany("INSERT INTO tools VALUES (?, ?)",
                        [("run", 12), ("browse", 3), ("read_file", 7)])
        con.commit()
        con.close()
        write(cls.root, "native/CMakeLists.txt", CMAKE)
        write(cls.root, "native/main.cpp", MAIN_CPP)
        write(cls.root, "native/broken.cpp", BROKEN_CPP)
        subprocess.run(["git", "init", "-q"], cwd=cls.root, check=True)
        git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
        subprocess.run(git + ["add", "fx/a.txt"], cwd=cls.root, check=True)
        subprocess.run(git + ["commit", "-q", "-m", "initial"], cwd=cls.root, check=True)
        cls.srv = McpServer(entry("terminal_mcp.py"), env={
            "TERMINAL_ROOT": str(cls.root),
            "TERMINAL_STATE_DIR": str(cls.root / ".terminal"),
            "TERMINAL_MAX_OUT": "1200",
            "TERMINAL_TIMEOUT_S": "20",
            # These cases are about the allowlist. A host that exports TERMINAL_ALLOW_ALL=1 turns the
            # check off, which makes them pass for the wrong reason, so pin it here.
            "TERMINAL_ALLOW_ALL": "0",
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.tmp.cleanup()

    # ---- what the server advertises -----------------------------------------
    @case(SERVER, "tools", "advertises all 14 tools")
    def test_terminal__advertised_tools(self):
        self.assertEqual(self.srv.tools, sorted(ADVERTISED))

    # ---- run --------------------------------------------------------------
    @case(SERVER, "run", "prints, reports exit code and cwd")
    def test_terminal__run_basic(self):
        r = self.srv.call("run", {"command": "printf hi"})
        self.assertTrue(r.ok)
        out = r.data
        self.assertEqual(out["stdout"], "hi")
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["cwd"], str(self.root))

    @case(SERVER, "run", "a failing command reports its exit code, not an error")
    def test_terminal__run_exit_code(self):
        out = self.srv.call("run", {"command": "false"}).data
        self.assertEqual(out["exit_code"], 1)

    @case(SERVER, "run", "relative cwd resolves under the root")
    def test_terminal__run_cwd_relative(self):
        out = self.srv.call("run", {"command": "pwd", "cwd": "fx"}).data
        self.assertEqual(out["cwd"], str(self.root / "fx"))

    @case(SERVER, "run", "cwd outside the root is refused")
    def test_terminal__run_cwd_outside_root(self):
        r = self.srv.call("run", {"command": "ls", "cwd": "/tmp"})
        self.assertFalse(r.ok)
        self.assertIn("outside", r.text.lower())

    @case(SERVER, "run", "a binary not on the allowlist is refused")
    def test_terminal__run_allowlist(self):
        r = self.srv.call("run", {"command": "lmnope something"})
        self.assertFalse(r.ok)
        self.assertIn("allowlist", r.text.lower())

    @case(SERVER, "run", "substitution, subshells and loop conditions are checked too")
    def test_terminal__run_allowlist_deep(self):
        for command in ("echo `lmnope`", "(lmnope)", "while lmnope; do :; done",
                        "if lmnope; then echo x; fi"):
            r = self.srv.call("run", {"command": command})
            self.assertFalse(r.ok, f"{command!r} was accepted though it runs a disallowed program")
            self.assertIn("allowlist", r.text.lower())
        # a builtin's arguments are not commands, and a for header names variables
        for command in ("if true; then echo hi; fi", "for i in 1 2; do echo $i; done",
                        "while read line; do echo $line; done"):
            r = self.srv.call("run", {"command": command})
            self.assertTrue(r.ok, f"{command!r} was refused although every program is allowed")

    @case(SERVER, "run", "a parenthesised subshell runs normally")
    def test_terminal__run_subshell(self):
        out = self.srv.call("run", {"command": "echo hi | (cat | wc -l)"}).data
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["stdout"].strip(), "1")

    @case(SERVER, "run", "/dev/null is allowed through the path jail")
    def test_terminal__run_dev_null(self):
        out = self.srv.call("run", {"command": "printf hello > /dev/null; printf done"}).data
        self.assertEqual(out["stdout"], "done")

    @case(SERVER, "run", "long output is clipped and says how much is missing")
    def test_terminal__run_truncation(self):
        out = self.srv.call("run", {"command": 'python3 -c "print(\'x\' * 3000)"'}).data
        self.assertLessEqual(len(out["stdout"]), 1200)
        self.assertEqual(out["truncated"]["stdout_chars"], 3001 - 1200)
        self.assertIn("save_to", out["truncated"]["note"])

    @case(SERVER, "run", "save_to keeps the whole output in a file under the root")
    def test_terminal__run_save_to(self):
        out = self.srv.call("run", {"command": 'python3 -c "print(\'y\' * 5000)"',
                                    "save_to": "captured.txt"}).data
        self.assertEqual(out["saved_bytes"], 5001)
        self.assertTrue((self.root / "captured.txt").is_file())

    @case(SERVER, "run", "stdin is piped to the command")
    def test_terminal__run_stdin(self):
        out = self.srv.call("run", {"command": "wc -l", "stdin": "one\ntwo\n"}).data
        self.assertEqual(out["stdout"].strip(), "2")

    @case(SERVER, "run", "extra environment variables reach the command")
    def test_terminal__run_env(self):
        out = self.srv.call("run", {"command": "printf $EXTRA", "env": {"EXTRA": "set"}}).data
        self.assertEqual(out["stdout"], "set")

    @case(SERVER, "run", "a slow command times out instead of hanging")
    def test_terminal__run_timeout(self):
        out = self.srv.call("run", {"command": "sleep 5", "timeout_s": 1}, timeout=20).data
        self.assertTrue(out["timed_out"])

    @case(SERVER, "run", "shell features (pipes, globs, redirection) work")
    def test_terminal__run_shell_features(self):
        expected = sum((self.root / f"fx/{n}").read_text().count("\n") for n in ("a.txt", "b.txt"))
        out = self.srv.call("run", {"command": "cat fx/a.txt fx/b.txt | wc -l"}).data
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["stdout"].strip(), str(expected))

    # ---- pipe -------------------------------------------------------------
    @case(SERVER, "pipe", "each stage reports its own exit code")
    def test_terminal__pipe_stages(self):
        out = self.srv.call("pipe", {"stages": ["seq 1 6", "grep -v 3", "wc -l"]}).data
        self.assertEqual(out["exit_code"], 0)
        self.assertIsNone(out["failed_stage"])
        self.assertEqual(out["output"].strip(), "5")
        self.assertEqual([s["exit_code"] for s in out["stages"]], [0, 0, 0])

    @case(SERVER, "pipe", "stages given as one string are split into stages")
    def test_terminal__pipe_string_stages(self):
        out = self.srv.call("pipe", {"stages": "seq 1 6\nwc -l"}).data
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["output"].strip(), "6")
        self.assertEqual(len(out["stages"]), 2)

    @case(SERVER, "pipe", "a failing middle stage is named, not hidden")
    def test_terminal__pipe_failure(self):
        out = self.srv.call("pipe", {"stages": ["seq 1 2", "grep zzz", "wc -l"]}).data
        self.assertEqual(out["failed_stage"], "grep zzz")
        self.assertEqual(len(out["stages"]), 2)
        self.assertEqual(out["stages"][1]["exit_code"], 1)
        self.assertEqual(out["exit_code"], 1)

    # ---- process ----------------------------------------------------------
    @case(SERVER, "process", "start, wait, and read the log")
    def test_terminal__process_lifecycle(self):
        started = self.srv.call("process", {"action": "start", "name": "smoke",
                                           "command": "printf four"}).data
        self.assertIn("pid", started)
        waited = self.srv.call("process", {"action": "wait", "name": "smoke"}, timeout=30).data
        self.assertEqual(waited["exit_code"], 0)
        self.assertFalse(waited["running"])
        log = self.srv.call("process", {"action": "log", "name": "smoke"}).data
        self.assertIn("four", log["lines"])
        self.assertIn("log", log)

    @case(SERVER, "process", "list shows running jobs")
    def test_terminal__process_list(self):
        self.srv.call("process", {"action": "start", "name": "long", "command": "sleep 30"})
        jobs = self.srv.call("process", {"action": "list"}).data
        names = [j["name"] for j in jobs["jobs"]]
        self.assertIn("long", names)
        self.srv.call("process", {"action": "kill", "name": "long"})

    @case(SERVER, "process", "a job that is still running is not waited out")
    def test_terminal__process_wait_returns_while_running(self):
        self.srv.call("process", {"action": "start", "name": "slow", "command": "sleep 5"})
        out = self.srv.call("process", {"action": "wait", "name": "slow", "timeout_s": 0.5},
                           timeout=20).data
        self.assertTrue(out["running"])
        self.srv.call("process", {"action": "kill", "name": "slow"})

    # ---- repl -------------------------------------------------------------
    @case(SERVER, "repl", "a python session keeps its state between calls")
    def test_terminal__repl_python_state(self):
        self.srv.call("repl", {"session": "python", "action": "start",
                               "code": "class G:\n    def hi(self): return 'hi'"})
        out = self.srv.call("repl", {"session": "python", "code": "print(G().hi())"}).data
        self.assertEqual(out["output"].strip(), "hi")
        self.srv.call("repl", {"session": "python", "action": "close"})

    @case(SERVER, "repl", "a bash session keeps its state between calls")
    def test_terminal__repl_bash_state(self):
        self.srv.call("repl", {"session": "bash", "action": "start", "code": "count=3"})
        out = self.srv.call("repl", {"session": "bash", "code": "echo $((count * 2))"}).data
        self.assertEqual(out["output"].strip(), "6")
        self.srv.call("repl", {"session": "bash", "action": "close"})

    @case(SERVER, "repl", "status reports whether a session is open")
    def test_terminal__repl_status(self):
        self.srv.call("repl", {"session": "python", "action": "start", "code": "x = 1"})
        self.assertTrue(self.srv.call("repl", {"session": "python", "action": "status"}).data["running"])
        self.srv.call("repl", {"session": "python", "action": "close"})
        self.assertFalse(self.srv.call("repl", {"session": "python", "action": "status"}).data["running"])

    @case(SERVER, "repl", "an unknown session type is refused")
    def test_terminal__repl_bad_session(self):
        r = self.srv.call("repl", {"session": "tcsh", "code": "echo hi"})
        self.assertFalse(r.ok)

    # ---- symbols ----------------------------------------------------------
    @case(SERVER, "symbols", "reads a file with ast and reports classes and methods")
    def test_terminal__symbols_classes(self):
        out = self.srv.call("symbols", {"path": "fx/notes.py"}).data
        self.assertEqual(out["files"], 1)
        names = {s["name"] for f in out["detail"] for s in f["symbols"]}
        self.assertIn("Ledger", names)
        self.assertIn("load", names)

    @case(SERVER, "symbols", "a directory with no python files says so")
    def test_terminal__symbols_no_python(self):
        write(self.root, "fx/nopy/notes.txt", "no python here")
        out = self.srv.call("symbols", {"path": "fx/nopy"}).data
        self.assertEqual(out["files"], 0)
        self.assertIn("no .py", out["note"])

    # ---- check -----------------------------------------------------------
    @case(SERVER, "check", "a relative path is read under cwd, not the root")
    def test_terminal__check_relative_path(self):
        out = self.srv.call("check", {"path": "fx/bad.py", "cwd": str(self.root),
                                      "tests": False}, timeout=120).data
        self.assertEqual(out["path"], str(self.root / "fx/bad.py"))

    @case(SERVER, "check", "installed tools run and missing ones are named")
    def test_terminal__check_report(self):
        out = self.srv.call("check", {"path": "fx/bad.py", "cwd": str(self.root),
                                      "tests": False}, timeout=120).data
        self.assertTrue(set(out["report"]) <= {"ruff", "mypy", "pylint"})
        for item in out["report"].values():
            self.assertIn("available", item)
            if item["available"]:
                self.assertIsInstance(item["exit_code"], int)
        self.assertEqual(out["missing_tools"], [n for n, i in out["report"].items() if not i["available"]])

    @case(SERVER, "check", "linting a file with an unused import reports it")
    def test_terminal__check_finds_lint_error(self):
        out = self.srv.call("check", {"path": "fx/bad.py", "cwd": str(self.root),
                                      "tests": False}, timeout=120).data
        self.assertEqual(out["report"]["ruff"]["exit_code"], 1)
        self.assertIn("F401", out["report"]["ruff"]["result"])

    @case(SERVER, "check", "pytest runs a test file")
    def test_terminal__check_tests(self):
        out = self.srv.call("check", {"path": "fx/test_ok.py", "cwd": str(self.root),
                                      "tests": True}, timeout=180).data
        self.assertEqual(out["report"]["pytest"]["exit_code"], 0)
        self.assertIn("1 passed", out["report"]["pytest"]["result"])

    # ---- install -----------------------------------------------------------
    @case(SERVER, "install", "an empty package list is refused")
    def test_terminal__install_empty(self):
        self.assertFalse(self.srv.call("install", {"packages": []}).ok)

    @case(SERVER, "install", "pip failures are reported, not raised")
    def test_terminal__install_unresolvable(self):
        out = self.srv.call("install", {"packages": ["hypercore-no-such-package-xyz"]},
                           timeout=180).data
        self.assertNotEqual(out["exit_code"], 0)
        self.assertTrue(out["output"].strip())

    # ---- build / compile / tidy ------------------------------------------
    @case(SERVER, "build", "cmake configures and builds a project")
    def test_terminal__build(self):
        require("cmake")
        out = self.srv.call("build", {"path": "native", "configure": True}, timeout=180).data
        self.assertEqual(out["exit_code"], 0)
        self.assertTrue(any(a.endswith("smoke") for a in out["artifacts"]))

    @case(SERVER, "build", "a second build reuses the existing build directory")
    def test_terminal__build_reuses_dir(self):
        require("cmake")
        out = self.srv.call("build", {"path": "native"}, timeout=180).data
        self.assertEqual(out["exit_code"], 0)

    @case(SERVER, "compile", "a valid translation unit compiles")
    def test_terminal__compile_ok(self):
        require("g++")
        out = self.srv.call("compile", {"path": "native/main.cpp", "flags": "-std=c++17 -Wall",
                                        "syntax_only": False}, timeout=120).data
        self.assertTrue(out["compiled"])
        self.assertTrue((self.root / "native/main.o").is_file())

    @case(SERVER, "compile", "a broken translation unit fails cleanly")
    def test_terminal__compile_bad(self):
        require("g++")
        out = self.srv.call("compile", {"path": "native/broken.cpp"}).data
        self.assertFalse(out["compiled"])
        self.assertIn("error", out["output"])

    @case(SERVER, "tidy", "clang-tidy reports warning and error counts")
    def test_terminal__tidy(self):
        require("clang-tidy")
        out = self.srv.call("tidy", {"path": "native/main.cpp",
                                     "build_dir": "native/build"}, timeout=180).data
        self.assertIsInstance(out["warnings"], int)
        self.assertIsInstance(out["errors"], int)

    # ---- debug -------------------------------------------------------------
    @case(SERVER, "debug", "runs a binary under gdb")
    def test_terminal__debug_run(self):
        require("gdb")
        require("g++")
        self.srv.call("build", {"path": "native"}, timeout=180)
        out = self.srv.call("debug", {"action": "run", "path": "native/build/smoke"},
                           timeout=120).data
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("sum=6", out["output"])

    @case(SERVER, "debug", "valgrind reports leaks")
    def test_terminal__debug_leak(self):
        require("valgrind")
        require("g++")
        self.srv.call("build", {"path": "native"}, timeout=180)
        out = self.srv.call("debug", {"action": "leak", "path": "native/build/smoke"},
                           timeout=120).data
        self.assertIn("total heap usage", out["leaks"])

    @case(SERVER, "debug", "symbols lists the entry points of a binary")
    def test_terminal__debug_symbols(self):
        self.srv.call("build", {"path": "native"}, timeout=180)
        out = self.srv.call("debug", {"action": "symbols", "path": "native/build/smoke"}).data
        self.assertGreater(out["total"], 0)

    @case(SERVER, "debug", "strace and time trace a command")
    def test_terminal__debug_trace(self):
        require("strace")
        out = self.srv.call("debug", {"action": "trace", "command": "printf traced",
                                      "tracer": "strace"}, timeout=60).data
        self.assertIn("traced", out["output"])

    # ---- query -------------------------------------------------------------
    @case(SERVER, "query", "no expression gives the shape of the document")
    def test_terminal__query_json_shape(self):
        out = self.srv.call("query", {"path": "fx/records.json"}).data
        self.assertEqual(out["shape"]["type"], "object")
        self.assertIn("terminal", out["shape"]["keys"])

    @case(SERVER, "query", "reads a value out of JSON")
    def test_terminal__query_json(self):
        out = self.srv.call("query", {"path": "fx/records.json", "expression": ".terminal.timeout_s"}).data
        self.assertEqual(out["result"], "90\n")

    @case(SERVER, "query", "reads a value out of YAML")
    def test_terminal__query_yaml(self):
        require("yq")
        out = self.srv.call("query", {"path": "fx/config.yaml", "expression": ".terminal.timeout_s"}).data
        self.assertEqual(out["result"], "30\n")

    @case(SERVER, "query", "reads a key path out of TOML")
    def test_terminal__query_toml(self):
        out = self.srv.call("query", {"path": "fx/settings.toml", "expression": "engine.max_context"}).data
        self.assertEqual(out["result"], {"type": "int", "value": 8192})

    @case(SERVER, "query", "runs a read-only SELECT")
    def test_terminal__query_sqlite(self):
        out = self.srv.call("query", {"path": "fx/index.sqlite",
                                     "expression": "select name, calls from tools order by calls desc"}).data
        rows = json.loads(out["rows"])
        self.assertEqual(rows[0]["name"], "run")

    @case(SERVER, "query", "the limit argument bounds the rows when the statement has none")
    def test_terminal__query_sqlite_limit(self):
        out = self.srv.call("query", {"path": "fx/index.sqlite", "expression": "select name from tools",
                                      "limit": 2}).data
        self.assertEqual(out["row_limit"], 2)
        self.assertEqual(len(json.loads(out["rows"])), 2)
        # a statement that already bounds itself is left alone
        out = self.srv.call("query", {"path": "fx/index.sqlite",
                                      "expression": "select name from tools limit 1"}).data
        self.assertNotIn("row_limit", out)
        self.assertEqual(len(json.loads(out["rows"])), 1)

    # ---- compare -----------------------------------------------------------
    @case(SERVER, "compare", "diffs two files and counts the lines")
    def test_terminal__compare_files(self):
        out = self.srv.call("compare", {"path": "fx/a.txt", "other": "fx/b.txt"}).data
        self.assertFalse(out["identical"])
        self.assertEqual(out["added_lines"], 1)
        self.assertEqual(out["removed_lines"], 0)

    @case(SERVER, "compare", "the same file compared to itself is identical")
    def test_terminal__compare_identical(self):
        out = self.srv.call("compare", {"path": "fx/a.txt", "other": "fx/a.txt"}).data
        self.assertTrue(out["identical"])

    @case(SERVER, "compare", "git_ref compares against the committed version")
    def test_terminal__compare_git(self):
        require("git")
        out = self.srv.call("compare", {"path": "fx/a.txt", "git_ref": "HEAD"}).data
        self.assertTrue(out["identical"])
        write(self.root, "fx/a.txt", "line one\nreplaced\n")
        out = self.srv.call("compare", {"path": "fx/a.txt", "git_ref": "HEAD"}).data
        self.assertFalse(out["identical"])
        self.assertEqual(out["added_lines"], 1)
        self.assertEqual(out["removed_lines"], 1)

    @case(SERVER, "compare", "a revision that does not exist is an error")
    def test_terminal__compare_bad_ref(self):
        require("git")
        r = self.srv.call("compare", {"path": "fx/a.txt", "git_ref": "no-such-revision"})
        self.assertFalse(r.ok)

    # ---- status -------------------------------------------------------------
    @case(SERVER, "status", "reports the root, the limits, and what is installed")
    def test_terminal__status(self):
        out = self.srv.call("status").data
        self.assertEqual(out["root"], str(self.root))
        self.assertGreater(out["allowed_binaries"], 0)
        self.assertTrue(out["interpreters"])
        self.assertIsInstance(out["missing_tools"], list)
        self.assertIn("git", out["tier2"])
