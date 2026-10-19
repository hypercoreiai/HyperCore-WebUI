"""Run the browser-free JavaScript checks as part of `pytest`.

The 18 files in tests/*.js read serve/web/app.js and serve/web/index.html as text and assert on
the behaviour of the parts that change their own labels: the Stop/Start pair, the two-click Shut
Down, the surface slider, the token-prefix folding. Before this wrapper they only ran when
someone remembered to loop over them by hand, so a UI regression could pass a routine `pytest`
run untouched.

Node is not a dependency of the project, so a machine without it skips the suite rather than
failing it. The skip is reported, never silent.

    python -m pytest tests/test_js_suites.py -v
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_TESTS = sorted((ROOT / "tests").glob("*.js"))
NODE = shutil.which("node")
TIMEOUT_S = 120


@pytest.mark.skipif(NODE is None, reason="node is not installed; the UI checks need it")
@pytest.mark.parametrize("js_path", JS_TESTS, ids=lambda p: p.name)
def test_js_suite(js_path: Path) -> None:
    proc = subprocess.run(
        [NODE, str(js_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()
        raise AssertionError(f"{js_path.name} failed (exit {proc.returncode}):\n{detail}")
