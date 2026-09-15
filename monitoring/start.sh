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
MONITORING_DASHBOARDS_DIR="${MONITORING_DASHBOARDS_DIR:-$BASE_DIR/dashboards}"
POSTGRES_QUERY_CONFIG="${POSTGRES_EXPORTER_QUERY_CONFIG:-$BASE_DIR/config/postgres_exporter/queries.yaml}"
POSTGRES_EXPORTER_DATA_SOURCE_NAME="${POSTGRES_EXPORTER_DATA_SOURCE_NAME:-postgresql://postgres@/keeninsight?host=/var/run/postgresql&sslmode=disable}"
BRIDGE_SCRIPT="${SYSINSIGHT_BRIDGE_SCRIPT:-$BASE_DIR/../perf-anomaly-demo/sysinsight_dream_bridge.py}"
BRIDGE_PYTHON="${SYSINSIGHT_BRIDGE_PYTHON:-python3}"
BRIDGE_STATE_DB="${SYSINSIGHT_BRIDGE_STATE_DB:-$BASE_DIR/data/sysinsight_dream_bridge.sqlite3}"
BRIDGE_OUTPUT="${SYSINSIGHT_BRIDGE_OUTPUT:-$BASE_DIR/data/sysinsight_dream_bridge}"
BRIDGE_HTTP_LISTEN="${SYSINSIGHT_BRIDGE_LISTEN:-127.0.0.1}"
BRIDGE_HTTP_PORT="${SYSINSIGHT_BRIDGE_PORT:-9108}"
BRIDGE_DB="${SYSINSIGHT_DB:-keeninsight}"
BRIDGE_DB_USER="${SYSINSIGHT_DB_USER:-postgres}"
BRIDGE_DB_HOST="${SYSINSIGHT_DB_HOST:-/var/run/postgresql}"
BRIDGE_DB_PORT="${SYSINSIGHT_DB_PORT:-5432}"
BRIDGE_DB_SCHEMA="${SYSINSIGHT_DB_SCHEMA:-tpcds}"
BRIDGE_ALERT_NAME="${SYSINSIGHT_ALERT_NAME:-SysInsightDemoAnomaly}"
BRIDGE_DREAM_CONFIG="${SYSINSIGHT_DREAM_CONFIG:-$BASE_DIR/../dream/config/tpcds_local_config.json}"
BRIDGE_DREAM_RUN_AS="${SYSINSIGHT_DREAM_RUN_AS:-postgres}"

# A peer-authenticated PostgreSQL worker cannot traverse /root. Previous
# reproducibility runs create a readable runtime view under /tmp; use the
# newest one automatically unless the operator explicitly selects a root.
BRIDGE_DREAM_RUNTIME_ROOT="${SYSINSIGHT_DREAM_RUNTIME_ROOT:-}"
if [ -z "$BRIDGE_DREAM_RUNTIME_ROOT" ]; then
  for candidate in /tmp/dream-runtime-*; do
    if [ -d "$candidate/dream" ] && [ -r "$candidate/dream" ]; then
      BRIDGE_DREAM_RUNTIME_ROOT="$candidate"
    fi
  done
fi
if [ -z "$BRIDGE_DREAM_RUNTIME_ROOT" ]; then
  BRIDGE_DREAM_RUNTIME_ROOT="$BASE_DIR/../dream"
fi

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
if [ "${SYSINSIGHT_BRIDGE_ENABLED:-1}" != "0" ] && [ ! -f "$BRIDGE_SCRIPT" ]; then
  echo "missing SysInsight/DREAM bridge: $BRIDGE_SCRIPT" >&2
  exit 1
fi

# /root is intentionally not traversable by the postgres OS account. A private
# runtime copy makes only this executable and query file reachable from /tmp
# for the local peer-authenticated exporter process. Copying instead of using
# a hard link also works when the checkout and /tmp are different filesystems.
POSTGRES_EXPORTER_RUNTIME_BIN="/tmp/new-monitoring-postgres-exporter"
POSTGRES_QUERY_RUNTIME="/tmp/new-monitoring-postgres-queries.yaml"
install -m 0755 "$POSTGRES_EXPORTER_BIN" "$POSTGRES_EXPORTER_RUNTIME_BIN"
install -m 0644 "$POSTGRES_QUERY_CONFIG" "$POSTGRES_QUERY_RUNTIME"

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
  DATA_SOURCE_NAME="$POSTGRES_EXPORTER_DATA_SOURCE_NAME" \
  "$POSTGRES_EXPORTER_RUNTIME_BIN" \
  --extend.query-path="$POSTGRES_QUERY_RUNTIME" \
  --web.listen-address=127.0.0.1:9187

MONITORING_DASHBOARDS_DIR="$MONITORING_DASHBOARDS_DIR" \
GF_PATHS_DATA="$BASE_DIR/data/grafana" \
GF_PATHS_LOGS="$BASE_DIR/logs" \
GF_PATHS_PLUGINS="$BASE_DIR/data/grafana/plugins" \
GF_PATHS_PROVISIONING="$BASE_DIR/config/grafana/provisioning" \
start_process grafana \
  "$GRAFANA_BIN" \
  server \
  --config="$GRAFANA_CONFIG" \
  --homepath="$GRAFANA_HOME"

if [ "${SYSINSIGHT_BRIDGE_ENABLED:-1}" != "0" ]; then
  start_process sysinsight_dream_bridge \
    "$BRIDGE_PYTHON" \
    "$BRIDGE_SCRIPT" \
    --db "$BRIDGE_DB" \
    --db-user "$BRIDGE_DB_USER" \
    --host "$BRIDGE_DB_HOST" \
    --port "$BRIDGE_DB_PORT" \
    --db-schema "$BRIDGE_DB_SCHEMA" \
    --alert-name "$BRIDGE_ALERT_NAME" \
    --dream-config "$BRIDGE_DREAM_CONFIG" \
    --dream-runtime-root "$BRIDGE_DREAM_RUNTIME_ROOT" \
    --dream-run-as "$BRIDGE_DREAM_RUN_AS" \
    --state-db "$BRIDGE_STATE_DB" \
    --output "$BRIDGE_OUTPUT" \
    --http-listen "$BRIDGE_HTTP_LISTEN" \
    --http-port "$BRIDGE_HTTP_PORT" \
    --tune-without-alert
fi

# Apply a changed scrape configuration when Prometheus was already running.
# New Prometheus processes read it at startup; the lifecycle endpoint makes
# repeated `start.sh` calls converge without restarting the time-series store.
if curl -fsS --max-time 3 -X POST http://127.0.0.1:9090/-/reload >/dev/null 2>&1; then
  echo "reloaded Prometheus configuration"
else
  echo "Prometheus configuration reload unavailable; startup config remains active" >&2
fi
