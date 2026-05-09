"""Global path configuration for KeenInsight.

All hardcoded paths in the project should import from here.
Override BASE_DIR to relocate the entire system to a different root.
"""

from __future__ import annotations

import os

# ── Root of the project ──────────────────────────────────────────────────────
# Resolves to the directory containing this file, so the project is portable.
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))

# ── Sub-directories ───────────────────────────────────────────────────────────
DATABASE_DIR: str = os.path.join(BASE_DIR, "database")
PREMODEL_DIR: str = os.path.join(BASE_DIR, "premodel")
PERFORMANCE_DIR: str = os.path.join(BASE_DIR, "performance")
NORMALDATA_DIR: str = os.path.join(DATABASE_DIR, "normaldata")
KNOBSPACE_DIR: str = os.path.join(DATABASE_DIR, "knobspace")

# Output directory for perf collection results (created on demand)
PERF_OUTPUT_DIR: str = os.path.join(BASE_DIR, "test_perf_output")

# FlameGraph tools directory (override via env var FLAMEGRAPH_DIR if needed)
FLAMEGRAPH_DIR: str = os.environ.get(
    "FLAMEGRAPH_DIR", os.path.join(BASE_DIR, "FlameGraph")
)

# ── Key files ─────────────────────────────────────────────────────────────────
KNOB_CONFIG_FILE: str = os.path.join(KNOBSPACE_DIR, "mysql_knobs.json")
STATIC_LIB_FILE: str = os.path.join(DATABASE_DIR, "paramater_association_library.json")
DB_CONFIG_FILE: str = os.path.join(DATABASE_DIR, "config_template.ini")

# Pre-trained models
MODEL_OLTP: str = os.path.join(PREMODEL_DIR, "performance_model_oltp.pkl")
MODEL_OLAP: str = os.path.join(PREMODEL_DIR, "performance_model_olap.pkl")
MAPPING_OLTP: str = os.path.join(PREMODEL_DIR, "function_mapping_oltp.json")
MAPPING_OLAP: str = os.path.join(PREMODEL_DIR, "function_mapping_olap.json")

# Baseline normal-behaviour profiles
NORMAL_SYSBENCH: str = os.path.join(NORMALDATA_DIR, "function_normal_sysbench.csv")
NORMAL_TPCC: str = os.path.join(NORMALDATA_DIR, "function_normal_tpcc.csv")
NORMAL_TPCH: str = os.path.join(NORMALDATA_DIR, "function_normal_tpch.csv")
NORMAL_PGBENCH: str = os.path.join(PERFORMANCE_DIR, "pgbench_normal_functions.txt")
NORMAL_CHBENCH: str = os.path.join(PERFORMANCE_DIR, "chbench_normal_functions.txt")

# Performance history (used by SHAP model as input)
HISTORY_PERF_SYSBENCH: str = os.path.join(
    PERFORMANCE_DIR, "history_performance-sysbench.json"
)
HISTORY_PERF_PGBENCH: str = os.path.join(
    PERFORMANCE_DIR, "history_performance-pgbench.json"
)
HISTORY_PERF_CHBENCH: str = os.path.join(
    PERFORMANCE_DIR, "history_performance-chbenchmark.json"
)

# Workload → baseline file mapping
BASELINE_BY_WORKLOAD: dict[str, str] = {
    "sysbench": NORMAL_SYSBENCH,
    "tpcc": NORMAL_TPCC,
    "tpch": NORMAL_TPCH,
    "pgbench": NORMAL_PGBENCH,
    "chbenchmark": NORMAL_CHBENCH,
}

# Workload → model/mapping file mapping
MODEL_BY_WORKLOAD: dict[str, dict[str, str]] = {
    "sysbench": {"model": MODEL_OLTP, "mapping": MAPPING_OLTP},
    "tpcc": {"model": MODEL_OLTP, "mapping": MAPPING_OLTP},
    "tpch": {"model": MODEL_OLAP, "mapping": MAPPING_OLAP},
    "pgbench": {"model": MODEL_OLTP, "mapping": MAPPING_OLTP},
    "chbenchmark": {"model": MODEL_OLTP, "mapping": MAPPING_OLTP},
}

# ── BenchBase (CH-Benchmark runner) ───────────────────────────────────────────
BENCHBASE_DIR: str = "/opt/benchbase/target/benchbase-postgres"
BENCHBASE_JAR: str = os.path.join(BENCHBASE_DIR, "benchbase.jar")
BENCHBASE_CHBENCH_CFG: str = os.path.join(BENCHBASE_DIR, "config", "chbenchmark_config.xml")
