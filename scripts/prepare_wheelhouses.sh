#!/usr/bin/env bash
# Run on the SAME Linux architecture/libc and Python minor as the offline host.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$#" -ne 1 ]; then
  echo "Usage: PYTHON_BIN=python3.10 bash scripts/prepare_wheelhouses.sh /path/to/dependency-cache" >&2
  exit 2
fi
python="${PYTHON_BIN:-python3.10}"
cache="$("$python" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).absolute())' "$1")"
"$python" -c 'import sys; assert sys.version_info[:2] in ((3,10),(3,11)), "Use Python 3.10/3.11 matching the target"'
"$python" -c 'import ssl, sqlite3, bz2, lzma, zlib, venv'
"$python" "$root/scripts/reproduction.py" verify --directory "$cache"
work_dir="$(mktemp -d /tmp/keeninsight-wheel-build.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT
"$python" -m venv "$work_dir/venv"
build_python="$work_dir/venv/bin/python"
"$build_python" -m pip install --upgrade pip wheel setuptools
for group in sysinsight dream dev; do
  case "$group" in
    sysinsight) requirement="$root/requirements-sysinsight.txt" ;;
    dream) requirement="$root/requirements-dream.lock.txt" ;;
    dev) requirement="$root/requirements-dev.txt" ;;
  esac
  wheel_directory="$cache/wheelhouses/$group"
  if [ -e "$wheel_directory" ]; then
    echo "Refusing existing wheel directory: $wheel_directory; inspect or move it first" >&2
    exit 1
  fi
  mkdir -p "$wheel_directory"
  # Include a modern offline pip; old OS pip may not support --dry-run.
  "$build_python" -m pip wheel --wheel-dir "$wheel_directory" 'pip==24.3.1'
  options=()
  if [ "$group" = dream ]; then
    options+=(--extra-index-url https://download.pytorch.org/whl/cpu)
  fi
  "$build_python" -m pip wheel "${options[@]}" --wheel-dir "$wheel_directory" -r "$requirement"
  "$build_python" -m pip install --dry-run --ignore-installed --no-index \
    --only-binary=:all: --find-links "$wheel_directory" -r "$requirement"
done
"$python" - "$cache/wheelhouses/platform.json" <<'PY'
import json, platform, sys, sysconfig
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "python": platform.python_version(), "machine": platform.machine(),
    "system": platform.system(), "libc": platform.libc_ver(),
    "platform": sysconfig.get_platform(),
    "note": "Build provenance, not a claim of cross-platform compatibility"
}, indent=2) + "\n")
PY
"$python" "$root/scripts/reproduction.py" seal-wheels --directory "$cache"
echo "Wheelhouses built and resolved locally. No API keys or installed virtualenvs copied."
