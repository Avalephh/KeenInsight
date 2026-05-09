#!/usr/bin/env python3
"""Quick A/B test: run CH-Benchmark under different PG configs, compare results."""

import json, os, subprocess, sys, time

BENCHBASE = "/opt/benchbase/target/benchbase-postgres"
JAR = os.path.join(BENCHBASE, "benchbase.jar")
DBNAME = "benchbase"

def psql(sql):
    return subprocess.run(
        ["su", "-", "postgres", "-c", f'psql -d {DBNAME} -t -c "{sql}"'],
        capture_output=True, text=True).stdout.strip()

def apply_knobs(knobs):
    for k, v in knobs.items():
        psql(f"ALTER SYSTEM SET {k} = '{v}';")
    subprocess.run(["pg_ctlcluster", "12", "main", "restart"],
                   capture_output=True, text=True)
    time.sleep(3)

def run_bench(label, duration=60, terminals=20):
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
    cfg_path = os.path.join(BENCHBASE, "config", "ab_test.xml")
    with open(cfg_path, "w") as f:
        f.write(cfg)

    print(f"\n  Running [{label}] terminals={terminals} duration={duration}s ...")
    t0 = time.time()
    r = subprocess.run(
        ["java", "-jar", JAR, "--bench", "chbenchmark",
         "--config", cfg_path, "--execute=true"],
        capture_output=True, text=True, cwd=BENCHBASE, timeout=duration+120)
    elapsed = time.time() - t0

    # find latest summary
    rdir = os.path.join(BENCHBASE, "results")
    files = sorted([f for f in os.listdir(rdir) if f.endswith(".summary.json")], reverse=True)
    if not files:
        return {"label": label, "tps": 0, "lat_avg": 0, "lat_p95": 0, "lat_p99": 0}
    with open(os.path.join(rdir, files[0])) as f:
        d = json.load(f)
    lat = d.get("Latency Distribution", {})
    m = {
        "label": label,
        "tps": d.get("Throughput (requests/second)", 0),
        "lat_avg": lat.get("Average Latency (microseconds)", 0) / 1000,
        "lat_p95": lat.get("95th Percentile Latency (microseconds)", 0) / 1000,
        "lat_p99": lat.get("99th Percentile Latency (microseconds)", 0) / 1000,
        "reqs": d.get("Measured Requests", 0),
        "elapsed": elapsed,
    }
    print(f"    TPS={m['tps']:.1f}  AvgLat={m['lat_avg']:.0f}ms  P95={m['lat_p95']:.0f}ms  P99={m['lat_p99']:.0f}ms  reqs={m['reqs']}")
    return m

# ── Configs to test ──────────────────────────────────────────────────────────

DEGRADED = {
    "shared_buffers": "32MB",
    "work_mem": "1MB",
    "maintenance_work_mem": "16MB",
    "effective_cache_size": "512MB",
    "max_wal_size": "256MB",
    "checkpoint_completion_target": "0.5",
    "random_page_cost": "4",
    "effective_io_concurrency": "1",
}

TUNED = {
    "shared_buffers": "2GB",
    "work_mem": "64MB",
    "maintenance_work_mem": "512MB",
    "effective_cache_size": "20GB",
    "max_wal_size": "4GB",
    "checkpoint_completion_target": "0.9",
    "random_page_cost": "1.1",
    "effective_io_concurrency": "200",
}

if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    terms = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    print("="*60)
    print("  A/B Test: Degraded vs Tuned  (CH-Benchmark, scale=10)")
    print("="*60)

    # Test A: Degraded
    print("\n[A] Applying DEGRADED config ...")
    for k,v in DEGRADED.items():
        print(f"    {k} = {v}")
    apply_knobs(DEGRADED)
    a = run_bench("degraded", duration=dur, terminals=terms)

    # Test B: Tuned
    print("\n[B] Applying TUNED config ...")
    for k,v in TUNED.items():
        print(f"    {k} = {v}")
    apply_knobs(TUNED)
    b = run_bench("tuned", duration=dur, terminals=terms)

    # Summary
    def pct(after, before):
        return f"{(after-before)/before*100:+.1f}%" if before else "N/A"

    print("\n" + "="*60)
    print("  RESULTS")
    print("="*60)
    print(f"  {'Metric':<25s} {'Degraded':>12s} {'Tuned':>12s} {'Change':>10s}")
    print(f"  {'-'*60}")
    print(f"  {'Throughput (req/s)':<25s} {a['tps']:>12.1f} {b['tps']:>12.1f} {pct(b['tps'],a['tps']):>10s}")
    print(f"  {'Avg Latency (ms)':<25s} {a['lat_avg']:>12.0f} {b['lat_avg']:>12.0f} {pct(b['lat_avg'],a['lat_avg']):>10s}")
    print(f"  {'P95 Latency (ms)':<25s} {a['lat_p95']:>12.0f} {b['lat_p95']:>12.0f} {pct(b['lat_p95'],a['lat_p95']):>10s}")
    print(f"  {'P99 Latency (ms)':<25s} {a['lat_p99']:>12.0f} {b['lat_p99']:>12.0f} {pct(b['lat_p99'],a['lat_p99']):>10s}")
    print(f"  {'Total Requests':<25s} {a['reqs']:>12d} {b['reqs']:>12d} {pct(b['reqs'],a['reqs']):>10s}")
    print()
