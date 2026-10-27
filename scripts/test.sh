#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
export PYTHONDONTWRITEBYTECODE=1
python="${PYTHON_BIN:-python3}"
"$python" scripts/release_check.py
"$python" -m unittest discover -s tests -p 'test_*.py' -v
for file in scripts/*.sh monitoring/*.sh; do bash -n "$file"; done
if command -v node >/dev/null 2>&1; then
  for file in app/keeninsight_web/*.js tests/*.cjs; do node --check "$file"; done
fi
