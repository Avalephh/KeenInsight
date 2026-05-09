"""SHAP model 接口 — 委托给顶层 ShapModel 实现。"""

from __future__ import annotations

import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import (  # noqa: E402
    MODEL_OLTP,
    MAPPING_OLTP,
    STATIC_LIB_FILE,
    PERF_OUTPUT_DIR,
    MODEL_BY_WORKLOAD,
)
from ShapModel import SHAPModel as _RealSHAP  # noqa: E402


class SHAPModel:
    """SHAP 解释器 — 委托给 ShapModel.SHAPModel。

    workload: 'sysbench' | 'tpcc' | 'tpch'，决定使用哪套模型文件。
    """

    def __init__(self, workload: str = "sysbench") -> None:
        paths = MODEL_BY_WORKLOAD.get(workload, MODEL_BY_WORKLOAD["sysbench"])
        os.makedirs(PERF_OUTPUT_DIR, exist_ok=True)
        self._impl = _RealSHAP(
            model_path=paths["model"],
            mapping_json_path=paths["mapping"],
            static_lib_path=STATIC_LIB_FILE,
            txt_folder=PERF_OUTPUT_DIR,
        )

    def explain_system_features(
        self, model_input: dict[str, object]
    ) -> list[dict[str, object]]:
        """解释系统级特征的重要性。"""
        return self._impl.explain_system_features(model_input)

    def explain_knob_features(
        self, model_input: dict[str, object]
    ) -> list[dict[str, object]]:
        """解释参数级特征的重要性（函数→knob 映射）。"""
        return self._impl.explain_knob_features(model_input)

    def explain_sql_features(
        self, model_input: dict[str, object]
    ) -> list[dict[str, object]]:
        """SQL 级特征解释（当前返回空列表，预留接口）。"""
        return []
