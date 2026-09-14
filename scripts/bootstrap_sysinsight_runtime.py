#!/usr/bin/env python3
"""Create local DREAM smoke-test artifacts from the SysInsight TPCC database.

The official DREAM checkpoint and retriever are downloaded from Google Drive by
the upstream instructions.  When that external artifact is unavailable, this
script creates a transparent, randomly initialized checkpoint and a real
runtime CSV from the local database.  It is suitable for exercising the full
code path, but it is not a substitute for the trained DREAM weights.
"""

import csv
import argparse
import json
import sys
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CONFIG_PATH = ROOT / "config" / "sysinsight_tpcc_config.json"
WORKLOAD_PATH = ROOT / "data" / "slow_queries" / "SysInsight-TPCC"


def psql(sql, database):
    command = [
        "runuser",
        "-u",
        "postgres",
        "--",
        "psql",
        "-X",
        "-Atq",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        database,
        "-c",
        sql,
    ]
    result = subprocess.run(command, cwd="/tmp", text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "psql failed")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(
        description="Create local DREAM smoke artifacts without replacing official assets."
    )
    parser.add_argument(
        "--force-smoke",
        action="store_true",
        help="overwrite existing model/data paths with smoke artifacts",
    )
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    database = config["DATABASE_CONFIG"]["dbname"]
    order_path = WORKLOAD_PATH / "qorder.txt"
    query_paths = [
        WORKLOAD_PATH / name.strip()
        for name in order_path.read_text(encoding="utf-8").splitlines()
        if name.strip().endswith(".sql")
    ]

    rows = []
    for query_path in query_paths:
        sql = query_path.read_text(encoding="utf-8").strip()
        plan_output = psql(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}",
            database,
        )
        plan = json.loads(plan_output)
        execution_ms = float(plan[0].get("Execution Time", 0.0))
        execution_s = execution_ms / 1000.0

        # The dimensions match PostgresDB.get_metrics_list() and
        # ResourceMonitor's seven streams x nine samples.  The plan is real;
        # the metric vectors are conservative smoke-test values.
        internal = [0.0] * 13
        internal[6] = 1.0
        internal[12] = execution_s
        external = [[0.0] * 9 for _ in range(7)]
        external[0] = [execution_ms] * 9

        rows.append(
            {
                "sql_file": str(query_path),
                "query": " ".join(sql.split()),
                "plan_json": json.dumps(plan, separators=(",", ":")),
                "internal_metrics": json.dumps(internal),
                "external_metrics": json.dumps(external),
                "multilabel": str([0, 0, 0, 0]),
                "duration": execution_s,
                "error": "",
                "case_label": "positive",
                "tuning_attempts": str([0, 0, 0, 0]),
            }
        )

    planner_config = config["PLANNER_CONFIG"]
    csv_path = Path(planner_config["train_data_path"])
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.exists() and not args.force_smoke:
        print(f"preserving_existing={csv_path}")
    else:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    # Keep the three documented RCRank dataset entry points executable for a
    # local smoke run.  These are repeated SysInsight TPCC samples, not the
    # upstream paper datasets and must not be interpreted as their results.
    rcrank_data_dir = ROOT / "dream" / "agent" / "plan" / "RCRank" / "data"
    rcrank_data_dir.mkdir(parents=True, exist_ok=True)
    smoke_rows = rows * 7
    for dataset_name in ("tpc_c", "tpc_h", "tpc_ds"):
        dataset_path = rcrank_data_dir / f"{dataset_name}.csv"
        if dataset_path.exists() and not args.force_smoke:
            print(f"preserving_existing={dataset_path}")
        else:
            with dataset_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(smoke_rows[0]))
                writer.writeheader()
                writer.writerows(smoke_rows)

    import pandas as pd

    pretrain_rows = []
    for row in rows:
        plan_payload = json.loads(row["plan_json"])
        while isinstance(plan_payload, list) and len(plan_payload) == 1:
            plan_payload = plan_payload[0]
        pretrain_rows.append(
            {
                "query": row["query"],
                "plan_json": json.dumps({"Plan": plan_payload["Plan"]}),
                "log_all": json.loads(row["internal_metrics"]),
            }
        )
    pretrain_path = ROOT / "dream" / "agent" / "plan" / "RCRank" / "pretrain" / "pretrain_data.pkl"
    if pretrain_path.exists() and not args.force_smoke:
        print(f"preserving_existing={pretrain_path}")
    else:
        pd.DataFrame(pretrain_rows).to_pickle(pretrain_path)

    html_input = {}
    for index, row in enumerate(rows, start=1):
        html_input[str(index)] = {
            "query_info": {
                "query_id": str(index),
                "query": row["query"],
                "plan_json": row["plan_json"],
                "internal_metrics": json.loads(row["internal_metrics"]),
                "external_metrics": json.loads(row["external_metrics"]),
                "execution_time": row["duration"],
                "is_rewrite": False,
            }
        }
    html_input_path = ROOT / "results" / "slow_query_list_sysinsight_tpcc.json"
    html_input_path.write_text(json.dumps(html_input, ensure_ascii=False), encoding="utf-8")

    import torch

    model_path = Path(planner_config["model_path"])
    model_path.parent.mkdir(parents=True, exist_ok=True)
    if model_path.exists() and not args.force_smoke:
        print(f"preserving_existing={model_path}")
    else:
        # Deliberately empty: RCRankPredictor loads it with strict=False,
        # leaving the architecture initialized for local chain verification.
        torch.save({}, model_path)

    from dream.agent.memory.soft_q_retriever import SoftQRetriever

    retriever_path = Path(config["MEMORY_MANAGER_CONFIG"]["retriever_model_path"])
    if retriever_path.exists() and not args.force_smoke:
        print(f"preserving_existing={retriever_path}")
    else:
        retriever = SoftQRetriever()
        retriever.save_model(str(retriever_path))

    memory_path = Path(config["MEMORY_MANAGER_CONFIG"]["db_path"])
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    if not memory_path.exists():
        memory_path.write_text("{}", encoding="utf-8")

    print(f"runtime_csv={csv_path}")
    print(f"bootstrap_checkpoint={model_path}")
    print(f"bootstrap_retriever={retriever_path}")
    print(f"rcrank_smoke_rows={len(smoke_rows)}")
    print(f"pretrain_smoke_data={pretrain_path}")
    print(f"html_smoke_data={html_input_path}")
    print(f"queries={len(rows)}")


if __name__ == "__main__":
    main()
