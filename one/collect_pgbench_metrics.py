#!/usr/bin/env python3
"""Collect pgbench metrics and save to KeenInsight format."""

import json
import subprocess
import time
import re
import os
import sys
from datetime import datetime
from typing import Dict, Any, List

class PgbenchMetricsCollector:
    def __init__(self, dbname="sbtest", scale=10):
        self.dbname = dbname
        self.scale = scale
        self.results: List[Dict[str, Any]] = []
    
    def run_pgbench(self, clients: int, threads: int, duration: int, mode: str = "normal") -> Dict[str, Any]:
        """Run pgbench and parse results."""
        # Run as postgres user to use peer authentication
        cmd = [
            "su", "-", "postgres", "-c",
            f"pgbench -d {self.dbname} -c {clients} -j {threads} -T {duration}"
        ]

        print(f"Running: {' '.join(cmd)}")
        start = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.time() - start

        # Print last 30 lines of output for debugging
        lines = result.stdout.strip().split('\n')
        print(f"  pgbench output (last 20 lines):")
        for line in lines[-20:]:
            print(f"    {line}")

        return self._parse_pgbench_output(result.stdout, result.stderr, elapsed, mode)
    
    def _parse_pgbench_output(self, stdout: str, stderr: str, elapsed: float, mode: str) -> Dict[str, Any]:
        """Parse pgbench output to extract metrics."""
        metrics = {
            "mode": mode,
            "elapsed_time": elapsed,
            "tps": 0.0,
            "lat_avg": 0.0,
            "lat_stddev": 0.0,
            "qps": 0.0,
        }

        # Parse TPS from "tps = ..." (second line, excluding connections establishing)
        tps_match = re.search(r'tps\s*=\s*([\d.]+)\s*\(excluding connections', stdout)
        if not tps_match:
            tps_match = re.search(r'tps\s*=\s*([\d.]+)', stdout)
        if tps_match:
            metrics["tps"] = float(tps_match.group(1))

        # Parse latency average
        lat_match = re.search(r'latency average\s*=\s*([\d.]+)\s*ms', stdout, re.IGNORECASE)
        if lat_match:
            metrics["lat_avg"] = float(lat_match.group(1))

        # Calculate QPS (approximate as TPS for read-write)
        metrics["qps"] = metrics["tps"]

        return metrics
    
    def get_db_knobs(self) -> Dict[str, Any]:
        """Get current PostgreSQL configuration."""
        import psycopg2

        # Use local socket with peer authentication
        conn = psycopg2.connect(
            host="/var/run/postgresql",
            port=5432,
            dbname=self.dbname,
            user="postgres"
        )
        cursor = conn.cursor()
        
        knobs = {}
        key_knobs = [
            'max_connections', 'shared_buffers', 'effective_cache_size',
            'work_mem', 'maintenance_work_mem', 'checkpoint_timeout',
            'max_wal_size', 'wal_buffers', 'effective_io_concurrency'
        ]
        
        for knob in key_knobs:
            cursor.execute(f"SHOW {knob}")
            result = cursor.fetchone()
            if result:
                knobs[knob] = result[0]
        
        cursor.close()
        conn.close()
        return knobs
    
    def collect_and_save(self, mode: str, clients: int, threads: int, duration: int, 
                         output_path: str, baseline_path: str = None):
        """Run benchmark and save results."""
        print(f"\n{'='*60}")
        print(f"Collecting {mode.upper()} performance data")
        print(f"{'='*60}")
        
        metrics = self.run_pgbench(clients, threads, duration, mode)
        knobs = self.get_db_knobs()
        
        record = {
            "timestamp": datetime.now().isoformat(),
            "workload": "pgbench",
            "mode": mode,
            "clients": clients,
            "threads": threads,
            "duration": duration,
            "external_metrics": {
                "tps": metrics["tps"],
                "lat": metrics["lat_avg"],
                "lat_stddev": metrics["lat_stddev"],
                "qps": metrics["qps"],
            },
            "configuration": knobs,
            "function_file": f"performance/pgbench_{mode}_functions.txt"
        }
        
        self.results.append(record)
        
        # Save to file
        data = {"data": self.results}
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"\nResults saved to: {output_path}")
        print(f"  TPS: {metrics['tps']:.2f}")
        print(f"  Latency: {metrics['lat_avg']:.2f} ms")
        print(f"  Knobs: {len(knobs)}")
        
        return record


def main():
    collector = PgbenchMetricsCollector(dbname="sbtest", scale=10)

    # Create performance directory
    perf_dir = "/root/KeenInsight/one/performance"
    os.makedirs(perf_dir, exist_ok=True)

    if len(sys.argv) > 1 and sys.argv[1] == "abnormal":
        # Collect abnormal scenario data
        print("\n" + "="*60)
        print("COLLECTING ABNORMAL SCENARIO DATA")
        print("="*60)

        # First collect baseline (normal) data
        normal_output = os.path.join(perf_dir, "history_performance-pgbench.json")
        if not os.path.exists(normal_output):
            collector.collect_and_save(
                mode="normal",
                clients=5,
                threads=2,
                duration=20,
                output_path=normal_output
            )

        # Collect abnormal data with high load
        # This simulates a scenario where:
        # - Many concurrent connections cause contention
        # - Low work_mem causes disk spills
        # - High latency due to memory pressure

        # First, reduce work_mem to simulate memory pressure
        print("\n[SETUP] Reducing work_mem to simulate memory pressure...")
        subprocess.run([
            "su", "-", "postgres", "-c",
            "psql -d sbtest -c \"ALTER SYSTEM SET work_mem = '1MB';\""
        ], capture_output=True)
        subprocess.run(["pg_ctlcluster", "12", "main", "reload"], capture_output=True)
        time.sleep(2)

        # Now run high-load benchmark
        print("\n[ABNORMAL] Running high-load benchmark...")
        abnormal_output = os.path.join(perf_dir, "history_performance-pgbench-abnormal.json")
        record = collector.collect_and_save(
            mode="abnormal",
            clients=50,
            threads=8,
            duration=20,
            output_path=abnormal_output
        )

        print("\n" + "="*60)
        print("ABNORMAL SCENARIO READY")
        print("="*60)
        print(f"Output file: {abnormal_output}")
        print(f"TPS: {record['external_metrics']['tps']:.2f}")
        print(f"Latency: {record['external_metrics']['lat']:.2f} ms")

        # Restore work_mem
        print("\n[CLEANUP] Restoring work_mem...")
        subprocess.run([
            "su", "-", "postgres", "-c",
            "psql -d sbtest -c \"ALTER SYSTEM SET work_mem = '5MB';\""
        ], capture_output=True)
        subprocess.run(["pg_ctlcluster", "12", "main", "reload"], capture_output=True)

    elif len(sys.argv) > 1 and sys.argv[1] == "create-baseline":
        # Create baseline data for both normal and abnormal
        print("\n" + "="*60)
        print("CREATING BASELINE DATA")
        print("="*60)

        # Normal baseline
        print("\n[1/2] Collecting NORMAL baseline...")
        collector.collect_and_save(
            mode="normal",
            clients=5,
            threads=2,
            duration=20,
            output_path=os.path.join(perf_dir, "history_performance-pgbench-normal.json")
        )

        # Abnormal baseline
        print("\n[2/2] Collecting ABNORMAL baseline...")

        # Reduce work_mem
        subprocess.run([
            "su", "-", "postgres", "-c",
            "psql -d sbtest -c \"ALTER SYSTEM SET work_mem = '1MB';\""
        ], capture_output=True)
        subprocess.run(["pg_ctlcluster", "12", "main", "reload"], capture_output=True)
        time.sleep(2)

        collector.collect_and_save(
            mode="abnormal",
            clients=50,
            threads=8,
            duration=20,
            output_path=os.path.join(perf_dir, "history_performance-pgbench-abnormal.json")
        )

        # Restore
        subprocess.run([
            "su", "-", "postgres", "-c",
            "psql -d sbtest -c \"ALTER SYSTEM SET work_mem = '5MB';\""
        ], capture_output=True)
        subprocess.run(["pg_ctlcluster", "12", "main", "reload"], capture_output=True)

        print("\n" + "="*60)
        print("BASELINE DATA CREATED")
        print("="*60)

    else:
        # Collect normal baseline data
        output_path = os.path.join(perf_dir, "history_performance-pgbench.json")
        record = collector.collect_and_save(
            mode="normal",
            clients=5,
            threads=2,
            duration=30,
            output_path=output_path
        )

        print("\n" + "="*60)
        print("NORMAL BASELINE DATA COLLECTED")
        print("="*60)
        print(f"Output file: {output_path}")


if __name__ == "__main__":
    main()
