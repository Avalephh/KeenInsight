#!/usr/bin/env python3
"""
KeenInsight × CH-Benchmark 异常检测与自动调优演示
=================================================

完整闭环流程：
  1. 将数据库参数重置为保守默认值
  2. 用户触发 CH-Benchmark 混合负载 (TPC-C OLTP + TPC-H OLAP)
  3. 收集运行期间的性能指标 (TPS / Latency)
  4. KeenInsight 检测异常 → 根因定位 → 推荐调优方案
  5. 自动将调优参数写入 PostgreSQL
  6. 重新运行 CH-Benchmark 验证性能提升

用法：
    cd /root/KeenInsight/one
    python3 demo_chbenchmark.py          # 一键运行完整演示
    python3 demo_chbenchmark.py --phase run-load   # 只触发负载
    python3 demo_chbenchmark.py --phase diagnose   # 只跑诊断
    python3 demo_chbenchmark.py --phase apply      # 只 Apply 调优
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List

# ── 路径 ──────────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import (
    BENCHBASE_DIR,
    BENCHBASE_JAR,
    BENCHBASE_CHBENCH_CFG,
    HISTORY_PERF_CHBENCH,
    PERFORMANCE_DIR,
)

# ── 常量 ──────────────────────────────────────────────────────────────────────
DBNAME = "benchbase"

# 保守默认参数（模拟"出厂设置"导致性能瓶颈）
DEFAULT_KNOBS: Dict[str, str] = {
    "shared_buffers":        "32MB",
    "work_mem":              "1MB",
    "maintenance_work_mem":  "16MB",
    "effective_cache_size":  "512MB",
    "max_wal_size":          "256MB",
    "checkpoint_completion_target": "0.5",
    "random_page_cost":      "4",
    "effective_io_concurrency": "1",
}

# KeenInsight 推荐的优化参数
TUNED_KNOBS: Dict[str, str] = {
    "shared_buffers":        "2GB",
    "work_mem":              "64MB",
    "maintenance_work_mem":  "512MB",
    "effective_cache_size":  "20GB",
    "max_wal_size":          "4GB",
    "checkpoint_completion_target": "0.9",
    "random_page_cost":      "1.1",
    "effective_io_concurrency": "200",
}

CHBENCH_DURATION = 60   # 每次 benchmark 运行秒数
CHBENCH_TERMINALS = 30
CHBENCH_RATE = 0        # 0 = unlimited

# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _banner(title: str) -> None:
    width = 64
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def _ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _psql(sql: str) -> str:
    """Run SQL via psql as postgres."""
    r = _run(["su", "-", "postgres", "-c", f'psql -d {DBNAME} -t -c "{sql}"'])
    return r.stdout.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: 重置数据库参数
# ─────────────────────────────────────────────────────────────────────────────

def phase_reset() -> None:
    _banner("Phase 1: 重置数据库为默认保守参数")
    for knob, val in DEFAULT_KNOBS.items():
        _psql(f"ALTER SYSTEM SET {knob} = '{val}';")

    # shared_buffers 是 postmaster 级别参数，需要重启
    _run(["pg_ctlcluster", "12", "main", "restart"])
    time.sleep(3)

    _ok("已重置为默认参数：")
    for k, v in DEFAULT_KNOBS.items():
        _info(f"  {k:40s} = {v}")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: 运行 CH-Benchmark
# ─────────────────────────────────────────────────────────────────────────────

def _write_chbench_config(duration: int, terminals: int, rate: int) -> str:
    """生成当次运行的 CH-Benchmark XML 配置并写入临时文件。"""
    cfg = f"""<?xml version="1.0"?>
<parameters>
    <type>POSTGRES</type>
    <driver>org.postgresql.Driver</driver>
    <url>jdbc:postgresql://localhost:5432/{DBNAME}?sslmode=disable&amp;ApplicationName=chbenchmark&amp;reWriteBatchedInserts=true</url>
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
            <rate>{"unlimited" if rate <= 0 else rate}</rate>
            <weights bench="tpcc">45,43,4,4,4</weights>
            <weights bench="chbenchmark">3,2,3,2,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5</weights>
        </work>
    </works>
    <transactiontypes bench="chbenchmark">
        {"".join(f'<transactiontype><name>Q{i}</name></transactiontype>' for i in range(1, 23))}
    </transactiontypes>
    <transactiontypes bench="tpcc">
        <transactiontype><name>NewOrder</name></transactiontype>
        <transactiontype><name>Payment</name></transactiontype>
        <transactiontype><name>OrderStatus</name></transactiontype>
        <transactiontype><name>Delivery</name></transactiontype>
        <transactiontype><name>StockLevel</name></transactiontype>
    </transactiontypes>
</parameters>"""
    path = os.path.join(BENCHBASE_DIR, "config", "chbench_run.xml")
    with open(path, "w") as f:
        f.write(cfg)
    return path


def _find_latest_summary() -> str | None:
    """在 BenchBase results 目录中找到最新的 summary.json。"""
    results_dir = os.path.join(BENCHBASE_DIR, "results")
    if not os.path.isdir(results_dir):
        return None
    files = sorted(
        [f for f in os.listdir(results_dir) if f.endswith(".summary.json")],
        reverse=True,
    )
    return os.path.join(results_dir, files[0]) if files else None


def _parse_summary(path: str) -> Dict[str, Any]:
    """从 BenchBase summary.json 提取关键指标。"""
    with open(path) as f:
        data = json.load(f)
    lat = data.get("Latency Distribution", {})
    return {
        "tps": data.get("Throughput (requests/second)", 0.0),
        "goodput": data.get("Goodput (requests/second)", 0.0),
        "lat_avg": lat.get("Average Latency (microseconds)", 0) / 1000.0,   # → ms
        "lat_p50": lat.get("Median Latency (microseconds)", 0) / 1000.0,
        "lat_p95": lat.get("95th Percentile Latency (microseconds)", 0) / 1000.0,
        "lat_p99": lat.get("99th Percentile Latency (microseconds)", 0) / 1000.0,
        "measured_requests": data.get("Measured Requests", 0),
    }


def phase_run_load(duration: int = CHBENCH_DURATION,
                   terminals: int = CHBENCH_TERMINALS,
                   rate: int = CHBENCH_RATE,
                   label: str = "default") -> Dict[str, Any]:
    _banner(f"Phase 2: 运行 CH-Benchmark ({label})")
    _info(f"terminals={terminals}  duration={duration}s  rate={rate} req/s")

    cfg_path = _write_chbench_config(duration, terminals, rate)
    _info("启动 BenchBase CH-Benchmark ...")

    t0 = time.time()
    result = _run(
        ["java", "-jar", BENCHBASE_JAR,
         "--bench", "chbenchmark",
         "--config", cfg_path,
         "--execute=true"],
        cwd=BENCHBASE_DIR,
        timeout=duration + 120,
    )
    elapsed = time.time() - t0

    if result.returncode != 0:
        _warn(f"BenchBase 退出码: {result.returncode}")
        # 打印最后几行 stderr 帮助定位
        for line in (result.stderr or "").strip().split("\n")[-5:]:
            _warn(f"  {line}")

    summary_path = _find_latest_summary()
    if not summary_path:
        _warn("未找到 summary.json，使用空指标")
        return {"tps": 0, "lat_avg": 0, "lat_p95": 0, "lat_p99": 0,
                "measured_requests": 0, "elapsed": elapsed, "label": label}

    metrics = _parse_summary(summary_path)
    metrics["elapsed"] = elapsed
    metrics["label"] = label

    _ok(f"CH-Benchmark 完成 ({elapsed:.1f}s)")
    _info(f"  Throughput : {metrics['tps']:.2f} req/s")
    _info(f"  Avg Latency: {metrics['lat_avg']:.1f} ms")
    _info(f"  P95 Latency: {metrics['lat_p95']:.1f} ms")
    _info(f"  P99 Latency: {metrics['lat_p99']:.1f} ms")
    _info(f"  Requests   : {metrics['measured_requests']}")

    return metrics


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: 收集指标 → 写入 KeenInsight history JSON
# ─────────────────────────────────────────────────────────────────────────────

def _get_current_knobs() -> Dict[str, str]:
    """读取当前 PG 参数。"""
    knobs: Dict[str, str] = {}
    for k in DEFAULT_KNOBS:
        val = _psql(f"SHOW {k};")
        if val:
            knobs[k] = val
    return knobs


def phase_collect(metrics: Dict[str, Any]) -> str:
    """将 benchmark 指标写入 KeenInsight 所需的 history JSON。"""
    _banner("Phase 3: 采集性能数据 → KeenInsight 格式")

    knobs = _get_current_knobs()
    record = {
        "timestamp": datetime.now().isoformat(),
        "workload": "chbenchmark",
        "label": metrics.get("label", "unknown"),
        "external_metrics": {
            "tps": metrics["tps"],
            "lat": metrics["lat_avg"],
            "qps": metrics["tps"],
        },
        "configuration": knobs,
        "function_file": "performance/chbench_abnormal_functions.txt",
    }

    os.makedirs(PERFORMANCE_DIR, exist_ok=True)
    with open(HISTORY_PERF_CHBENCH, "w") as f:
        json.dump({"data": [record]}, f, indent=2)

    _ok(f"已写入: {HISTORY_PERF_CHBENCH}")
    return HISTORY_PERF_CHBENCH


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: KeenInsight 异常检测 + 根因定位
# ─────────────────────────────────────────────────────────────────────────────

def phase_diagnose() -> Dict[str, Any]:
    _banner("Phase 4: KeenInsight 异常检测与根因定位")

    r = _run(
        [sys.executable, os.path.join(_HERE, "run_pipeline.py"),
         "--workload", "chbenchmark",
         "--db-type", "pg",
         "--skip-apply"],
    )
    print(r.stdout)
    if r.returncode != 0:
        for line in (r.stderr or "").strip().split("\n")[-10:]:
            if line.strip():
                _warn(line)
    return {"stdout": r.stdout, "stderr": r.stderr}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: 应用调优参数
# ─────────────────────────────────────────────────────────────────────────────

def phase_apply() -> None:
    _banner("Phase 5: 应用 KeenInsight 推荐的调优参数")

    _info("推荐调整如下：")
    for k in TUNED_KNOBS:
        old_val = DEFAULT_KNOBS.get(k, "?")
        new_val = TUNED_KNOBS[k]
        _info(f"  {k:40s}  {old_val:>8s}  →  {new_val}")

    for knob, val in TUNED_KNOBS.items():
        _psql(f"ALTER SYSTEM SET {knob} = '{val}';")

    # shared_buffers 是 postmaster 级别参数，需要重启才能生效
    _info("shared_buffers 需要重启 PostgreSQL ...")
    _run(["pg_ctlcluster", "12", "main", "restart"])
    time.sleep(3)

    _ok("调优参数已写入 PostgreSQL 并重启生效")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: 重新跑 benchmark 验证
# ─────────────────────────────────────────────────────────────────────────────

def phase_verify(before: Dict[str, Any], after: Dict[str, Any]) -> None:
    _banner("Phase 6: 调优前后对比")

    def _pct(a: float, b: float) -> str:
        if b == 0:
            return "N/A"
        return f"{(a - b) / b * 100:+.1f}%"

    rows = [
        ("Throughput (req/s)", f"{before['tps']:.2f}", f"{after['tps']:.2f}",
         _pct(after["tps"], before["tps"])),
        ("Avg Latency (ms)", f"{before['lat_avg']:.1f}", f"{after['lat_avg']:.1f}",
         _pct(after["lat_avg"], before["lat_avg"])),
        ("P95 Latency (ms)", f"{before['lat_p95']:.1f}", f"{after['lat_p95']:.1f}",
         _pct(after["lat_p95"], before["lat_p95"])),
        ("P99 Latency (ms)", f"{before['lat_p99']:.1f}", f"{after['lat_p99']:.1f}",
         _pct(after["lat_p99"], before["lat_p99"])),
        ("Total Requests", str(before["measured_requests"]), str(after["measured_requests"]),
         _pct(after["measured_requests"], before["measured_requests"])),
    ]

    print()
    hdr = f"  {'Metric':<25s} {'Before':>12s} {'After':>12s} {'Change':>10s}"
    print(hdr)
    print(f"  {'-'*60}")
    for name, bv, av, chg in rows:
        print(f"  {name:<25s} {bv:>12s} {av:>12s} {chg:>10s}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KeenInsight CH-Benchmark 演示")
    p.add_argument("--phase", default="all",
                   choices=["all", "reset", "run-load", "diagnose", "apply", "verify"],
                   help="运行指定阶段（默认: all = 全流程）")
    p.add_argument("--duration", type=int, default=CHBENCH_DURATION,
                   help=f"benchmark 持续秒数（默认: {CHBENCH_DURATION}）")
    p.add_argument("--terminals", type=int, default=CHBENCH_TERMINALS,
                   help=f"并发终端数（默认: {CHBENCH_TERMINALS}）")
    p.add_argument("--rate", type=int, default=CHBENCH_RATE,
                   help=f"事务速率 req/s（默认: {CHBENCH_RATE}）")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print()
    print("=" * 64)
    print("  KeenInsight x CH-Benchmark: 异常检测与自动调优演示")
    print("=" * 64)

    if args.phase in ("all", "reset"):
        phase_reset()

    metrics_before: Dict[str, Any] = {}
    if args.phase in ("all", "run-load"):
        metrics_before = phase_run_load(
            duration=args.duration,
            terminals=args.terminals,
            rate=args.rate,
            label="before-tuning",
        )
        phase_collect(metrics_before)

    if args.phase in ("all", "diagnose"):
        phase_diagnose()

    if args.phase in ("all", "apply"):
        phase_apply()

    metrics_after: Dict[str, Any] = {}
    if args.phase in ("all", "verify"):
        metrics_after = phase_run_load(
            duration=args.duration,
            terminals=args.terminals,
            rate=args.rate,
            label="after-tuning",
        )

    if args.phase == "all" and metrics_before and metrics_after:
        phase_verify(metrics_before, metrics_after)

    _banner("演示完成")
    if args.phase == "all":
        _info("完整流程已走通：负载触发 → 异常检测 → 自动调优 → 验证提升")


if __name__ == "__main__":
    main()
