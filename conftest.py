"""conftest.py - make this project importable when pytest is run as plain `pytest`.

`python3 -m pytest` puts the directory it is run from on sys.path; the `pytest` script
does not. Every module in tests/ imports `serve`, so without this a run from the repo
root stops at collection with ModuleNotFoundError: No module named 'serve'.

pytest loads the conftest of its rootdir before it imports any test module, so this
file is what makes both spellings of the command work:

    pytest tests -q
    python3 -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
