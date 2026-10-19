"""tests/test_pytest_scope.py - a plain pytest run from the repo root reaches this suite.

Loop 41 leaves the Strata clone in tmp/ rather than removing it. A bare `pytest` from the
repo root walks into that clone and collects its own test files, which import
tools/_paths.py; that module calls sys.exit() when llama.cpp's gguf-py is not on the box,
so the run ended with a collection error before it reached anything of this project's.
pytest.ini scopes collection to tests/ and to serve/mcp/tests/, the MCP hub's own contract
suite. It does not reach the vendored serve/mcp/web-scrape-mcp/ server, which has its own
pyproject and its own pytest configuration. This file keeps two things true: that scope, and
that the suite itself has no gguf-py dependency, so the scope is not hiding one.

Run with:

    python3 -m pytest tests/test_pytest_scope.py -q
"""
from __future__ import annotations

import configparser
import importlib.util
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO / "tests"
PYTEST_INI = REPO / "pytest.ini"
CONFTEST = REPO / "conftest.py"

COLLECT_TIMEOUT_S = 120            # collection only; a real run of the suite is minutes
GGUF_MARKERS = ("import gguf", "gguf_py", "STRATA_GGUF_PY")
SUITE_PREFIXES = ("tests", "serve/mcp/tests")     # what a bare `pytest` is allowed to collect


class PytestScopeTests(unittest.TestCase):
    """The collection scope, read as pytest reads it."""

    def _ini(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read(PYTEST_INI)
        return cfg

    def test_collection_is_scoped_to_the_suite(self) -> None:
        """testpaths names tests/ and the MCP contract suite, and the clone in tmp/ is excluded."""
        cfg = self._ini()
        self.assertTrue(cfg.has_section("pytest"), "pytest.ini has no [pytest] section")
        self.assertEqual(cfg["pytest"].get("testpaths", "").split(), list(SUITE_PREFIXES),
                         "pytest.ini does not scope collection to tests/ and serve/mcp/tests/")
        excluded = cfg["pytest"].get("norecursedirs", "").split()
        self.assertIn("tmp", excluded,
                      "the clone loop 41 keeps in tmp/ is not excluded from collection")
        self.assertIn(".venv", excluded, "the virtualenv is not excluded from collection")

    def test_the_ini_itself_carries_the_import_path(self) -> None:
        """pytest.ini names the repo root, so the scope does not depend on conftest loading."""
        cfg = self._ini()
        entries = cfg["pytest"].get("pythonpath", "").split()
        self.assertIn(".", entries,
                      "pytest.ini does not put the repo root on the import path")
        for entry in entries:
            root = (REPO / entry).resolve() if not entry.startswith("/") else Path(entry)
            self.assertTrue(root.is_dir(), f"pythonpath entry {entry} is not a directory")
        self.assertTrue((REPO / "serve" / "__init__.py").is_file(),
                        "the import path does not reach the serve package")

    def test_a_bare_pytest_run_collects_only_tests(self) -> None:
        """Run collection the way someone at the repo root would, and read what it took."""
        if importlib.util.find_spec("pytest") is None:
            self.skipTest("pytest is not installed")
        proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                              cwd=str(REPO), capture_output=True, text=True,
                              timeout=COLLECT_TIMEOUT_S, check=False)
        out = (proc.stdout + proc.stderr).strip()
        self.assertEqual(proc.returncode, 0, f"collection failed:\n{out[-800:]}")
        collected = [line for line in out.splitlines() if "::" in line]
        self.assertTrue(collected, "nothing was collected")
        outside = [line for line in collected if not line.startswith(SUITE_PREFIXES)]
        self.assertEqual(outside, [], f"collected outside the suite: {outside[:5]}")

    def test_the_suite_has_no_gguf_py_dependency(self) -> None:
        """The scope is not hiding an import that would need llama.cpp on the box."""
        offenders: list[str] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            if path == Path(__file__).resolve():
                continue    # this file names the markers it is looking for
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(marker in text for marker in GGUF_MARKERS):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], f"tests import gguf-py: {offenders[:5]}")

    def test_the_repo_root_is_on_sys_path_for_a_bare_pytest_run(self) -> None:
        """`pytest tests` and `python3 -m pytest tests` have to collect the same files.

        The pytest script does not add the directory it is run from to sys.path the way
        `python3 -m pytest` does, and every module here imports `serve`. Without the
        root conftest the run stops at collection with ModuleNotFoundError: No module
        named 'serve' before a single test runs.
        """
        self.assertTrue(CONFTEST.is_file(), "no conftest.py at the repo root to set sys.path")
        if importlib.util.find_spec("pytest") is None:
            self.skipTest("pytest is not installed")
        script = shutil.which("pytest")
        if script is None:
            self.skipTest("the pytest script is not on PATH")
        proc = subprocess.run([script, "--collect-only", "-q"], cwd=str(REPO),
                              capture_output=True, text=True, timeout=COLLECT_TIMEOUT_S,
                              check=False)
        self._assert_collects_serve(proc, "a bare pytest run at the repo root")

    def test_a_bare_pytest_run_from_inside_tests_still_imports_serve(self) -> None:
        """Someone inside tests/ gets the same result as someone at the root.

        The root conftest is only read when the rootdir resolves to the repo, so this is
        the case pythonpath in pytest.ini has to cover.
        """
        script = shutil.which("pytest")
        if script is None:
            self.skipTest("the pytest script is not on PATH")
        proc = subprocess.run([script, "--collect-only", "-q", "test_pytest_scope.py"],
                              cwd=str(TESTS_DIR), capture_output=True, text=True,
                              timeout=COLLECT_TIMEOUT_S, check=False)
        self._assert_collects_serve(proc, "a bare pytest run from inside tests/")

    def _assert_collects_serve(self, proc: subprocess.CompletedProcess[str],
                              where: str) -> None:
        out = (proc.stdout + proc.stderr).strip()
        self.assertEqual(proc.returncode, 0, f"{where} could not collect:\n{out[-800:]}")
        self.assertNotIn("ModuleNotFoundError", out,
                        f"{where} cannot import serve")
        collected = [line for line in out.splitlines() if "::" in line]
        self.assertTrue(collected, f"{where} collected nothing")
        outside = [line for line in collected
                   if not line.startswith(SUITE_PREFIXES) and not line.startswith("test_")]
        self.assertEqual(outside, [], f"collected outside the suite: {outside[:5]}")


if __name__ == "__main__":
    unittest.main()
