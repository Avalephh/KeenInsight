"""慢查询预测 — 预留接口，当前返回空结果。"""

from __future__ import annotations

from ..models import SQLLifecycle


class SlowQueryPrediction:
    """慢查询预测器（预留接口）。"""

    def predict(
        self, sql_lifecycles: list[SQLLifecycle]
    ) -> list[dict[str, object]]:
        """预测慢查询风险（当前返回空列表）。"""
        return []

    def extract_sql_features(
        self, sql_lifecycles: list[SQLLifecycle]
    ) -> list[dict[str, object]]:
        """提取 SQL 级诊断特征（当前返回空列表）。"""
        return []
