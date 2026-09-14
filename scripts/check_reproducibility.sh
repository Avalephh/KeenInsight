#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_EXTERNAL=0
CHECK_RUNTIME=0

for argument in "$@"; do
  case "$argument" in
    --external) CHECK_EXTERNAL=1 ;;
    --runtime) CHECK_RUNTIME=1 ;;
    --strict) CHECK_EXTERNAL=1; CHECK_RUNTIME=1 ;;
    -h|--help)
      echo "usage: $0 [--external] [--runtime] [--strict]"
      exit 0
      ;;
    *)
      echo "unknown argument: $argument" >&2
      exit 2
      ;;
  esac
done

required_files=(
  README.md
  EXTERNAL_SOURCES.md
  external-sources.lock
  postgresql-source.lock
  requirements-sysinsight.txt
  monitoring/config/prometheus/prometheus.yml
  monitoring/config/grafana/grafana.ini
  monitoring/dashboards/local-postgresql-demo.json
  perf-anomaly-demo/db_profile.py
  perf-anomaly-demo/sysinsight_detection.py
  perf-anomaly-demo/sysinsight_original_llm.py
  perf-anomaly-demo/sysinsight_prometheus.py
  perf-anomaly-demo/sysinsight_candidate_benchmark.py
  perf-anomaly-demo/sysinsight_pipeline.py
  perf-anomaly-demo/sysinsight_auto.py
  perf-anomaly-demo/sysinsight_dream_bridge.py
  perf-anomaly-demo/dream_live_adapter.py
  perf-anomaly-demo/tpcc_api_recommendation_validation.py
)
for relative_path in "${required_files[@]}"; do
  if [ ! -e "$PROJECT_ROOT/$relative_path" ]; then
    echo "missing tracked file: $relative_path" >&2
    exit 1
  fi
done

mapfile -t python_files < <(git -C "$PROJECT_ROOT" ls-files '*.py')
if [ "${#python_files[@]}" -gt 0 ]; then
  python3 -m py_compile "${python_files[@]/#/$PROJECT_ROOT/}"
fi
for shell_file in "$PROJECT_ROOT"/monitoring/*.sh "$PROJECT_ROOT"/scripts/*.sh; do
  bash -n "$shell_file"
done

PROJECT_ROOT="$PROJECT_ROOT" python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["PROJECT_ROOT"])
sys.path.insert(0, str(root / "perf-anomaly-demo"))
from db_profile import resolve_profile  # noqa: E402

for dbms, version in [("postgresql", "12"), ("postgresql", "13"), ("postgresql", "14")]:
    profile = resolve_profile(dbms, version)
    for key in profile.raw.get("files", {}):
        profile.path(key)
    print(f"profile {profile.name}: tracked files OK")

for relative_path in [
    "monitoring/config/prometheus/prometheus.yml",
    "monitoring/config/grafana/provisioning/dashboards/dashboards.yml",
    "monitoring/config/grafana/provisioning/datasources/prometheus.yml",
    "monitoring/dashboards/local-postgresql-demo.json",
]:
    path = root / relative_path
    if path.suffix == ".json":
        json.loads(path.read_text(encoding="utf-8"))
    print(f"structured file {relative_path}: OK")
PY

if [ "$CHECK_EXTERNAL" -eq 1 ]; then
  source_root="${SYSINSIGHT_REPOSITORY_ROOT:-$PROJECT_ROOT/repositories/Avalephh-KeenInsight/branch-sources}"
  while read -r branch commit; do
    [ -z "${branch:-}" ] && continue
    case "$branch" in \#*) continue ;; esac
    path="$source_root/$branch"
    if [ ! -d "$path/.git" ]; then
      echo "missing Git source: $path (run scripts/fetch_keeninsight_sources.sh)" >&2
      exit 1
    fi
    actual="$(git -C "$path" rev-parse HEAD)"
    [ "$actual" = "$commit" ] || { echo "$path is $actual, expected $commit" >&2; exit 1; }
    echo "source $branch: $actual"
  done < "$PROJECT_ROOT/external-sources.lock"

  pg_root="${POSTGRES_SOURCE_ROOT:-$PROJECT_ROOT/third_party/postgresql-12.22}"
  if [ ! -f "$pg_root/.gitrevision" ]; then
    echo "missing PostgreSQL source: $pg_root (run scripts/fetch_postgresql_source.sh)" >&2
    exit 1
  fi
  pg_expected="$(awk '$1 == "postgresql" {print $3; exit}' "$PROJECT_ROOT/postgresql-source.lock")"
  if [ "$(tr -d '[:space:]' < "$pg_root/.gitrevision")" != "$pg_expected" ] \
    || [ ! -f "$pg_root/src/backend/utils/misc/guc.c" ]; then
    echo "PostgreSQL .gitrevision is missing or does not match $pg_expected" >&2
    exit 1
  fi
  echo "source postgresql: $pg_expected"

  PROJECT_ROOT="$PROJECT_ROOT" POSTGRES_SOURCE_ROOT="$pg_root" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["PROJECT_ROOT"])
source_root = Path(os.environ["POSTGRES_SOURCE_ROOT"])
files = sorted((source_root / "src").rglob("*.c"))
digest = hashlib.sha256()
for path in files:
    digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    digest.update(b"\n")
provenance = json.loads(
    (root / "perf-anomaly-demo/db_profiles/postgresql/common/function_parameter_association.provenance.json")
    .read_text(encoding="utf-8")
)
if digest.hexdigest() != provenance["source_digest"]:
    raise SystemExit("PostgreSQL source digest does not match the checked-in profile artifact")
print(f"source postgresql digest: {digest.hexdigest()}")
PY

  SYSINSIGHT_REPOSITORY_ROOT="$source_root" \
  SYSINSIGHT_SOURCE_ROOT="$source_root/WorkloadTune" \
  python3 - "$PROJECT_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "perf-anomaly-demo"))
from db_profile import resolve_profile  # noqa: E402

profile = resolve_profile("mysql", "8.0.36")
for key in profile.raw.get("files", {}):
    profile.path(key)
print("profile mysql/8.0.36: external files OK")
PY
fi

if [ "$CHECK_RUNTIME" -eq 1 ]; then
  for command_name in psql pgbench perf curl perl; do
    command -v "$command_name" >/dev/null 2>&1 || {
      echo "missing runtime command: $command_name" >&2
      exit 1
    }
  done
  bin_dir="${MONITORING_BIN_DIR:-$PROJECT_ROOT/monitoring/vendor/usr/bin}"
  grafana_home="${GRAFANA_HOME:-$PROJECT_ROOT/monitoring/grafana-13.2.1}"
  for binary in prometheus promtool prometheus-node-exporter prometheus-postgres-exporter; do
    [ -x "$bin_dir/$binary" ] || {
      echo "missing monitoring binary: $bin_dir/$binary" >&2
      exit 1
    }
  done
  [ -x "$grafana_home/bin/grafana" ] || {
    echo "missing Grafana binary: $grafana_home/bin/grafana" >&2
    exit 1
  }
  for versioned_binary in \
    "$bin_dir/prometheus 2.15.2" \
    "$bin_dir/prometheus-node-exporter 0.18.1" \
    "$bin_dir/prometheus-postgres-exporter 0.8.0" \
    "$grafana_home/bin/grafana 13.2.1"; do
    binary="${versioned_binary% *}"
    expected="${versioned_binary##* }"
    version_output="$("$binary" --version 2>&1 || true)"
    case "$version_output" in
      *"$expected"*) ;;
      *)
        echo "unexpected version for $binary; expected $expected" >&2
        exit 1
        ;;
    esac
  done
  echo "runtime binaries and commands: OK"
fi

echo "reproducibility check passed"
