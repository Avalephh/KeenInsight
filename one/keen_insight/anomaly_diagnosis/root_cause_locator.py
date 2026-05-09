"""根因定位 — 融合 SHAP 和差分剖析结果，输出 DiagnosisResult。"""

from __future__ import annotations

import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import STATIC_LIB_FILE  # noqa: E402
from StaticAnalysis import StaticAnalysis as _SA  # noqa: E402
from ..models import DiagnosisResult


class RootCauseLocator:
    """根因定位器。

    融合 SHAP 函数重要性排名和差分剖析结果，通过静态分析库
    将热点函数映射到数据库 knob，输出结构化根因结论。
    """

    def __init__(self, static_lib_path: str | None = None) -> None:
        path = static_lib_path or STATIC_LIB_FILE
        self._sa = _SA(path)

    def locate_system_root_cause(
        self, scored_candidates: list[dict[str, object]]
    ) -> DiagnosisResult:
        """确定系统级根因。

        scored_candidates: 每条记录至少包含 'function_name' 字段，
        可选 'shap'/'diff_from_mean' 作为重要性分数。
        """
        function_names = [
            str(c["function_name"])
            for c in scored_candidates
            if c.get("function_name")
        ]

        analysis = self._sa.analyze_functions(function_names)
        matched_knobs: list[dict] = analysis.get("matched_knobs", [])
        func_to_knobs: dict = analysis.get("function_to_knobs", {})

        # Build a concise summary
        knob_names = [m["knob_name"] for m in matched_knobs if m.get("knob_name")]
        top_funcs = function_names[:5]

        summary = (
            f"Top anomalous functions: {top_funcs}. "
            f"Related knobs: {knob_names[:10]}."
        )

        return DiagnosisResult(
            category="system",
            summary=summary,
            root_causes=knob_names,
            confidence=1.0 if knob_names else 0.0,
            evidence={
                "top_functions": top_funcs,
                "function_to_knobs": func_to_knobs,
                "matched_knobs": matched_knobs,
            },
        )

    def locate_sql_root_cause(
        self, scored_candidates: list[dict[str, object]]
    ) -> DiagnosisResult:
        """确定 SQL 级根因（当前返回空结论，预留接口）。"""
        return DiagnosisResult(
            category="sql",
            summary="SQL-level root cause analysis not yet implemented.",
            root_causes=[],
            confidence=0.0,
        )
