#!/usr/bin/env bash
set -u

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for name in prometheus node_exporter postgres_exporter grafana; do
  pid_file="$BASE_DIR/run/$name.pid"
  if [ -f "$pid_file" ] && kill -0 "$(<"$pid_file")" 2>/dev/null; then
    echo "$name: running (pid $(<"$pid_file"))"
  else
    echo "$name: stopped"
  fi
done

for endpoint in \
  http://127.0.0.1:9090/-/ready \
  http://127.0.0.1:9100/metrics \
  http://127.0.0.1:9187/metrics \
  http://127.0.0.1:3000/api/health; do
  if curl -fsS --max-time 3 "$endpoint" >/dev/null 2>&1; then
    echo "$endpoint: OK"
  else
    echo "$endpoint: NOT READY"
  fi
done
