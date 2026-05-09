import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import (
    MODEL_OLTP,
    MAPPING_OLTP,
    STATIC_LIB_FILE,
    PERF_OUTPUT_DIR,
    HISTORY_PERF_SYSBENCH,
    NORMAL_SYSBENCH,
)
from ShapModel import SHAPModel
from DifferentialPofiling import DifferentialProfiling
from StaticAnalysis import StaticAnalysis


def shap_to_knobs_demo():
    model = SHAPModel(
        model_path=MODEL_OLTP,
        mapping_json_path=MAPPING_OLTP,
        static_lib_path=STATIC_LIB_FILE,
        txt_folder=PERF_OUTPUT_DIR,
    )

    system_features = model.explain_system_features({
        "json_path": HISTORY_PERF_SYSBENCH,
        "top_k": 60
    })

    function_list = [
        item["function_name"]
        for item in system_features
        if item.get("function_name") is not None
    ]

    sa = StaticAnalysis(STATIC_LIB_FILE)
    result = sa.analyze_functions(function_list)

    print("=== Function -> Knobs ===")
    for func, knobs in result["function_to_knobs"].items():
        print(func, "=>", knobs)

    print("\n=== Knob -> Matched Functions ===")
    for item in result["matched_knobs"]:
        print(item)


def diff_to_knobs_demo():
    profiler = DifferentialProfiling()

    baseline_profile = {"function_file": NORMAL_SYSBENCH}
    abnormal_profile = {
        "function_file": os.path.join(
            PERF_OUTPUT_DIR, "perf_1776398000_counts_sysbench.txt"
        )
    }

    diff = profiler.compare_profiles(baseline_profile, abnormal_profile)
    top_features = profiler.rank_changed_features(diff)

    diff_function_list = [
        item["function_name"]
        for item in top_features
        if item.get("function_name") is not None
    ]

    sa = StaticAnalysis(STATIC_LIB_FILE)
    result = sa.analyze_functions(diff_function_list)

    print("=== Function -> Knobs ===")
    for func, knobs in result["function_to_knobs"].items():
        print(func, "=>", knobs)

    print("\n=== Knob -> Matched Functions ===")
    for item in result["matched_knobs"]:
        print(item)


if __name__ == "__main__":
    # shap_to_knobs_demo()
    diff_to_knobs_demo()
