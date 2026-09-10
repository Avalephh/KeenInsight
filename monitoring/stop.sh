#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for name in grafana postgres_exporter node_exporter prometheus; do
  pid_file="$BASE_DIR/run/$name.pid"
  if [ -f "$pid_file" ]; then
    pid="$(<"$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "stopped $name (pid $pid)"
    else
      echo "$name is not running"
    fi
  fi
done
