#!/usr/bin/env python3
"""Deploy and supervise the existing demo; never initialize or replace a DB."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MON = ROOT / "monitoring"
UNIT = "keeninsight-demo.service"  # Do not replace the separate port-8000 service.
COMPONENTS = ["prometheus", "node_exporter", "postgres_exporter", "grafana"]


def components():
    return COMPONENTS + (["sysinsight_dream_bridge"] if os.environ.get("SYSINSIGHT_BRIDGE_ENABLED", "1") != "0" else [])


def endpoints():
    port = int(os.environ.get("SYSINSIGHT_BRIDGE_PORT", "9108"))
    result = {"prometheus": "http://127.0.0.1:9090/-/ready",
              "node_exporter": "http://127.0.0.1:9100/metrics",
              "postgres_exporter": "http://127.0.0.1:9187/metrics",
              "grafana": "http://127.0.0.1:3000/api/health"}
    if "sysinsight_dream_bridge" in components():
        result["sysinsight_dream_bridge"] = "http://127.0.0.1:{}/healthz".format(port)
    return result


def checks():
    from process_control import matches
    results = {}
    for name, url in endpoints().items():
        item = {"healthy": False, "url": url}
        try:
            value = matches(MON / "run" / (name + ".pid"))
            if not value:
                raise RuntimeError("No matching process birth identity")
            with urlopen(url, timeout=3) as response:
                if name == "postgres_exporter":
                    # Per-statement metrics can exceed 2 MiB and pg_up is
                    # emitted near the end. A truncated body used to make a
                    # healthy database look down and restart the whole UI.
                    scanned = 0
                    while True:
                        line = response.readline(65536)
                        if not line:
                            raise RuntimeError("Exporter response has no pg_up metric")
                        scanned += len(line)
                        if scanned > 32 * 1024 * 1024:
                            raise RuntimeError("Exporter health scan exceeded 32 MiB")
                        if re.match(rb"^pg_up(?:\{[^}]*\})?\s+", line):
                            if not re.fullmatch(rb"pg_up(?:\{[^}]*\})?\s+1(?:\.0)?\s*", line):
                                raise RuntimeError("Exporter cannot reach PostgreSQL (pg_up != 1)")
                            break
                    item["healthy"] = True
                    results[name] = item
                    continue
                body = response.read(2 * 1024 * 1024).decode()
                if name == "sysinsight_dream_bridge" and json.loads(body).get("pid") != value["pid"]:
                    raise RuntimeError("HTTP process is not the recorded bridge")
                if name == "grafana" and json.loads(body).get("database") != "ok":
                    raise RuntimeError("Grafana database is not healthy")
            item["healthy"] = True
        except Exception as exc:
            item["error"] = str(exc)
        results[name] = item
    return results


def health(wait=0):
    deadline = time.monotonic() + wait
    while True:
        result = checks()
        if all(r["healthy"] for r in result.values()) or time.monotonic() >= deadline:
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return all(r["healthy"] for r in result.values())
        time.sleep(1)


def run_script(name):
    subprocess.run(["bash", str(MON / name)], check=True, cwd=str(ROOT))


def doctor():
    """Read-only preflight. Missing assets are errors, never smoke replacements."""
    errors = []
    required = ["curl", "perl", "perf", "psql", "pgbench", "runuser"]
    import shutil
    for command in required:
        if not shutil.which(command):
            errors.append("Missing system command: " + command)
    for item in (ROOT / "dream/dream/agent/db_agent.py",
                 Path(os.environ.get("DREAM_SOURCE_VENV", str(ROOT / "dream/.venv"))) / "bin/python",
                 Path(os.environ.get("DREAM_SOURCE_CONFIG", str(ROOT / "config/dream.example.json")))):
        if not item.exists():
            errors.append("Missing external source/runtime asset: " + str(item))
    source = Path(os.environ.get("SYSINSIGHT_SOURCE_ROOT", str(
        ROOT / "third_party/sysinsight/WorkloadTune")))
    if not (source / "DBTuner/utils/analyzeException.py").is_file():
        errors.append("Missing SysInsight source: " + str(source))
    for item in (ROOT / "app/db_profiles/postgresql/common/constraints.json",
                 ROOT / "app/vendor/FlameGraph/stackcollapse-perf.pl"):
        if not item.is_file():
            errors.append("Missing generated/external asset: {}; see docs/DEPLOYMENT.md".format(item))
    pg_source = os.environ.get("POSTGRES_SOURCE_ROOT")
    pg_candidates = [Path(pg_source)] if pg_source else [ROOT / "third_party/postgresql-12.22"]
    if not any((p / "src/backend").is_dir() for p in pg_candidates):
        errors.append("Missing PostgreSQL source; set POSTGRES_SOURCE_ROOT or run scripts/fetch_postgresql_source.sh")
    bridge_python = os.environ.get("SYSINSIGHT_BRIDGE_PYTHON") or (
        str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable)
    dependency_code = """import sys
sys.path.insert(0,sys.argv[1])
import numpy,pandas,scipy,sklearn,shap,joblib,openai,yaml,langchain,ConfigSpace
print('SysInsight dependency imports passed')
"""
    try:
        check = subprocess.run([bridge_python, "-c", dependency_code,
            os.environ.get("SYSINSIGHT_PYDEPS", str(ROOT / "app/.pydeps"))],
            text=True, capture_output=True, timeout=30,
            env=dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1"))
        if check.returncode:
            errors.append("SysInsight Python dependencies are not importable; run install-python or set SYSINSIGHT_BRIDGE_PYTHON/PYDEPS")
    except (OSError, subprocess.SubprocessError):
        errors.append("Cannot execute the configured SysInsight Python dependency check")
    cfg = os.environ.get("SYSINSIGHT_CODEX_CONFIG_DIR")
    if cfg:
        try:
            from codex_api_launcher import load_settings
            settings = load_settings(cfg)
            print("Shared API credentials readable; model=" + settings["model"])
        except Exception as exc:
            errors.append("Shared API configuration is unreadable: " + type(exc).__name__)
    elif not any(os.environ.get(k) for k in ("SYSINSIGHT_GPT_API_KEY", "SYSINSIGHT_API_KEY", "OPENAI_API_KEY")):
        errors.append("No shared API key or SYSINSIGHT_CODEX_CONFIG_DIR configured")
    else:
        endpoint = (os.environ.get("SYSINSIGHT_GPT_BASE_URL") or os.environ.get("SYSINSIGHT_API_BASE")
                    or os.environ.get("OPENAI_BASE_URL") or "")
        model = (os.environ.get("SYSINSIGHT_GPT_MODEL") or os.environ.get("SYSINSIGHT_API_MODEL")
                 or os.environ.get("OPENAI_MODEL") or "")
        if not endpoint or "example.com" in endpoint:
            errors.append("Configure an actual LLM endpoint, not the template endpoint")
        if not model or model == "your-model-name":
            errors.append("Configure an actual provider model, not the template model")
    if not os.environ.get("GF_SECURITY_ADMIN_PASSWORD"):
        errors.append("Set a private GF_SECURITY_ADMIN_PASSWORD; no default is distributed")
    if not errors:
        schema = os.environ.get("SYSINSIGHT_DB_SCHEMA", "tpcds").replace("'", "''")
        sql = """BEGIN READ ONLY;
        SELECT json_build_object('version',current_setting('server_version_num')::int,
          'pg_stat_statements',EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'),
          'pg_hint_plan',EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_hint_plan'),
          'ap_data',to_regclass('{}' || '.store_sales') IS NOT NULL,
          'tp_data',to_regclass('keeninsight_tpcc.warehouse') IS NOT NULL);
        COMMIT;""".format(schema)
        command = ["psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1",
                   "-h", os.environ.get("SYSINSIGHT_DB_HOST", "/var/run/postgresql"),
                   "-p", os.environ.get("SYSINSIGHT_DB_PORT", "5432"),
                   "-U", os.environ.get("SYSINSIGHT_DB_USER", "postgres"),
                   "-d", os.environ.get("SYSINSIGHT_DB", "keeninsight")]
        if os.geteuid() == 0:
            command = ["runuser", "-u", os.environ.get("SYSINSIGHT_RUN_AS", "postgres"), "--"] + command
        try:
            result = subprocess.run(command, input=sql, capture_output=True, text=True, timeout=15, cwd="/")
            if result.returncode:
                raise RuntimeError("PostgreSQL connection/read-only query failed")
            database = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
            print("Database preflight: " + json.dumps(database))
            for key in ("pg_stat_statements", "pg_hint_plan", "ap_data", "tp_data"):
                if not database[key]:
                    errors.append("Database prerequisite missing: " + key + " (not auto-created/imported)")
            if not 120000 <= database["version"] < 130000:
                errors.append("This demonstration/profile is validated on PG12; another version needs explicit adaptation")
        except Exception as exc:
            errors.append("Database preflight failed: " + str(exc))
    for error in errors:
        print("ERROR: " + error)
    print("Preflight: " + ("FAILED" if errors else "passed (database/model execution is checked separately)"))
    return not errors


def unit_text(root, python):
    def quoted(value):
        value = str(value)
        if "\n" in value or "\r" in value:
            raise ValueError("Newlines are not permitted in deployment paths")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
    pg = os.environ.get("SYSINSIGHT_PG_SERVICE", "postgresql@{}-{}.service".format(
        os.environ.get("SYSINSIGHT_DB_VERSION", "12"), os.environ.get("SYSINSIGHT_PG_CLUSTER", "main")))
    if not re.fullmatch(r"[A-Za-z0-9@_.-]+\.service", pg):
        raise ValueError("Invalid PostgreSQL systemd unit")
    return """[Unit]
Description=KeenInsight SysInsight DREAM and Grafana demo supervisor
Wants=network-online.target
After=network-online.target {pg}
Requires={pg}
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Type=simple
ExecStart=/usr/bin/bash {entry} supervise
Environment={python_env}
Restart=on-failure
RestartSec=15
TimeoutStopSec=100
KillMode=control-group
UMask=0022

[Install]
WantedBy=multi-user.target
""".format(pg=pg, entry=quoted(Path(root) / "scripts/deploy.sh"),
           python_env=quoted("KEENINSIGHT_DEPLOY_PYTHON=" + str(python)))


def install_service():
    if os.geteuid() != 0:
        raise ValueError("Installing systemd units requires root")
    if not doctor():
        raise ValueError("Preflight failed; prepare the reported assets before enabling")
    path = Path("/etc/systemd/system") / UNIT
    text = unit_text(ROOT, sys.executable)
    if path.exists() and "KeenInsight SysInsight DREAM and Grafana demo supervisor" not in path.read_text():
        raise ValueError("Refusing to overwrite an unrelated service: " + str(path))
    path.write_text(text)
    path.chmod(0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", UNIT], check=True)
    print("Enabled " + UNIT + "; use deploy.sh start to start it", flush=True)


def supervise():
    """Foreground supervisor, so systemd notices child death and restarts stack."""
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    failed = False
    try:
        run_script("start.sh")
        misses = 0
        while not stop.wait(10):
            result = checks()
            misses = 0 if all(r["healthy"] for r in result.values()) else misses + 1
            if misses >= 3:
                print("Unhealthy child; restart required: " + json.dumps(result), flush=True)
                failed = True
                break
    finally:
        run_script("stop.sh")
    return 1 if failed else 0


def systemd_installed():
    return (Path("/etc/systemd/system") / UNIT).is_file() and Path("/run/systemd/system").is_dir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "start", "stop", "restart", "status", "health", "doctor", "prepare", "enable", "supervise", "install-components", "install-python", "install-dream-python"])
    parser.add_argument("--wait", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.action == "health":
            return 0 if health(args.wait) else 1
        if args.action == "doctor":
            return 0 if doctor() else 1
        if args.action == "prepare":
            subprocess.run([sys.executable, str(MON / "prepare_runtime.py")], check=True)
        elif args.action == "install":
            if not doctor():
                raise ValueError("Supply prerequisites first; see docs/DEPLOYMENT.md")
            bindir = Path(os.environ.get("MONITORING_BIN_DIR", str(MON / "vendor/usr/bin")))
            binaries = [bindir / name for name in
                        ("prometheus", "prometheus-node-exporter", "prometheus-postgres-exporter")]
            grafana = Path(os.environ.get("GRAFANA_HOME", str(MON / "grafana-13.2.1"))) / "bin/grafana"
            if not all(p.is_file() for p in binaries) or not grafana.is_file():
                import platform
                if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
                    raise ValueError("Locked monitoring packages require Linux x86_64")
                run_script("install_open_source.sh")
            subprocess.run([sys.executable, str(MON / "prepare_runtime.py")], check=True)
            install_service()
            subprocess.run(["systemctl", "start", UNIT], check=True)
            return 0 if health(120) else 1
        elif args.action == "enable":
            install_service()
        elif args.action == "supervise":
            return supervise()
        elif args.action == "install-components":
            run_script("install_open_source.sh")
        elif args.action == "install-python":
            subprocess.run(["bash", str(ROOT / "scripts/setup_python.sh")], check=True)
        elif args.action == "install-dream-python":
            subprocess.run(["bash", str(ROOT / "scripts/setup_dream_python.sh")], check=True)
        elif args.action == "status":
            if systemd_installed():
                # Do not dump child argv: a configured exporter DSN can embed
                # a database password. Health output is enough for this CLI.
                subprocess.run(["systemctl", "show", UNIT, "-p", "ActiveState",
                                "-p", "SubState", "-p", "UnitFileState", "-p", "NRestarts"])
            return 0 if health() else 1
        elif systemd_installed():
            subprocess.run(["systemctl", args.action, UNIT], check=True)
            if args.action != "stop":
                return 0 if health(max(args.wait, 120)) else 1
        else:
            if args.action in ("stop", "restart"):
                run_script("stop.sh")
            if args.action in ("start", "restart"):
                run_script("start.sh")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Deployment failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
