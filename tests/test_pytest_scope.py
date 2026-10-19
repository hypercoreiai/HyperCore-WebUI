"""tests/test_pytest_scope.py - a plain pytest run from the repo root reaches this suite.

Loop 41 leaves the Strata clone in tmp/ rather than removing it. A bare `pytest` from the
repo root walks into that clone and collects its own test files, which import
tools/_paths.py; that module calls sys.exit() when llama.cpp's gguf-py is not on the box,
so the run ended with a collection error before it reached anything of this project's.
pytest.ini scopes collection to tests/. This file keeps two things true: that scope, and
that the suite itself has no gguf-py dependency, so the scope is not hiding one.

Run with:

    python3 -m pytest tests/test_pytest_scope.py -q
"""
from __future__ import annotations

import configparser
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO / "tests"
PYTEST_INI = REPO / "pytest.ini"

COLLECT_TIMEOUT_S = 120            # collection only; a real run of the suite is minutes
GGUF_MARKERS = ("import gguf", "gguf_py", "STRATA_GGUF_PY")


class PytestScopeTests(unittest.TestCase):
    """The collection scope, read as pytest reads it."""

    def _ini(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read(PYTEST_INI)
        return cfg

    def test_collection_is_scoped_to_the_suite(self) -> None:
        """testpaths names tests/ and the clone folder left in tmp/ is excluded."""
        cfg = self._ini()
        self.assertTrue(cfg.has_section("pytest"), "pytest.ini has no [pytest] section")
        self.assertEqual(cfg["pytest"].get("testpaths", "").split(), ["tests"],
                         "pytest.ini does not scope collection to tests/")
        excluded = cfg["pytest"].get("norecursedirs", "").split()
        self.assertIn("tmp", excluded,
                      "the clone loop 41 keeps in tmp/ is not excluded from collection")

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
        outside = [line for line in collected if not line.startswith("tests/")]
        self.assertEqual(outside, [], f"collected outside tests/: {outside[:5]}")

    def test_the_suite_has_no_gguf_py_dependency(self) -> None:
        """The scope is not hiding an import that would need llama.cpp on the box."""
        offenders: list[str] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            if path == Path(__file__).resolve():
                continue    # this file names the markers it is looking for
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(marker in text for marker in GGUF_MARKERS):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], f"tests import gguf-py: {offenders}")


if __name__ == "__main__":
    unittest.main()
