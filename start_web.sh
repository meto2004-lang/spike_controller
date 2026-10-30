#!/usr/bin/env sh
# Start the browser app (Pybricks Code style IDE) on http://127.0.0.1:8000
set -eu
cd "$(dirname "$0")"

PYTHON_BIN=python3
if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11 or newer is required (found: $("$PYTHON_BIN" -V 2>&1))." >&2
    exit 1
fi

exec "$PYTHON_BIN" spike_server.py "$@"
