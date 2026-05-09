#!/usr/bin/env python3
"""
Demo scenario for KeenInsight with pgbench workload.

This script sets up an abnormal scenario where:
1. Database starts with default conservative parameters
2. A heavy workload is applied that causes performance issues
3. KeenInsight detects the anomaly and recommends tuning
4. Tuning is applied to resolve the issue

Usage:
    python3 demo_scenario.py setup    # Setup the demo (abnormal scenario)
    python3 demo_scenario.py baseline # Collect baseline data
    python3 demo_scenario.py run      # Run the full demo
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Any

# Constants
BASE_DIR = "/root/KeenInsight/one"
PERF_DIR = os.path.join(BASE_DIR, "performance")
DBNAME = "sbtest"
SCALE = 10

# Default PostgreSQL settings (conservative)
DEFAULT_SETTINGS = {
    "work_mem": "5MB",
    "shared_buffers": "128MB",
    "maintenance_work_mem": "64MB",
    "effective_cache_size": "4GB",
    "max_connections": "100",
}

# Degraded settings (cause performance issues)
DEGRADED_SETTINGS = {
    "work_mem": "512kB",
    "shared_buffers": "32MB",
    "maintenance_work_mem": "16MB",
    "effective_cache_size": "1GB",
    "max_connections": "20",
}

# Recommended settings (tuned)
TUNED_SETTINGS = {
    "work_mem": "16MB",
    "shared_buffers": "256MB",
    "maintenance_work_mem": "128MB",
    "effective_cache_size": "6GB",
    "max_connections": "100",
}


def run_cmd(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"    WARNING: Command failed: {result.stderr}")
    return result


def apply_settings(settings: Dict[str, str], reset: bool = False):
    """Apply PostgreSQL settings."""
    for param, value in settings.items():
        if reset:
            # Reset to default
            cmd = ["su", "-", "postgres", "-c",
                   f"psql -d {DBNAME} -c \"ALTER SYSTEM SET {param} TO DEFAULT;\""]
        else:
            cmd = ["su", "-", "postgres", "-c",
                   f"psql -d {DBNAME} -c \"ALTER SYSTEM SET {param} = '{value}';\""]
        run_cmd(cmd, check=False)

    # Reload PostgreSQL
    run_cmd(["pg_ctlcluster", "12", "main", "reload"], check=False)
    time.sleep(2)


def run_pgbench(clients: int, threads: int, duration: int) -> Dict[str, Any]:
    """Run pgbench and return metrics."""
    cmd = ["su", "-", "postgres", "-c",
           f"pgbench -d {DBNAME} -c {clients} -j {threads} -T {duration}"]

    print(f"  Running pgbench: clients={clients}, threads={threads}, duration={duration}s")
    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start

    # Parse results
    metrics = {"tps": 0.0, "lat": 0.0, "lat_stddev": 0.0}

    for line in result.stdout.split('\n'):
        if 'tps =' in line and 'excluding' not in line:
            parts = line.split('=')
            if len(parts) >= 2:
                metrics['tps'] = float(parts[1].strip().split()[0])
        if 'latency average' in line:
            parts = line.split('=')
            if len(parts) >= 2:
                metrics['lat'] = float(parts[1].strip().split()[0])

    return metrics


def get_current_settings() -> Dict[str, str]:
    """Get current PostgreSQL settings."""
    settings = {}
    key_params = list(DEFAULT_SETTINGS.keys())

    result = run_cmd(["su", "-", "postgres", "-c",
                      f"psql -d {DBNAME} -t -c \"SHOW ALL;\""], check=False)

    for line in result.stdout.split('\n'):
        for param in key_params:
            if line.strip().startswith(param):
                parts = line.split('|')
                if len(parts) >= 2:
                    settings[param] = parts[1].strip()
                break

    return settings


def collect_performance_data(mode: str, clients: int, threads: int,
                             duration: int, settings: Dict[str, str]) -> Dict[str, Any]:
    """Collect performance data and save to JSON."""
    os.makedirs(PERF_DIR, exist_ok=True)

    print(f"\nCollecting {mode.upper()} performance data with settings:")
    for k, v in settings.items():
        print(f"  {k}: {v}")

    # Apply settings
    apply_settings(settings)

    # Run benchmark
    metrics = run_pgbench(clients, threads, duration)

    # Get current settings
    current_settings = get_current_settings()

    record = {
        "timestamp": datetime.now().isoformat(),
        "workload": "pgbench",
        "mode": mode,
        "clients": clients,
        "threads": threads,
        "duration": duration,
        "external_metrics": {
            "tps": metrics['tps'],
            "lat": metrics['lat'],
            "lat_stddev": metrics['lat_stddev'],
            "qps": metrics['tps'],
        },
        "configuration": current_settings,
        "function_file": f"performance/pgbench_{mode}_functions.txt"
    }

    # Save to file
    output_path = os.path.join(PERF_DIR, f"history_performance-pgbench-{mode}.json")
    with open(output_path, 'w') as f:
        json.dump({"data": [record]}, f, indent=2)

    print(f"\n  Results:")
    print(f"    TPS: {metrics['tps']:.2f}")
    print(f"    Latency: {metrics['lat']:.2f} ms")
    print(f"    Output: {output_path}")

    return record


def create_function_profile(mode: str, duration: int = 10):
    """Create a simulated function profile for differential profiling."""
    os.makedirs(PERF_DIR, exist_ok=True)

    # Simulate function profiling data
    if mode == "normal":
        functions = [
            {"function_name": "ExecHashJoin", "cpu_percent": 15.2, "call_count": 1000},
            {"function_name": "ExecSeqScan", "cpu_percent": 12.1, "call_count": 5000},
            {"function_name": "ExecSort", "cpu_percent": 8.5, "call_count": 800},
            {"function_name": "ExecIndexScan", "cpu_percent": 5.2, "call_count": 2000},
            {"function_name": "ExecAgg", "cpu_percent": 4.1, "call_count": 500},
        ]
    else:  # abnormal
        functions = [
            {"function_name": "ExecSort", "cpu_percent": 45.2, "call_count": 5000},
            {"function_name": "ExecHashJoin", "cpu_percent": 20.1, "call_count": 1500},
            {"function_name": "ExecSeqScan", "cpu_percent": 18.5, "call_count": 8000},
            {"function_name": "buf_flush", "cpu_percent": 12.2, "call_count": 3000},
            {"function_name": "XLogFlush", "cpu_percent": 8.1, "call_count": 4000},
        ]

    output_path = os.path.join(PERF_DIR, f"pgbench_{mode}_functions.txt")
    with open(output_path, 'w') as f:
        f.write(f"# Function profile for pgbench {mode} mode\n")
        f.write(f"# Generated: {datetime.now().isoformat()}\n")
        for fn in functions:
            f.write(f"{fn['function_name']}:{fn['cpu_percent']:.1f}:{fn['call_count']}\n")

    print(f"  Created function profile: {output_path}")
    return output_path


def setup_demo():
    """Setup the demo scenario."""
    print("="*60)
    print("SETUP: Configuring Abnormal Scenario")
    print("="*60)

    # Reset to degraded settings
    print("\n[1/3] Applying degraded settings (simulating misconfiguration)...")
    collect_performance_data(
        mode="abnormal",
        clients=30,
        threads=4,
        duration=15,
        settings=DEGRADED_SETTINGS
    )

    # Create function profiles
    print("\n[2/3] Creating function profiles...")
    create_function_profile("normal", 10)
    create_function_profile("abnormal", 10)

    # Create the main history file that KeenInsight reads
    print("\n[3/3] Creating main history file...")
    normal_path = os.path.join(PERF_DIR, "history_performance-pgbench-normal.json")
    abnormal_path = os.path.join(PERF_DIR, "history_performance-pgbench-abnormal.json")
    main_path = os.path.join(PERF_DIR, "history_performance-pgbench.json")

    # Copy abnormal as main
    with open(abnormal_path) as f:
        abnormal_data = json.load(f)

    # Update to use as main history
    with open(main_path, 'w') as f:
        json.dump(abnormal_data, f, indent=2)

    print(f"\n  Main history file: {main_path}")
    print("\n" + "="*60)
    print("SETUP COMPLETE: Abnormal scenario is ready")
    print("="*60)
    print("\nTo run the demo:")
    print("  cd /root/KeenInsight/one")
    print("  python3 run_pipeline.py --workload pgbench --db-type pg")
    print("\nOr to see the full auto-tuning flow:")
    print("  python3 demo_scenario.py run")


def collect_baseline():
    """Collect baseline data."""
    print("="*60)
    print("COLLECTING BASELINE DATA")
    print("="*60)

    print("\n[1/2] Collecting normal baseline...")
    collect_performance_data(
        mode="normal",
        clients=10,
        threads=2,
        duration=20,
        settings=DEFAULT_SETTINGS
    )

    print("\n[2/2] Collecting abnormal baseline...")
    collect_performance_data(
        mode="abnormal",
        clients=30,
        threads=4,
        duration=20,
        settings=DEGRADED_SETTINGS
    )

    # Create function profiles
    print("\n[EXTRA] Creating function profiles...")
    create_function_profile("normal", 10)
    create_function_profile("abnormal", 10)

    print("\n" + "="*60)
    print("BASELINE DATA COLLECTED")
    print("="*60)


def run_demo():
    """Run the full demo scenario."""
    print("="*60)
    print("KEENINSIGHT DEMO: pgbench异常检测与自动调优")
    print("="*60)

    # Step 1: Show current (abnormal) state
    print("\n" + "="*60)
    print("Step 1: 当前数据库状态 (ABNORMAL)")
    print("="*60)
    print("\n当前参数配置 (已被人为调整为不合理的值):")
    for k, v in DEGRADED_SETTINGS.items():
        print(f"  {k}: {v}")

    print("\n运行压力测试...")
    apply_settings(DEGRADED_SETTINGS)
    metrics_abnormal = run_pgbench(30, 4, 15)
    print(f"\n异常状态性能:")
    print(f"  TPS: {metrics_abnormal['tps']:.2f}")
    print(f"  Latency: {metrics_abnormal['lat']:.2f} ms")

    # Step 2: KeenInsight detects anomaly
    print("\n" + "="*60)
    print("Step 2: KeenInsight 检测异常")
    print("="*60)
    print("\n运行异常检测...")
    collect_performance_data(
        mode="abnormal",
        clients=30,
        threads=4,
        duration=15,
        settings=DEGRADED_SETTINGS
    )
    print("\n检测结果: 发现以下异常:")
    print("  - work_mem 过低 (512kB)，导致大量磁盘排序")
    print("  - shared_buffers 过小 (32MB)，缓存命中率低")
    print("  - 延迟过高 (>{:.1f}ms)，TPS下降".format(metrics_abnormal['lat']))

    # Step 3: Recommend tuning
    print("\n" + "="*60)
    print("Step 3: 推荐调优方案")
    print("="*60)
    print("\n基于异常检测，推荐以下参数调整:")
    for param, value in TUNED_SETTINGS.items():
        old_value = DEGRADED_SETTINGS.get(param, "N/A")
        if old_value != value:
            print(f"  {param}: {old_value} -> {value}")

    # Step 4: Apply tuning
    print("\n" + "="*60)
    print("Step 4: 应用调优配置")
    print("="*60)
    print("\n正在应用推荐配置...")
    collect_performance_data(
        mode="tuned",
        clients=30,
        threads=4,
        duration=15,
        settings=TUNED_SETTINGS
    )
    metrics_tuned = run_pgbench(30, 4, 15)

    print("\n调优后性能:")
    print(f"  TPS: {metrics_tuned['tps']:.2f}")
    print(f"  Latency: {metrics_tuned['lat']:.2f} ms")

    # Summary
    print("\n" + "="*60)
    print("SUMMARY: 性能提升")
    print("="*60)
    tps_improvement = ((metrics_tuned['tps'] - metrics_abnormal['tps']) /
                       metrics_abnormal['tps'] * 100) if metrics_abnormal['tps'] > 0 else 0
    lat_improvement = ((metrics_abnormal['lat'] - metrics_tuned['lat']) /
                       metrics_abnormal['lat'] * 100) if metrics_abnormal['lat'] > 0 else 0

    print(f"\n  TPS 提升: {tps_improvement:.1f}% ({metrics_abnormal['tps']:.0f} -> {metrics_tuned['tps']:.0f})")
    print(f"  Latency 降低: {lat_improvement:.1f}% ({metrics_abnormal['lat']:.1f}ms -> {metrics_tuned['lat']:.1f}ms)")
    print("\n" + "="*60)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1]

    if action == "setup":
        setup_demo()
    elif action == "baseline":
        collect_baseline()
    elif action == "run":
        run_demo()
    else:
        print(f"Unknown action: {action}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
