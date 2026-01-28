import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO)
import asyncio
import csv
import json
from typing import Any, Dict, List

from dream.agent.db_agent import DBAgent
from dream.utils.types import AnomalyInfo, QueryInfo

# Label slow SQL
# python label_slow_sql.py

# Assume root cause list is consistent with original
ROOT_CAUSES = [
    "under-optimized join order",
    "inappropriate knob settings",
    "missing indexes",
    "repeatedly executing subqueries",
    "complex table joins",
    "poorly written queries",
]

# Use relative path based on project root directory
project_root = Path(__file__).parent.parent
INPUT_CSV = str(project_root / "test_sql_gen" / "tpch_detailed1.csv")  # Input file name
OUTPUT_CSV = "slow_sql_labeled.csv"  # Output file name


# Read slow SQL
def read_slow_sqls(csv_path: str) -> List[Dict[str, Any]]:
    sqls = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sqls.append(row)
    return sqls


async def label_slow_sqls():
    agent = DBAgent()
    slow_sqls = read_slow_sqls(INPUT_CSV)
    # Open CSV and write header
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["query", "opt_rate_label"])
        writer.writeheader()
        for idx, sql_row in enumerate(slow_sqls):
            print(f"Processing {idx+1}/{len(slow_sqls)} slow SQL...")
            time_ratios = []
            for cause_id, cause in enumerate(ROOT_CAUSES):
                print(f"Processing {idx+1}/{len(slow_sqls)} slow SQL, assuming root cause: {cause}...")
                # Construct query_info/anomaly_info, assume field names are consistent with main process
                query_info = {
                    "query_id": sql_row.get("index_x", str(idx)),
                    "query": sql_row["query"],
                    "query_plan": sql_row.get("plan_json", ""),
                    "query_kpis": sql_row.get("timeseries", ""),
                    "log_all": sql_row.get("log_all", ""),
                    "execution_time": float(sql_row.get("duration", 0)),
                }

                # Deserialize timeseries field
                try:
                    kpis_list = json.loads(query_info["query_kpis"])
                except Exception as e:
                    print(f"Failed to parse timeseries: {query_info['query_kpis']}, error: {e}")
                    kpis_list = [[0.0], [0.0], [0.0], [0.0]]

                metrics = {
                    "cpu_usage": kpis_list[0] if len(kpis_list) > 0 else [],
                    "memory_usage": kpis_list[1] if len(kpis_list) > 1 else [],
                    "io_wait": kpis_list[2] if len(kpis_list) > 2 else [],
                    "network_traffic": kpis_list[3] if len(kpis_list) > 3 else [],
                }
                # Parse log information
                try:
                    log_all = json.loads(query_info["log_all"])
                except Exception as e:
                    print(f"Failed to parse log_all: {query_info['log_all']}, error: {e}")
                    log_all = [0, 0, 0]
                log_info = {
                    "read_rows": log_all[0] if len(log_all) > 0 else 0,
                    "write_rows": log_all[1] if len(log_all) > 1 else 0,
                    "scan_rows": log_all[2] if len(log_all) > 2 else 0,
                }

                kpi_descriptions = {}
                for metric_name, values in metrics.items():
                    avg_value = sum(values) / len(values) if values else 0
                    kpi_descriptions[metric_name] = f"avg_value: {avg_value:.2f}"
                anomaly_info = {
                    "kpis": metrics,
                    "kpi_descriptions": kpi_descriptions,
                    "log_info": log_info,
                }

                # Force specify root cause here
                analysis_result = {
                    "root_cause": cause,
                }

                # Construct input objects for type checking
                query = QueryInfo(
                    query_id=query_info["query_id"],
                    query=query_info["query"],
                    query_plan=query_info["query_plan"],
                    query_kpis=query_info["query_kpis"],
                    log_all=query_info["log_all"],
                    execution_time=query_info["execution_time"],
                )
                anomaly = AnomalyInfo(
                    kpis=anomaly_info["kpis"],
                    kpi_descriptions=anomaly_info["kpi_descriptions"],
                    log_info=anomaly_info["log_info"],
                )

                # action_manager generates fix SQL
                historical_data = []
                action_results = await agent.action_manager.analyze_and_act(analysis_result, query, anomaly, historical_data)
                diagnosis = action_results.get("diagnosis", "")
                sql_fixes = agent.extract_sql_fix(diagnosis)
                if not sql_fixes:
                    time_ratio = 0.0  # Cannot fix, optimization rate is 0
                else:
                    # Evaluate fix effectiveness
                    evaluation = await agent.planner.evaluate_action(sql_fixes, cause, query, simulation_mode=True)
                    msg = evaluation[1]
                    print(f"msg: {msg}")
                    import re

                    match = re.search(r"耗时由([0-9.]+)s降至([0-9.]+)s", msg)
                    if match:
                        orig_time = float(match.group(1))
                        fixed_time = float(match.group(2))
                        print(f"orig_time: {orig_time}, fixed_time: {fixed_time}")
                    else:
                        match = re.search(r"原耗时([0-9.]+)s，新耗时([0-9.]+)s", msg)
                        if match:
                            orig_time = float(match.group(1))
                            fixed_time = float(match.group(2))
                            print(f"orig_time: {orig_time}, fixed_time: {fixed_time}")
                        else:
                            orig_time, fixed_time = 1.0, 1.0
                            print(f"orig_time: {orig_time}, fixed_time: {fixed_time}")
                    if orig_time > 0:
                        time_ratio = (orig_time - fixed_time) / orig_time
                    else:
                        time_ratio = 0.0
                time_ratios.append(round(time_ratio, 4))
            # Write record immediately
            writer.writerow({"query": sql_row["query"], "opt_rate_label": str(time_ratios)})
            print(f"Written: query={sql_row['query'][:50]}..., opt_rate_label={time_ratios}")
    await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(label_slow_sqls())
