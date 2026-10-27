#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VENV="${PYTHON_VENV:-$PROJECT_ROOT/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python is required: $PYTHON_BIN" >&2
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; assert (3,8) <= sys.version_info[:2] <= (3,11), "Pinned SysInsight dependencies require Python 3.8–3.11; set PYTHON_BIN"'
"$PYTHON_BIN" -c 'import ssl, sqlite3, bz2, lzma, zlib, venv; print("Required Python standard-library modules available")'

if [ ! -x "$PYTHON_VENV/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$PYTHON_VENV"
fi

if [ -n "${KEENINSIGHT_WHEELHOUSE_ROOT:-}" ]; then
  export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
  wheel_directory="$KEENINSIGHT_WHEELHOUSE_ROOT/sysinsight"
  "$PYTHON_VENV/bin/python" -m pip install --no-index --find-links "$wheel_directory" 'pip==24.3.1'
  "$PYTHON_VENV/bin/python" -m pip install --no-index --only-binary=:all: \
    --find-links "$wheel_directory" -r "$PROJECT_ROOT/requirements-sysinsight.txt"
else
  "$PYTHON_VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements-sysinsight.txt"
fi

echo "Python environment ready: $PYTHON_VENV"
echo "Use $PYTHON_VENV/bin/python for the demo"
