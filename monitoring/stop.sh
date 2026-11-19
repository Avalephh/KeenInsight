#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for name in sysinsight_dream_bridge grafana postgres_exporter node_exporter prometheus; do
  pid_file="$BASE_DIR/run/$name.pid"
  python3 "$BASE_DIR/process_control.py" stop "$pid_file"
done
