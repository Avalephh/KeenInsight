#!/usr/bin/env python3
"""
场景一: CH-Benchmark 负载
===========================
运行 CH-Benchmark (ClickHouse 混合分析型负载) 产生真实的数据库压力，
同时采集 perf 数据供 KeenInsight 分析使用。

功能：
- 运行 CH-Benchmark 负载
- 实时采集 PostgreSQL 性能指标
- 使用 perf record 采集系统级函数热点
- 将结果写入 history JSON 文件供诊断分析

使用方式：
    python3 scenario_1_chbenchmark.py              # 运行完整流程
    python3 scenario_1_chbenchmark.py --duration 60 # 指定运行时间
    python3 scenario_1_chbenchmark.py --skip-perf   # 跳过 perf 采集
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── 路径配置 ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(_HERE)  # KeenInsight 根目录
sys.path.insert(0, BASE_DIR)

from config import (
    BENCHBASE_DIR,
    BENCHBASE_JAR,
    HISTORY_PERF_CHBENCH,
    PERFORMANCE_DIR,
)


# ── 配置 ────────────────────────────────────────────────────────────────────

DBNAME = "benchbase"

# 默认参数 (保守配置，产生性能问题)
DEFAULT_KNOBS = {
    "shared_buffers": "32MB",
    "work_mem": "1MB",
    "maintenance_work_mem": "16MB",
    "effective_cache_size": "512MB",
    "max_wal_size": "256MB",
    "checkpoint_completion_target": "0.5",
    "random_page_cost": "4",
    "effective_io_concurrency": "1",
}

# 调优参数 (优化配置)
TUNED_KNOBS = {
    "shared_buffers": "2GB",
    "work_mem": "64MB",
    "maintenance_work_mem": "512MB",
    "effective_cache_size": "20GB",
    "max_wal_size": "4GB",
    "checkpoint_completion_target": "0.9",
    "random_page_cost": "1.1",
    "effective_io_concurrency": "200",
}


@dataclass
class BenchmarkState:
    """Benchmark 运行状态"""
    phase: str = "idle"
    message: str = "就绪"
    progress: int = 0
    metrics: List[Dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "phase": self.phase,
                "message": self.message,
                "progress": self.progress,
                "metrics": self.metrics[-300:],
            }


state = BenchmarkState()


# ── 数据库工具 ──────────────────────────────────────────────────────────────

def _psql(sql: str) -> str:
    """执行 SQL 查询"""
    r = subprocess.run(
        ["su", "-", "postgres", "-c", f'psql -d {DBNAME} -t -c "{sql}"'],
        capture_output=True, text=True
    )
    return r.stdout.strip()


def _pg_metrics() -> Dict[str, float]:
    """从 pg_stat_database 获取实时指标"""
    try:
        row = _psql(
            "SELECT xact_commit, xact_rollback, blks_read, blks_hit, "
            "tup_returned, tup_fetched, temp_bytes, deadlocks "
            f"FROM pg_stat_database WHERE datname='{DBNAME}';"
        )
        parts = [x.strip() for x in row.split("|")]
        if len(parts) < 8:
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
            "temp_bytes": int(parts[6]),
            "deadlocks": int(parts[7]),
        }
    except Exception:
        return {}


def _apply_knobs(knobs: Dict[str, str]):
    """应用 PostgreSQL 配置"""
    for k, v in knobs.items():
        _psql(f"ALTER SYSTEM SET {k} = '{v}';")
    subprocess.run(["pg_ctlcluster", "12", "main", "reload"],
                   capture_output=True, text=True)
    time.sleep(2)


# ── BenchBase 运行 ───────────────────────────────────────────────────────────

def _write_chbench_cfg(duration: int, terminals: int) -> str:
    """生成 CH-Benchmark 配置文件"""
    cfg = f"""<?xml version="1.0"?>
<parameters>
    <type>POSTGRES</type>
    <driver>org.postgresql.Driver</driver>
    <url>jdbc:postgresql://localhost:5432/{DBNAME}?sslmode=disable</url>
    <username>admin</username>
    <password>password</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <isolation>TRANSACTION_READ_COMMITTED</isolation>
    <batchsize>128</batchsize>
    <scalefactor>10</scalefactor>
    <terminals>{terminals}</terminals>
    <works>
        <work>
            <time>{duration}</time>
            <rate>unlimited</rate>
            <weights bench="chbenchmark">3,2,3,2,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5</weights>
        </work>
    </works>
    <transactiontypes bench="chbenchmark">
        {"".join(f'<transactiontype><name>Q{i}</name></transactiontype>' for i in range(1, 23))}
    </transactiontypes>
</parameters>"""
    cfg_path = os.path.join(BENCHBASE_DIR, "config", "web_run.xml")
    with open(cfg_path, "w") as f:
        f.write(cfg)
    return cfg_path


def _find_latest_summary() -> Optional[str]:
    """查找最新的 benchmark 结果文件"""
    rdir = os.path.join(BENCHBASE_DIR, "results")
    if not os.path.isdir(rdir):
        return None
    files = sorted([f for f in os.listdir(rdir) if f.endswith(".summary.json")], reverse=True)
    return os.path.join(rdir, files[0]) if files else None


def _parse_summary(path: str) -> Dict[str, Any]:
    """解析 benchmark 结果"""
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


def _run_benchmark(duration: int, terminals: int, label: str, skip_perf: bool = False) -> Dict[str, Any]:
    """运行 CH-Benchmark，同时采集 perf 数据"""
    from PerfCollectorPG import PerfCollectorPG

    cfg = _write_chbench_cfg(duration, terminals)

    # 记录初始状态
    prev = _pg_metrics()
    prev_commits = prev.get("xact_commit", 0)
    prev_time = time.time()

    # 后台线程 1: 运行 BenchBase
    bench_result = {"done": False}
    def _bench():
        try:
            subprocess.run(
                ["java", "-jar", BENCHBASE_JAR,
                 "--bench", "chbenchmark", "--config", cfg, "--execute=true"],
                capture_output=True, text=True, cwd=BENCHBASE_DIR,
                timeout=duration + 120
            )
        except subprocess.TimeoutExpired:
            pass
        bench_result["done"] = True

    # 后台线程 2: 采集 perf 数据
    perf_result: Dict[str, Any] = {"data_file": None}
    def _perf():
        if skip_perf:
            return
        time.sleep(3)
        collector = PerfCollectorPG()
        perf_dur = min(duration - 6, 15)
        if perf_dur < 5:
            perf_dur = 5
        perf_result["data_file"] = collector.collect(duration=perf_dur, frequency=99)

    t_bench = threading.Thread(target=_bench, daemon=True)
    t_perf = threading.Thread(target=_perf, daemon=True)
    t_bench.start()
    t_perf.start()

    # 主循环: 采集指标
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
            state.progress = min(95, int(elapsed / duration * 100))

    t_bench.join(timeout=30)
    t_perf.join(timeout=30)

    # 写入 perf profile
    if perf_result["data_file"] and not skip_perf:
        collector = PerfCollectorPG()
        if label == "before":
            collector.write_abnormal_profile(
                perf_result["data_file"],
                os.path.join(PERFORMANCE_DIR, "chbench_abnormal_functions.txt")
            )
        else:
            collector.write_baseline_profile(
                perf_result["data_file"],
                os.path.join(PERFORMANCE_DIR, "chbench_normal_functions.txt")
            )

    summary_path = _find_latest_summary()
    return _parse_summary(summary_path) if summary_path else {}


# ── 主流程 ─────────────────────────────────────────────────────────────────

def run_scenario(
    duration: int = 60,
    terminals: int = 30,
    knob_mode: str = "default",
    skip_perf: bool = False,
) -> Dict[str, Any]:
    """
    运行场景一: CH-Benchmark

    参数:
        duration: 运行时间 (秒)
        terminals: 并发终端数
        knob_mode: "default" (保守) 或 "tuned" (优化)
        skip_perf: 是否跳过 perf 采集
    """
    print(f"\n{'='*60}")
    print(f"  场景一: CH-Benchmark 负载测试")
    print(f"  duration={duration}s, terminals={terminals}, mode={knob_mode}")
    print(f"{'='*60}")

    knobs = DEFAULT_KNOBS if knob_mode == "default" else TUNED_KNOBS

    # Phase 1: 应用配置
    state.set(phase="config", message="应用数据库配置...", progress=5)
    _apply_knobs(knobs)

    # Phase 2: 运行 benchmark
    state.set(phase="benchmark", message="运行 CH-Benchmark...", progress=10)
    result = _run_benchmark(duration, terminals, "before" if knob_mode == "default" else "after", skip_perf)

    # Phase 3: 更新 history JSON
    state.set(phase="saving", message="保存结果...", progress=95)

    knobs_values = {}
    for k in DEFAULT_KNOBS:
        knobs_values[k] = _psql(f"SHOW {k};") or DEFAULT_KNOBS[k]

    record = {
        "timestamp": datetime.now().isoformat(),
        "workload": "chbenchmark",
        "external_metrics": {
            "tps": result.get("tps", 0),
            "lat": result.get("lat_avg", 0),
            "qps": result.get("tps", 0),
        },
        "configuration": knobs_values,
        "function_file": "performance/chbench_abnormal_functions.txt" if knob_mode == "default" else "performance/chbench_normal_functions.txt",
    }

    # 追加到历史文件
    try:
        with open(HISTORY_PERF_CHBENCH) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {"data": []}

    history["data"].append(record)

    os.makedirs(PERFORMANCE_DIR, exist_ok=True)
    with open(HISTORY_PERF_CHBENCH, "w") as f:
        json.dump(history, f, indent=2)

    state.set(phase="done", message="完成", progress=100)

    print(f"\n[OK] 结果:")
    print(f"  TPS: {result.get('tps', 0):.2f}")
    print(f"  平均延迟: {result.get('lat_avg', 0):.2f}ms")
    print(f"  P95 延迟: {result.get('lat_p95', 0):.2f}ms")
    print(f"  缓存命中率: {state.metrics[-1].get('cache_hit', 0) if state.metrics else 0:.1f}%")

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="场景一: CH-Benchmark 负载")
    parser.add_argument("--duration", type=int, default=60, help="运行时间 (秒)")
    parser.add_argument("--terminals", type=int, default=30, help="并发终端数")
    parser.add_argument("--mode", choices=["default", "tuned"], default="default",
                        help="配置模式: default=保守配置, tuned=优化配置")
    parser.add_argument("--skip-perf", action="store_true", help="跳过 perf 数据采集")
    args = parser.parse_args()

    run_scenario(
        duration=args.duration,
        terminals=args.terminals,
        knob_mode=args.mode,
        skip_perf=args.skip_perf,
    )


if __name__ == "__main__":
    main()