#!/usr/bin/env python3
"""
KeenInsight 前端监控服务
======================
- GET  /                → 监控仪表盘页面
- GET  /api/stream      → SSE 实时指标流 (TPS / Latency / 缓存命中率)
- POST /api/start       → 触发完整演示流程
- GET  /api/status      → 当前状态 JSON
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request, send_from_directory

# ── 路径 ──────────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import (
    BENCHBASE_DIR, BENCHBASE_JAR,
    HISTORY_PERF_CHBENCH, PERFORMANCE_DIR,
)

app = Flask(__name__, static_folder=os.path.join(_HERE, "web"))

# ─────────────────────────────────────────────────────────────────────────────
# 全局状态
# ─────────────────────────────────────────────────────────────────────────────

DBNAME = "benchbase"

DEFAULT_KNOBS = {
    "shared_buffers": "32MB", "work_mem": "1MB",
    "maintenance_work_mem": "16MB", "effective_cache_size": "512MB",
    "max_wal_size": "256MB", "checkpoint_completion_target": "0.5",
    "random_page_cost": "4", "effective_io_concurrency": "1",
}

TUNED_KNOBS = {
    "shared_buffers": "2GB", "work_mem": "64MB",
    "maintenance_work_mem": "512MB", "effective_cache_size": "20GB",
    "max_wal_size": "4GB", "checkpoint_completion_target": "0.9",
    "random_page_cost": "1.1", "effective_io_concurrency": "200",
}

@dataclass
class AppState:
    phase: str = "idle"        # idle | resetting | loading | diagnosing | tuning | verifying | done
    message: str = "就绪，等待用户触发负载"
    progress: int = 0          # 0-100
    metrics: List[Dict] = field(default_factory=list)   # 时间序列
    diagnosis: List[str] = field(default_factory=list)
    knob_changes: List[Dict] = field(default_factory=list)
    before_summary: Dict = field(default_factory=dict)
    after_summary: Dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "phase": self.phase, "message": self.message,
                "progress": self.progress,
                "metrics": self.metrics[-300:],  # 最新 300 点
                "diagnosis": self.diagnosis,
                "knob_changes": self.knob_changes,
                "before": self.before_summary,
                "after": self.after_summary,
            }

state = AppState()

# ─────────────────────────────────────────────────────────────────────────────
# 数据库工具
# ─────────────────────────────────────────────────────────────────────────────

def _psql(sql: str) -> str:
    r = subprocess.run(
        ["su", "-", "postgres", "-c", f'psql -d {DBNAME} -t -c "{sql}"'],
        capture_output=True, text=True)
    return r.stdout.strip()


def _pg_metrics() -> Dict[str, float]:
    """从 pg_stat_database 获取实时指标。"""
    try:
        row = _psql(
            "SELECT xact_commit, xact_rollback, blks_read, blks_hit, "
            "tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted, "
            "temp_bytes, deadlocks "
            f"FROM pg_stat_database WHERE datname='{DBNAME}';"
        )
        parts = [x.strip() for x in row.split("|")]
        if len(parts) < 11:
            return {}
        commits = int(parts[0])
        rollbacks = int(parts[1])
        blks_read = int(parts[2])
        blks_hit = int(parts[3])
        total_blks = blks_read + blks_hit
        return {
            "xact_commit": commits,
            "xact_rollback": rollbacks,
            "blks_read": blks_read,
            "blks_hit": blks_hit,
            "cache_hit_ratio": (blks_hit / total_blks * 100) if total_blks > 0 else 100.0,
            "tup_returned": int(parts[4]),
            "temp_bytes": int(parts[9]),
            "deadlocks": int(parts[10]),
        }
    except Exception:
        return {}


def _apply_knobs(knobs: Dict[str, str], label: str):
    for k, v in knobs.items():
        _psql(f"ALTER SYSTEM SET {k} = '{v}';")
    subprocess.run(["pg_ctlcluster", "12", "main", "restart"],
                   capture_output=True, text=True)
    time.sleep(3)

# ─────────────────────────────────────────────────────────────────────────────
# BenchBase 运行
# ─────────────────────────────────────────────────────────────────────────────

def _write_chbench_cfg(duration: int, terminals: int) -> str:
    cfg = f"""<?xml version="1.0"?>
<parameters>
    <type>POSTGRES</type><driver>org.postgresql.Driver</driver>
    <url>jdbc:postgresql://localhost:5432/{DBNAME}?sslmode=disable&amp;reWriteBatchedInserts=true</url>
    <username>admin</username><password>password</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <isolation>TRANSACTION_READ_COMMITTED</isolation>
    <batchsize>128</batchsize><scalefactor>10</scalefactor>
    <terminals>{terminals}</terminals>
    <works><work><time>{duration}</time><rate>unlimited</rate>
        <weights bench="tpcc">45,43,4,4,4</weights>
        <weights bench="chbenchmark">3,2,3,2,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5</weights>
    </work></works>
    <transactiontypes bench="chbenchmark">
        {"".join(f'<transactiontype><name>Q{i}</name></transactiontype>' for i in range(1,23))}
    </transactiontypes>
    <transactiontypes bench="tpcc">
        <transactiontype><name>NewOrder</name></transactiontype>
        <transactiontype><name>Payment</name></transactiontype>
        <transactiontype><name>OrderStatus</name></transactiontype>
        <transactiontype><name>Delivery</name></transactiontype>
        <transactiontype><name>StockLevel</name></transactiontype>
    </transactiontypes>
</parameters>"""
    p = os.path.join(BENCHBASE_DIR, "config", "web_run.xml")
    with open(p, "w") as f:
        f.write(cfg)
    return p


def _find_latest_summary() -> Optional[str]:
    rdir = os.path.join(BENCHBASE_DIR, "results")
    if not os.path.isdir(rdir):
        return None
    files = sorted([f for f in os.listdir(rdir) if f.endswith(".summary.json")], reverse=True)
    return os.path.join(rdir, files[0]) if files else None


def _parse_summary(path: str) -> Dict[str, Any]:
    with open(path) as f:
        d = json.load(f)
    lat = d.get("Latency Distribution", {})
    return {
        "tps": round(d.get("Throughput (requests/second)", 0), 2),
        "lat_avg": round(lat.get("Average Latency (microseconds)", 0) / 1000, 1),
        "lat_p95": round(lat.get("95th Percentile Latency (microseconds)", 0) / 1000, 1),
        "lat_p99": round(lat.get("99th Percentile Latency (microseconds)", 0) / 1000, 1),
        "requests": d.get("Measured Requests", 0),
    }


def _run_benchmark(duration: int, terminals: int, label: str) -> Dict[str, Any]:
    """运行 CH-Benchmark，同时真实 perf 采集 + 实时 PG 指标推送。"""
    from PerfCollectorPG import PerfCollectorPG

    cfg = _write_chbench_cfg(duration, terminals)

    # 记录运行前的 commit 数，用来计算瞬时 TPS
    prev = _pg_metrics()
    prev_commits = prev.get("xact_commit", 0)
    prev_time = time.time()

    # 后台线程 1：运行 BenchBase
    bench_result = {"done": False}
    def _bench():
        subprocess.run(
            ["java", "-jar", BENCHBASE_JAR,
             "--bench", "chbenchmark", "--config", cfg, "--execute=true"],
            capture_output=True, text=True, cwd=BENCHBASE_DIR,
            timeout=duration + 120)
        bench_result["done"] = True

    # 后台线程 2：真实 perf 采集（等 benchmark 进入稳态后采集中间段）
    perf_result: Dict[str, Any] = {"data_file": None}
    def _perf():
        time.sleep(5)
        collector = PerfCollectorPG()
        perf_dur = min(duration - 10, 15)
        if perf_dur < 3:
            perf_dur = 3
        perf_result["data_file"] = collector.collect(duration=perf_dur, frequency=99)

    t_bench = threading.Thread(target=_bench, daemon=True)
    t_perf  = threading.Thread(target=_perf, daemon=True)
    t_bench.start()
    t_perf.start()

    # 主循环：每 2 秒采集一次指标
    elapsed = 0
    while not bench_result["done"] and elapsed < duration + 30:
        time.sleep(2)
        elapsed += 2
        now = time.time()
        cur = _pg_metrics()
        cur_commits = cur.get("xact_commit", 0)
        dt = now - prev_time
        tps = (cur_commits - prev_commits) / dt if dt > 0 else 0
        prev_commits = cur_commits
        prev_time = now

        point = {
            "ts": round(elapsed),
            "tps": round(tps, 1),
            "cache_hit": round(cur.get("cache_hit_ratio", 0), 1),
            "phase": label,
        }
        with state.lock:
            state.metrics.append(point)
            state.progress = min(95, int(elapsed / duration * 50) + (50 if label == "after" else 0))

    t_bench.join(timeout=30)
    t_perf.join(timeout=30)

    # 用真实 perf 数据生成函数 profile
    if perf_result["data_file"]:
        collector = PerfCollectorPG()
        if label == "before":
            collector.write_abnormal_profile(perf_result["data_file"],
                os.path.join(PERFORMANCE_DIR, "chbench_abnormal_functions.txt"))
        else:
            collector.write_baseline_profile(perf_result["data_file"],
                os.path.join(PERFORMANCE_DIR, "chbench_normal_functions.txt"))

    summary_path = _find_latest_summary()
    return _parse_summary(summary_path) if summary_path else {}

# ─────────────────────────────────────────────────────────────────────────────
# KeenInsight 诊断
# ─────────────────────────────────────────────────────────────────────────────

def _run_diagnosis(metrics: Dict[str, Any]) -> List[str]:
    """运行 KeenInsight pipeline，返回诊断摘要列表。"""
    # 写 history JSON
    knobs = {}
    for k in DEFAULT_KNOBS:
        knobs[k] = _psql(f"SHOW {k};") or DEFAULT_KNOBS[k]
    record = {
        "timestamp": datetime.now().isoformat(),
        "workload": "chbenchmark",
        "external_metrics": {"tps": metrics.get("tps",0), "lat": metrics.get("lat_avg",0), "qps": metrics.get("tps",0)},
        "configuration": knobs,
        "function_file": "performance/chbench_abnormal_functions.txt",
    }
    os.makedirs(PERFORMANCE_DIR, exist_ok=True)
    with open(HISTORY_PERF_CHBENCH, "w") as f:
        json.dump({"data": [record]}, f, indent=2)

    # 运行 pipeline
    r = subprocess.run(
        [sys.executable, os.path.join(_HERE, "run_pipeline.py"),
         "--workload", "chbenchmark", "--db-type", "pg", "--skip-apply"],
        capture_output=True, text=True)

    # 提取关键信息
    lines = []
    for line in r.stdout.split("\n"):
        for kw in ["阈值违规", "异常函数", "knob 映射", "调优动作", "Tune '", "=>"]:
            if kw in line:
                lines.append(line.strip().lstrip("[INFO] ").lstrip("[WARN] ").lstrip("[OK]   "))
                break
    return lines

# ─────────────────────────────────────────────────────────────────────────────
# 完整流程（后台线程）
# ─────────────────────────────────────────────────────────────────────────────

BENCH_DURATION = 60
BENCH_TERMINALS = 30

def _full_pipeline():
    """在后台线程中运行完整演示流程。"""
    try:
        # Phase 1: Reset
        state.set(phase="resetting", message="正在重置数据库为默认保守参数 ...", progress=2, metrics=[], diagnosis=[], knob_changes=[], before_summary={}, after_summary={})
        _apply_knobs(DEFAULT_KNOBS, "default")
        state.set(progress=5)

        # Phase 2: Run degraded benchmark
        state.set(phase="loading", message="正在运行 CH-Benchmark (默认参数) ...", progress=8)
        before = _run_benchmark(BENCH_DURATION, BENCH_TERMINALS, "before")
        state.set(before_summary=before, progress=45)

        # Phase 3: Diagnose
        state.set(phase="diagnosing", message="KeenInsight 正在分析异常根因 ...", progress=50)
        diag_lines = _run_diagnosis(before)
        state.set(diagnosis=diag_lines, progress=60)
        time.sleep(2)  # 让用户看到诊断

        # Phase 4: Apply tuning
        state.set(phase="tuning", message="正在应用调优参数并重启数据库 ...", progress=65)
        changes = []
        for k in TUNED_KNOBS:
            changes.append({"knob": k, "before": DEFAULT_KNOBS.get(k, "?"), "after": TUNED_KNOBS[k]})
        state.set(knob_changes=changes)
        _apply_knobs(TUNED_KNOBS, "tuned")
        state.set(progress=70)

        # Phase 5: Re-run benchmark
        state.set(phase="verifying", message="正在运行 CH-Benchmark (调优后) ...", progress=72)
        after = _run_benchmark(BENCH_DURATION, BENCH_TERMINALS, "after")
        state.set(after_summary=after, progress=98)

        # Done
        state.set(phase="done", message="演示完成！调优效果已呈现", progress=100)

    except Exception as e:
        state.set(phase="idle", message=f"出错: {e}", progress=0)


# ─────────────────────────────────────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/status")
def api_status():
    return jsonify(state.snapshot())


@app.route("/api/start", methods=["POST"])
def api_start():
    if state.phase not in ("idle", "done"):
        return jsonify({"error": "流程正在运行中"}), 409
    t = threading.Thread(target=_full_pipeline, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/stream")
def api_stream():
    """SSE 实时指标流。"""
    def generate():
        last_len = 0
        while True:
            snap = state.snapshot()
            cur_len = len(snap["metrics"])
            # 只推送新增的点 + 状态
            payload = {
                "phase": snap["phase"],
                "message": snap["message"],
                "progress": snap["progress"],
                "new_metrics": snap["metrics"][last_len:],
                "diagnosis": snap["diagnosis"],
                "knob_changes": snap["knob_changes"],
                "before": snap["before"],
                "after": snap["after"],
            }
            last_len = cur_len
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            time.sleep(2)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    os.makedirs(os.path.join(_HERE, "web"), exist_ok=True)
    print("KeenInsight Dashboard: http://0.0.0.0:8888")
    app.run(host="0.0.0.0", port=8888, debug=False, threaded=True)
