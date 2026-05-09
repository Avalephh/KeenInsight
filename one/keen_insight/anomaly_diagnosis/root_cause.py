"""热点检测 — 融合阈值告警和 SHAP 解释结果。"""

from __future__ import annotations

from ..models import SQLLifecycle


class RootCause:
    """热点检测器。"""

    def detect_system_hotspots(
        self,
        threshold_alerts: list[dict[str, object]],
        feature_explanations: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """将阈值告警和 SHAP 解释合并为热点候选列表。"""
        hotspots: list[dict[str, object]] = []

        # Carry over SHAP-ranked functions as hotspot candidates
        for item in feature_explanations:
            func = item.get("function_name")
            if func:
                hotspots.append({
                    "function_name": func,
                    "shap": item.get("shap", 0.0),
                    "source": "shap",
                })

        # Append threshold violations as additional evidence
        for alert in threshold_alerts:
            hotspots.append({
                "metric": alert.get("metric"),
                "value": alert.get("value"),
                "direction": alert.get("direction"),
                "source": "threshold",
            })

        return hotspots

    def detect_sql_hotspots(
        self, sql_lifecycles: list[SQLLifecycle]
    ) -> list[dict[str, object]]:
        """SQL 级热点检测（预留接口，当前返回空列表）。"""
        return []
