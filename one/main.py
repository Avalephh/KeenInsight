"""KeenInsight 主入口。

运行方式：
    cd /root/KeenInsight/one
    python main.py

流程：
1. 从 performance/history_performance-sysbench.json 读取历史性能数据
2. 构建 MonitoringSnapshot（含 SHAP 输入和差分剖析路径）
3. 执行完整诊断 → 知识检索 → 调优方案生成
4. 打印诊断结论和推荐 knob 调整方案
"""

from __future__ import annotations

import json
import os
import sys

# Ensure project root is on sys.path when run directly
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import (
    HISTORY_PERF_SYSBENCH,
    NORMAL_SYSBENCH,
    PERF_OUTPUT_DIR,
)
from keen_insight.models import MonitoringSnapshot
from keen_insight.pipeline import build_default_pipeline


def _load_history(json_path: str) -> dict:
    with open(json_path) as f:
        return json.load(f)


def _build_snapshot(history: dict) -> MonitoringSnapshot:
    """从历史性能 JSON 构建 MonitoringSnapshot。"""
    records = history.get("data", [])
    if not records:
        raise ValueError(f"No data records found in {HISTORY_PERF_SYSBENCH}")

    latest = records[-1]
    ext = latest.get("external_metrics", {})

    # system_metrics: 用外部指标模拟时序（单点）
    system_metrics: dict[str, list] = {
        "tps": [ext.get("tps", 0.0)],
        "lat": [ext.get("lat", 0.0)],
        "qps": [ext.get("qps", 0.0)],
    }

    # Resolve the perf output file referenced in the history record
    raw_func_file: str = latest.get("function_file", "")
    if not os.path.isabs(raw_func_file):
        raw_func_file = os.path.join(_HERE, raw_func_file)

    db_metrics: dict = {
        "configuration": latest.get("configuration", {}),
        "external_metrics": ext,
        # SHAP input: points to the history JSON itself
        "shap_input": {
            "json_path": HISTORY_PERF_SYSBENCH,
            "top_k": 20,
        },
    }

    # Only add differential profiling paths if the abnormal perf file exists
    if os.path.exists(raw_func_file) and os.path.exists(NORMAL_SYSBENCH):
        db_metrics["baseline_profile"] = {"function_file": NORMAL_SYSBENCH}
        db_metrics["abnormal_profile"] = {"function_file": raw_func_file}

    return MonitoringSnapshot(
        system_metrics=system_metrics,
        db_metrics=db_metrics,
    )


def main() -> None:
    print("=" * 60)
    print("KeenInsight Pipeline")
    print("=" * 60)

    # 1. Load history
    print(f"\n[1] Loading performance history: {HISTORY_PERF_SYSBENCH}")
    history = _load_history(HISTORY_PERF_SYSBENCH)
    records = history.get("data", [])
    print(f"    Found {len(records)} record(s).")

    # 2. Build snapshot
    print("\n[2] Building MonitoringSnapshot ...")
    snapshot = _build_snapshot(history)
    ext = snapshot.db_metrics.get("external_metrics", {})
    print(f"    TPS={ext.get('tps')}, LAT={ext.get('lat')}, QPS={ext.get('qps')}")
    has_diff = "baseline_profile" in snapshot.db_metrics
    print(f"    Differential profiling available: {has_diff}")

    # 3. Build pipeline
    print("\n[3] Building pipeline ...")
    pipeline = build_default_pipeline()

    # 4. Run pipeline
    print("\n[4] Running pipeline ...")
    ctx = pipeline.run_once({
        "snapshot": snapshot,
        "sql_lifecycles": [],
    })

    # 5. Print diagnosis results
    print("\n[5] Diagnosis results:")
    for i, diag in enumerate(ctx.diagnosis):
        print(f"    [{i}] category={diag.category}, confidence={diag.confidence:.2f}")
        print(f"        summary: {diag.summary[:120]}")
        if diag.root_causes:
            print(f"        root_causes ({len(diag.root_causes)}): {diag.root_causes[:5]}")

    # 6. Print resolution plans
    print("\n[6] Resolution plans:")
    for i, plan in enumerate(ctx.resolution_plans):
        print(f"    [{i}] type={plan.plan_type}, target={plan.target[:60]}, cost={plan.estimated_cost}")
        for j, action in enumerate(plan.actions[:5]):
            print(f"        action[{j}]: {action[:100]}")
        if len(plan.actions) > 5:
            print(f"        ... and {len(plan.actions) - 5} more actions")

    print("\n" + "=" * 60)
    print("Pipeline completed successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()
