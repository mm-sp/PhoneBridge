#!/usr/bin/env bash
# One-command test run: builds a throw-away venv (system GTK bindings visible) from linux/requirements.txt, then runs
# every test. Skips count as failures (PHONEBRIDGE_STRICT=1) so missing prerequisites are never silent.
#   tools/run_tests.sh            # strict
#   PHONEBRIDGE_STRICT=0 tools/run_tests.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PHONEBRIDGE_TEST_VENV:-$ROOT/.venv-test}"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install --quiet -r "$ROOT/linux/requirements.txt"
command -v xvfb-run >/dev/null || echo "WARNING: xvfb-run not found; GUI tests need a display" >&2
cd "$ROOT/linux"
PHONEBRIDGE_STRICT="${PHONEBRIDGE_STRICT:-1}" PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" tests/run_all.py "$VENV/bin/python"
