#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DREAM_PYTHON_BIN="${DREAM_PYTHON_BIN:-python3.10}"
DREAM_SOURCE_VENV="${DREAM_SOURCE_VENV:-$PROJECT_ROOT/dream/.venv}"
"$DREAM_PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] in ((3,10),(3,11)), "Use Python 3.10 or 3.11 for the pinned DREAM CPU environment"'
if [ ! -x "$DREAM_SOURCE_VENV/bin/python" ]; then
  "$DREAM_PYTHON_BIN" -m venv "$DREAM_SOURCE_VENV"
fi
"$DREAM_SOURCE_VENV/bin/python" -m pip install \
  --index-url https://download.pytorch.org/whl/cpu 'torch==2.4.1+cpu'
"$DREAM_SOURCE_VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements-dream.lock.txt"
echo "DREAM Python prepared; genuine models/data and a configured database are still required."
