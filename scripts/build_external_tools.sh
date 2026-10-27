#!/usr/bin/env bash
# Compile only. No apt, downloads, sudo, make install or database restart.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:---help}" in
  tpcds)
    # SQL queries already come from DREAM; only data generator/index are used.
    # Avoid building unused dsqgen (and its yacc/flex toolchain).
    make -C "$root/third_party/tpcds-kit/tools" OS=LINUX dsdgen tpcds.idx
    ;;
  pg-hint-plan)
    pg_config="${PG_CONFIG:-/usr/lib/postgresql/12/bin/pg_config}"
    if ! "$pg_config" --version | grep -q '^PostgreSQL 12[.]'; then
      echo "This pinned extension is for PostgreSQL 12; choose matching PG_CONFIG" >&2
      exit 1
    fi
    make -C "$root/third_party/pg_hint_plan" USE_PGXS=1 PG_CONFIG="$pg_config"
    echo "Compiled only. Review docs/REPRODUCING.md before explicit make install/preload/restart."
    ;;
  *)
    echo "Usage: bash scripts/build_external_tools.sh {tpcds|pg-hint-plan}"
    ;;
esac
