#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="${MONITORING_VERSION_LOCK:-$BASE_DIR/versions.lock}"
BIN_DIR="${MONITORING_BIN_DIR:-$BASE_DIR/vendor/usr/bin}"
GRAFANA_HOME="${GRAFANA_HOME:-$BASE_DIR/grafana-13.2.1}"
DOWNLOAD_DIR="${MONITORING_DOWNLOAD_DIR:-$BASE_DIR/downloads}"

case "${1:-}" in
  "") ;;
  -h|--help)
    echo "usage: $0"
    echo "downloads the locked native Prometheus/Grafana components and verifies SHA-256"
    exit 0
    ;;
  *)
    echo "usage: $0" >&2
    exit 2
    ;;
esac

for command_name in curl sha256sum tar install; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required" >&2
    exit 1
  }
done
if [ ! -f "$LOCK_FILE" ]; then
  echo "monitoring lock file not found: $LOCK_FILE" >&2
  exit 1
fi

mkdir -p "$BIN_DIR" "$DOWNLOAD_DIR"
work_dir="$(mktemp -d /tmp/keeninsight-monitoring.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT

lock_value() {
  local component="$1"
  awk -F '\t' -v component="$component" \
    '$1 == component {print $2 "\t" $3 "\t" $4 "\t" $5; exit}' "$LOCK_FILE"
}

download_component() {
  local component="$1"
  local version archive url sha
  IFS=$'\t' read -r version archive url sha < <(lock_value "$component")
  if [ -z "${archive:-}" ] || [ -z "${url:-}" ] || [ -z "${sha:-}" ]; then
    echo "no valid lock entry for $component" >&2
    exit 1
  fi
  local destination="$DOWNLOAD_DIR/$archive"
  if [ ! -f "$destination" ] \
    || ! printf '%s  %s\n' "$sha" "$destination" | sha256sum -c - >/dev/null 2>&1; then
    echo "downloading $component $version" >&2
    curl -fL --retry 3 --connect-timeout 15 "$url" -o "$destination"
  fi
  printf '%s  %s\n' "$sha" "$destination" | sha256sum -c - >/dev/null
  printf '%s\n' "$destination"
}

prometheus_archive="$(download_component prometheus)"
node_archive="$(download_component node_exporter)"
postgres_archive="$(download_component postgres_exporter)"
grafana_archive="$(download_component grafana)"

tar -xzf "$prometheus_archive" -C "$work_dir"
tar -xzf "$node_archive" -C "$work_dir"
tar -xzf "$postgres_archive" -C "$work_dir"
install -m 0755 "$work_dir/prometheus-2.15.2.linux-amd64/prometheus" "$BIN_DIR/prometheus"
install -m 0755 "$work_dir/prometheus-2.15.2.linux-amd64/promtool" "$BIN_DIR/promtool"
install -m 0755 "$work_dir/node_exporter-0.18.1.linux-amd64/node_exporter" "$BIN_DIR/prometheus-node-exporter"
install -m 0755 "$work_dir/postgres_exporter_v0.8.0_linux-amd64/postgres_exporter" "$BIN_DIR/prometheus-postgres-exporter"

if [ ! -x "$GRAFANA_HOME/bin/grafana" ]; then
  tar -xzf "$grafana_archive" -C "$work_dir"
  extracted="$work_dir/grafana-13.2.1"
  if [ -e "$GRAFANA_HOME" ]; then
    echo "Grafana home exists but is incomplete: $GRAFANA_HOME" >&2
    exit 1
  fi
  mkdir -p "$(dirname -- "$GRAFANA_HOME")"
  mv "$extracted" "$GRAFANA_HOME"
fi

if [ ! -x "$GRAFANA_HOME/bin/grafana" ]; then
  echo "Grafana binary not found after installation: $GRAFANA_HOME/bin/grafana" >&2
  exit 1
fi

verify_version() {
  local binary="$1"
  local expected="$2"
  local output
  output="$("$binary" --version 2>&1 || true)"
  case "$output" in
    *"$expected"*) ;;
    *)
      echo "unexpected version for $binary; expected $expected, got: $output" >&2
      exit 1
      ;;
  esac
}

verify_version "$BIN_DIR/prometheus" "2.15.2"
verify_version "$BIN_DIR/prometheus-node-exporter" "0.18.1"
verify_version "$BIN_DIR/prometheus-postgres-exporter" "0.8.0"
verify_version "$GRAFANA_HOME/bin/grafana" "13.2.1"

echo "Prometheus and exporters installed under $BIN_DIR"
echo "Grafana installed at $GRAFANA_HOME"
