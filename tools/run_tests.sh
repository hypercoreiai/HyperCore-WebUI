#!/bin/sh
# Run everything that can be run without a GPU.
#
#   ./tools/run_tests.sh
#
# Two suites: the Python tests (pytest) and the browser-free JS tests, which read
# serve/web/app.js and index.html as text and check the behaviour of the parts that
# change their own labels - the Stop/Start pair, the two-click Shut Down, the
# surface slider, the token-prefix folding. Node is only needed for the second.
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

fail=0

echo "== python =="
if "$PY" -m pytest tests/ -q; then
  echo "python: ok"
else
  echo "python: FAILED"
  fail=1
fi

if command -v node >/dev/null 2>&1; then
  echo "== javascript =="
  for f in tests/*.js; do
    if node "$f"; then
      echo "ok  $f"
    else
      echo "FAILED $f"
      fail=1
    fi
  done
else
  echo "node is not installed; the JS checks were skipped"
fi

exit $fail
