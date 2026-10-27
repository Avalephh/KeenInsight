#!/usr/bin/env bash
# One portable entry point. Does not initialize, import or reset a database.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
deployment_env="${SYSINSIGHT_ENV_FILE:-$PROJECT_ROOT/monitoring/.env.local}"
if [ -f "$deployment_env" ]; then
  set -a
  # This is an operator-owned local shell configuration, never an uploaded file.
  # shellcheck disable=SC1090
  . "$deployment_env"
  set +a
fi
case "${1:---help}" in
  -h|--help)
    echo "Usage: bash scripts/deploy.sh {install|doctor|prepare|enable|start|restart|stop|status}"
    echo "Explicit dependency setup: install-components | install-python | install-dream-python"
    echo "New machines require existing PostgreSQL/data, upstream sources and real DREAM assets."
    echo "See docs/DEPLOYMENT.md. No database is automatically created or overwritten."
    exit 0
    ;;
esac
default_deploy_python=python3
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  default_deploy_python="$PROJECT_ROOT/.venv/bin/python"
fi
exec "${KEENINSIGHT_DEPLOY_PYTHON:-$default_deploy_python}" "$PROJECT_ROOT/monitoring/deployment.py" "$@"
