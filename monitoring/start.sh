#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${MONITORING_BIN_DIR:-$BASE_DIR/vendor/usr/bin}"
PROMETHEUS_BIN="${PROMETHEUS_BIN:-$BIN_DIR/prometheus}"
NODE_EXPORTER_BIN="${NODE_EXPORTER_BIN:-$BIN_DIR/prometheus-node-exporter}"
POSTGRES_EXPORTER_BIN="${POSTGRES_EXPORTER_BIN:-$BIN_DIR/prometheus-postgres-exporter}"
GRAFANA_HOME="${GRAFANA_HOME:-$BASE_DIR/grafana-13.2.1}"
GRAFANA_BIN="${GRAFANA_BIN:-$GRAFANA_HOME/bin/grafana}"
PROMETHEUS_CONFIG="$BASE_DIR/config/prometheus/prometheus.yml"
GRAFANA_CONFIG="$BASE_DIR/config/grafana/grafana.ini"

mkdir -p "$BASE_DIR/run" "$BASE_DIR/logs" "$BASE_DIR/data/prometheus" "$BASE_DIR/data/grafana"

start_process() {
  local name="$1"
  local pid_file="$BASE_DIR/run/$name.pid"
  shift

  if [ -f "$pid_file" ] && kill -0 "$(<"$pid_file")" 2>/dev/null; then
    echo "$name already running (pid $(<"$pid_file"))"
    return 0
  fi

  nohup setsid "$@" >>"$BASE_DIR/logs/$name.log" 2>&1 </dev/null &
  echo $! >"$pid_file"
  echo "started $name (pid $!)"
}

if [ ! -x "$PROMETHEUS_BIN" ]; then
  echo "missing Prometheus binary: $PROMETHEUS_BIN" >&2
  exit 1
fi
if [ ! -x "$NODE_EXPORTER_BIN" ]; then
  echo "missing node_exporter binary: $NODE_EXPORTER_BIN" >&2
  exit 1
fi
if [ ! -x "$POSTGRES_EXPORTER_BIN" ]; then
  echo "missing postgres_exporter binary: $POSTGRES_EXPORTER_BIN" >&2
  exit 1
fi
if [ ! -x "$GRAFANA_BIN" ]; then
  echo "missing Grafana binary: $GRAFANA_BIN" >&2
  exit 1
fi

# /root is intentionally not traversable by the postgres OS account. A hard
# link keeps the same binary while making only this executable reachable from
# /tmp for the local peer-authenticated exporter process.
POSTGRES_EXPORTER_RUNTIME_BIN="/tmp/new-monitoring-postgres-exporter"
if [ ! -e "$POSTGRES_EXPORTER_RUNTIME_BIN" ]; then
  ln "$POSTGRES_EXPORTER_BIN" "$POSTGRES_EXPORTER_RUNTIME_BIN"
  chmod 755 "$POSTGRES_EXPORTER_RUNTIME_BIN"
fi
POSTGRES_QUERY_CONFIG="$BASE_DIR/config/postgres_exporter/queries.yaml"
POSTGRES_QUERY_RUNTIME="/tmp/new-monitoring-postgres-queries.yaml"
if [ ! -e "$POSTGRES_QUERY_RUNTIME" ]; then
  ln "$POSTGRES_QUERY_CONFIG" "$POSTGRES_QUERY_RUNTIME"
fi

start_process prometheus \
  "$PROMETHEUS_BIN" \
  --config.file="$PROMETHEUS_CONFIG" \
  --storage.tsdb.path="$BASE_DIR/data/prometheus" \
  --storage.tsdb.retention.time=24h \
  --web.listen-address=127.0.0.1:9090 \
  --web.enable-lifecycle

start_process node_exporter \
  "$NODE_EXPORTER_BIN" \
  --web.listen-address=127.0.0.1:9100

start_process postgres_exporter \
  runuser -u postgres -- env \
  DATA_SOURCE_NAME='postgresql://postgres@/keeninsight?host=/var/run/postgresql&sslmode=disable' \
  "$POSTGRES_EXPORTER_RUNTIME_BIN" \
  --extend.query-path="$POSTGRES_QUERY_RUNTIME" \
  --web.listen-address=127.0.0.1:9187

MONITORING_DASHBOARDS_DIR="$BASE_DIR/dashboards" \
GF_PATHS_DATA="$BASE_DIR/data/grafana" \
GF_PATHS_LOGS="$BASE_DIR/logs" \
GF_PATHS_PLUGINS="$BASE_DIR/data/grafana/plugins" \
GF_PATHS_PROVISIONING="$BASE_DIR/config/grafana/provisioning" \
start_process grafana \
  "$GRAFANA_BIN" \
  server \
  --config="$GRAFANA_CONFIG" \
  --homepath="$GRAFANA_HOME"
