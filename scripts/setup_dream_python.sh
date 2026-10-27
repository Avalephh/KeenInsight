#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DREAM_PYTHON_BIN="${DREAM_PYTHON_BIN:-python3.10}"
DREAM_SOURCE_VENV="${DREAM_SOURCE_VENV:-$PROJECT_ROOT/dream/.venv}"
"$DREAM_PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] in ((3,10),(3,11)), "Use Python 3.10 or 3.11 for the pinned DREAM CPU environment"'
"$DREAM_PYTHON_BIN" -c 'import ssl, sqlite3, bz2, lzma, zlib, venv; print("Required Python standard-library modules available")'
if [ ! -x "$DREAM_SOURCE_VENV/bin/python" ]; then
  "$DREAM_PYTHON_BIN" -m venv "$DREAM_SOURCE_VENV"
fi
if [ -n "${KEENINSIGHT_WHEELHOUSE_ROOT:-}" ]; then
  export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
  wheel_directory="$KEENINSIGHT_WHEELHOUSE_ROOT/dream"
  "$DREAM_SOURCE_VENV/bin/python" -m pip install --no-index --find-links "$wheel_directory" 'pip==24.3.1'
  "$DREAM_SOURCE_VENV/bin/python" -m pip install --no-index --only-binary=:all: \
    --find-links "$wheel_directory" -r "$PROJECT_ROOT/requirements-dream.lock.txt"
else
  "$DREAM_SOURCE_VENV/bin/python" -m pip install \
    --index-url https://download.pytorch.org/whl/cpu 'torch==2.4.1+cpu'
  "$DREAM_SOURCE_VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements-dream.lock.txt"
fi
echo "DREAM Python prepared; genuine models/data and a configured database are still required."
