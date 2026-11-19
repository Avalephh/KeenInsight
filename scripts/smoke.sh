#!/usr/bin/env bash
set -euo pipefail

base_url="${KI_BASE_URL:-http://127.0.0.1:8000}"

python3 - "$base_url" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1].rstrip("/")
for path in ("/api/health", "/api/meta", "/api/workloads", "/api/settings", "/api/export?limit=1"):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        body = json.load(response)
    if path == "/api/health" and not body.get("ok"):
        raise SystemExit("PostgreSQL health check failed: " + str(body.get("error")))
    print(path, json.dumps(body, ensure_ascii=False)[:500])
PY
