#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON_BIN:-python3}"
exec "$python" "$root/app/db_profiles/build_profile_artifacts.py" "$@"
