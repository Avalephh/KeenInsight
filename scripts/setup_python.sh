#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VENV="${PYTHON_VENV:-$PROJECT_ROOT/.venv}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if [ ! -x "$PYTHON_VENV/bin/python" ]; then
  python3 -m venv "$PYTHON_VENV"
fi

"$PYTHON_VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements-sysinsight.txt"
if [ "${INSTALL_PPT_DEPENDENCIES:-0}" = "1" ]; then
  "$PYTHON_VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements-ppt.txt"
fi

echo "Python environment ready: $PYTHON_VENV"
echo "Use $PYTHON_VENV/bin/python for the demo"
