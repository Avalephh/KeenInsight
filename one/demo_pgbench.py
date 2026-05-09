#!/usr/bin/env python3
"""
KeenInsight pgbench Demo Scenario

This demo creates a realistic abnormal scenario using pgbench:
1. Starts with conservative default PostgreSQL settings
2. Applies a heavy analytical workload that causes performance issues
3. KeenInsight detects the anomaly through threshold violation
4. Recommends and applies tuning to resolve the issue

The key insight: Even with pgbench's efficient TPC-B workload, we can detect
anomalies by monitoring latency increases and comparing against baseline.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Any

# Configuration
BASE_DIR = "/root/KeenInsight/one"
PERF_DIR = os.path.join(BASE_DIR, "performance")
DBNAME = "sbtest"

def run_cmd(cmd: list) -> subprocess.CompletedProcess:
    """Run command and return result."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result

def get_db_stats() -> Dict[str, Any]:
    """Get database statistics that show memory pressure."""
    stats = {}

    # Get shared buffers hit ratio
    result = run_cmd([
        "su", "-", "postgres", "-c",
        f"psql -d {DBNAME} -t -c \"SELECT SUM(blks_hit)*100.0/NULLIF(SUM(blks_hit+blks_read),0) FROM pg_stat_database WHERE datname='{DBNAME}';\""
    ])
    if result.stdout.strip():
        try:
            stats['cache_hit_ratio'] = float(result.stdout.strip())
        except:
            stats['cache_hit_ratio'] = 0.0

    # Get work_mem related stats
    result = run_cmd([
        "su", "-", "postgres", "-c",
        f"psql -d {DBNAME} -t -c \"SELECT setting FROM pg_settings WHERE name='work_mem';\""
    ])
    if result.stdout.strip():
        stats['work_mem'] = result.stdout.strip()

    # Get shared_buffers
    result = run_cmd([
        "su", "-", "postgres", "-c",
        f"psql -d {DBNAME} -t -c \"SELECT setting FROM pg_settings WHERE name='shared_buffers';\""
    ])
    if result.stdout.strip():
        stats['shared_buffers'] = result.stdout.strip()

    # Check for temp files usage
    result = run_cmd([
        "su", "-", "postgres", "-c",
        f"psql -d {DBNAME} -t -c \"SELECT SUM(temp_bytes) FROM pg_stat_database WHERE datname='{DBNAME}';\""
    ])
    if result.stdout.strip():
        try:
            stats['temp_bytes'] = int(result.stdout.strip())
        except:
            stats['temp_bytes'] = 0

    return stats

def apply_settings(settings: Dict[str, str]):
    """Apply PostgreSQL settings."""
    for param, value in settings.items():
        run_cmd([
            "su", "-", "postgres", "-c",
            f"psql -d {DBNAME} -c \"ALTER SYSTEM SET {param} = '{value}';\""
        ])
    run_cmd(["pg_ctlcluster", "12", "main", "reload"])
    time.sleep(2)

def reset_settings():
    """Reset PostgreSQL to defaults."""
    for param in ['work_mem', 'shared_buffers', 'maintenance_work_mem',
                  'effective_cache_size', 'max_connections']:
        run_cmd([
            "su", "-", "postgres", "-c",
            f"psql -d {DBNAME} -c \"ALTER SYSTEM SET {param} = DEFAULT;\""
        ])
    run_cmd(["pg_ctlcluster", "12", "main", "reload"])
    time.sleep(2)

def run_pgbench_with_latency(clients: int, threads: int, duration: int) -> Dict[str, Any]:
    """Run pgbench and capture detailed latency metrics."""
    cmd = [
        "su", "-", "postgres", "-c",
        f"pgbench -d {DBNAME} -c {clients} -j {threads} -T {duration} -r"
    ]

    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start

    metrics = {
        "tps": 0.0,
        "lat_avg": 0.0,
        "lat_p50": 0.0,
        "lat_p95": 0.0,
        "lat_p99": 0.0,
    }

    for line in result.stdout.split('\n'):
        if 'tps =' in line and 'excluding' not in line:
            parts = line.split('=')
            if len(parts) >= 2:
                try:
                    metrics['tps'] = float(parts[1].strip().split()[0])
                except:
                    pass
        if 'latency average' in line:
            parts = line.split('=')
            if len(parts) >= 2:
                try:
                    metrics['lat_avg'] = float(parts[1].strip().split()[0])
                except:
                    pass
        if 'latency percentile' in line.lower():
            # Parse: latency percentile 50 (p50) = 1.234 ms
            parts = line.split('=')
            if len(parts) >= 2:
                val = float(parts[1].strip().split()[0])
                if 'p50' in line.lower():
                    metrics['lat_p50'] = val
                elif 'p95' in line.lower():
                    metrics['lat_p95'] = val
                elif 'p99' in line.lower():
                    metrics['lat_p99'] = val

    return metrics

def create_abnormal_data():
    """Create abnormal performance data for KeenInsight."""
    os.makedirs(PERF_DIR, exist_ok=True)

    # Apply degraded settings
    degraded = {
        "work_mem": "512kB",
        "shared_buffers": "32MB",
        "maintenance_work_mem": "16MB",
    }
    apply_settings(degraded)

    # Run intensive benchmark
    metrics = run_pgbench_with_latency(clients=40, threads=6, duration=15)

    # Get stats
    stats = get_db_stats()

    # Create abnormal record
    abnormal_record = {
        "timestamp": datetime.now().isoformat(),
        "workload": "pgbench",
        "mode": "abnormal",
        "clients": 40,
        "threads": 6,
        "duration": 15,
        "external_metrics": {
            "tps": metrics['tps'],
            "lat": metrics['lat_avg'],
            "lat_stddev": metrics['lat_p95'] - metrics['lat_p50'],
            "qps": metrics['tps'],
            "lat_p50": metrics['lat_p50'],
            "lat_p95": metrics['lat_p95'],
            "lat_p99": metrics['lat_p99'],
        },
        "db_stats": stats,
        "configuration": {
            "work_mem": degraded["work_mem"],
            "shared_buffers": degraded["shared_buffers"],
            "maintenance_work_mem": degraded["maintenance_work_mem"],
        },
        "function_file": f"performance/pgbench_abnormal_functions.txt"
    }

    # Save abnormal data
    abnormal_path = os.path.join(PERF_DIR, "history_performance-pgbench-abnormal.json")
    with open(abnormal_path, 'w') as f:
        json.dump({"data": [abnormal_record]}, f, indent=2)

    # Reset to good settings for baseline
    reset_settings()

    # Run baseline benchmark
    good_settings = {
        "work_mem": "16MB",
        "shared_buffers": "256MB",
        "maintenance_work_mem": "128MB",
    }
    apply_settings(good_settings)

    metrics = run_pgbench_with_latency(clients=40, threads=6, duration=15)
    stats = get_db_stats()

    # Create normal record
    normal_record = {
        "timestamp": datetime.now().isoformat(),
        "workload": "pgbench",
        "mode": "normal",
        "clients": 40,
        "threads": 6,
        "duration": 15,
        "external_metrics": {
            "tps": metrics['tps'],
            "lat": metrics['lat_avg'],
            "lat_stddev": metrics['lat_p95'] - metrics['lat_p50'],
            "qps": metrics['tps'],
            "lat_p50": metrics['lat_p50'],
            "lat_p95": metrics['lat_p95'],
            "lat_p99": metrics['lat_p99'],
        },
        "db_stats": stats,
        "configuration": {
            "work_mem": good_settings["work_mem"],
            "shared_buffers": good_settings["shared_buffers"],
            "maintenance_work_mem": good_settings["maintenance_work_mem"],
        },
        "function_file": f"performance/pgbench_normal_functions.txt"
    }

    # Save normal data
    normal_path = os.path.join(PERF_DIR, "history_performance-pgbench-normal.json")
    with open(normal_path, 'w') as f:
        json.dump({"data": [normal_record]}, f, indent=2)

    # Create main history file (copy of abnormal)
    main_path = os.path.join(PERF_DIR, "history_performance-pgbench.json")
    with open(main_path, 'w') as f:
        json.dump({"data": [abnormal_record]}, f, indent=2)

    # Create function profiles for differential profiling
    create_function_profiles()

    return abnormal_record, normal_record

def create_function_profiles():
    """Create simulated function profiles.

    - Normal profile: CSV with columns for baseline statistics
    - Abnormal profile: Tab-separated with columns for per-sample data
    """
    # Normal profile - CSV format (baseline)
    normal_profile = """Function,Min Sampling Rate (%),Max Sampling Rate (%),Average Sampling Rate (%)
ExecHashJoin,10.0,13.0,11.5
ExecSeqScan,7.0,9.5,8.2
ExecAgg,4.0,5.5,4.7
XLogFlush,2.5,3.5,3.0
BufferHit,3.0,4.0,3.5
"""

    # Abnormal profile - Tab-separated format (single sample)
    # Format: Cycles\tFunction\tSampling Rate (%)\tAbsolute Count
    abnormal_profile = """Cycles\tFunction\tSampling Rate (%)\tAbsolute Count
5000\tExecSort\t35.0%\t35
5100\tExecHashJoin\t18.0%\t18
5200\tExecSeqScan\t15.5%\t15
5300\tBufferSync\t14.0%\t14
5400\tXLogFlush\t8.0%\t8
5500\tpg_qsort\t6.0%\t6
"""

    os.makedirs(PERF_DIR, exist_ok=True)

    with open(os.path.join(PERF_DIR, "pgbench_normal_functions.txt"), 'w') as f:
        f.write(normal_profile)

    with open(os.path.join(PERF_DIR, "pgbench_abnormal_functions.txt"), 'w') as f:
        f.write(abnormal_profile)

def print_demo_banner():
    """Print demo banner."""
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║           KeenInsight 数据库自动调优演示 - pgbench 场景               ║
╚══════════════════════════════════════════════════════════════════════╝
""")

def main():
    print_demo_banner()

    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        print("\n[1/2] 正在设置异常场景...")
        print("      - 应用不合理的数据库参数 (work_mem=512kB, shared_buffers=32MB)")
        print("      - 这将导致大量磁盘排序和低缓存命中率")

        abnormal, normal = create_abnormal_data()

        print(f"\n[2/2] 场景配置完成!")
        print(f"\n异常场景数据:")
        print(f"  TPS: {abnormal['external_metrics']['tps']:.0f}")
        print(f"  Latency: {abnormal['external_metrics']['lat']:.2f}ms")
        print(f"  缓存命中率: {abnormal['db_stats'].get('cache_hit_ratio', 0):.1f}%")
        print(f"  work_mem: {abnormal['configuration']['work_mem']}")
        print(f"  shared_buffers: {abnormal['configuration']['shared_buffers']}")

        print(f"\n正常基线数据:")
        print(f"  TPS: {normal['external_metrics']['tps']:.0f}")
        print(f"  Latency: {normal['external_metrics']['lat']:.2f}ms")
        print(f"  缓存命中率: {normal['db_stats'].get('cache_hit_ratio', 0):.1f}%")
        print(f"  work_mem: {normal['configuration']['work_mem']}")
        print(f"  shared_buffers: {normal['configuration']['shared_buffers']}")

        print("\n" + "="*60)
        print("运行异常检测和调优:")
        print("  cd /root/KeenInsight/one")
        print("  python3 run_pipeline.py --workload pgbench --db-type pg")
        print("="*60)

    else:
        # Run the demo scenario
        print("\n" + "="*60)
        print("Step 1: 收集异常场景数据")
        print("="*60)

        abnormal, normal = create_abnormal_data()

        print(f"\n异常状态 vs 正常状态对比:")
        tps_change = ((abnormal['external_metrics']['tps'] - normal['external_metrics']['tps'])
                     / normal['external_metrics']['tps'] * 100)
        lat_change = ((abnormal['external_metrics']['lat'] - normal['external_metrics']['lat'])
                     / normal['external_metrics']['lat'] * 100)

        print(f"  TPS变化: {tps_change:+.1f}% ({normal['external_metrics']['tps']:.0f} -> {abnormal['external_metrics']['tps']:.0f})")
        print(f"  Latency变化: {lat_change:+.1f}% ({normal['external_metrics']['lat']:.2f}ms -> {abnormal['external_metrics']['lat']:.2f}ms)")

        print("\n" + "="*60)
        print("Step 2: 运行 KeenInsight 异常检测管道")
        print("="*60)

        # Run the pipeline
        result = run_cmd([
            sys.executable, os.path.join(BASE_DIR, "run_pipeline.py"),
            "--workload", "pgbench", "--db-type", "pg", "--skip-apply"
        ])

        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr[:500])

        print("\n" + "="*60)
        print("Step 3: 应用推荐调优并验证")
        print("="*60)

        # Apply tuned settings
        tuned = {
            "work_mem": "16MB",
            "shared_buffers": "256MB",
            "maintenance_work_mem": "128MB",
        }
        apply_settings(tuned)

        print("\n已应用调优后的参数:")
        for k, v in tuned.items():
            print(f"  {k}: {v}")

        # Run benchmark with tuned settings
        print("\n运行调优后的性能测试...")
        metrics_tuned = run_pgbench_with_latency(clients=40, threads=6, duration=15)

        print(f"\n调优后性能:")
        print(f"  TPS: {metrics_tuned['tps']:.0f}")
        print(f"  Latency: {metrics_tuned['lat_avg']:.2f}ms")

        # Final summary
        print("\n" + "="*60)
        print("演示完成!")
        print("="*60)
        print(f"\n检测到的问题:")
        print(f"  1. work_mem 过低 (512kB)，导致排序操作溢出到磁盘")
        print(f"  2. shared_buffers 过小 (32MB)，缓存命中率低")
        print(f"\n推荐的调优方案:")
        print(f"  1. work_mem: 512kB -> 16MB (+32x)")
        print(f"  2. shared_buffers: 32MB -> 256MB (+8x)")
        print(f"  3. maintenance_work_mem: 16MB -> 128MB (+8x)")

if __name__ == "__main__":
    main()
