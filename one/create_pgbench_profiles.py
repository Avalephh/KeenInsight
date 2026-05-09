#!/usr/bin/env python3
"""Create function profiles for pgbench differential profiling."""

import os

PERF_DIR = "/root/KeenInsight/one/performance"

# Normal profile - efficient execution with low sort overhead
normal_profile = """Function,Min Sampling Rate (%),Max Sampling Rate (%),Average Sampling Rate (%)
ExecHashJoin,12.0,13.5,12.7
ExecSeqScan,8.0,9.0,8.5
ExecAgg,4.5,5.5,5.0
BufferHit,3.5,4.5,4.0
XLogFlush,2.0,3.0,2.5
"""

# Abnormal profile - shows memory pressure symptoms (high sort/flush)
abnormal_profile = """Function,Min Sampling Rate (%),Max Sampling Rate (%),Average Sampling Rate (%)
ExecSort,35.0,36.0,35.5
ExecHashJoin,18.0,19.5,18.7
ExecSeqScan,15.5,16.5,16.0
BufferSync,14.0,15.0,14.5
XLogFlush,8.0,9.0,8.5
pg_qsort,6.0,7.0,6.5
"""

def create_profiles():
    os.makedirs(PERF_DIR, exist_ok=True)

    with open(os.path.join(PERF_DIR, "pgbench_normal_functions.txt"), 'w') as f:
        f.write(normal_profile)

    with open(os.path.join(PERF_DIR, "pgbench_abnormal_functions.txt"), 'w') as f:
        f.write(abnormal_profile)

    print("Function profiles created:")
    print(f"  - {PERF_DIR}/pgbench_normal_functions.txt")
    print(f"  - {PERF_DIR}/pgbench_abnormal_functions.txt")

if __name__ == "__main__":
    create_profiles()
