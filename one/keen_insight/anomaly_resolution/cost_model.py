"""处置成本评估 — 简单启发式实现。"""

from __future__ import annotations

from ..models import ResolutionPlan


class CostModel:
    """成本模型 — 按动作数量估算成本。"""

    def estimate_system_plan(self, plan: ResolutionPlan) -> float:
        """评估系统级方案成本（动作越多成本越高）。"""
        return float(len(plan.actions))

    def estimate_sql_plan(self, plan: ResolutionPlan) -> float:
        """评估 SQL 级方案成本（动作越多成本越高）。"""
        return float(len(plan.actions))
