"""SQL 优化 — 预留接口，当前返回空方案。"""

from __future__ import annotations

from ..models import DiagnosisResult, KnowledgeEntry, ResolutionPlan


class SQLOptimization:
    """SQL 优化器（预留接口）。"""

    def generate_plan(
        self, diagnosis: DiagnosisResult, knowledge: list[KnowledgeEntry]
    ) -> ResolutionPlan:
        """生成 SQL 优化方案（当前返回空方案）。"""
        return ResolutionPlan(
            plan_type="sql_optimization",
            target="sql",
            actions=[],
            validation_steps=[],
        )

    def rewrite_sql(self, raw_sql: str, strategy: str) -> str:
        """根据指定策略生成 SQL 改写结果（当前原样返回）。"""
        return raw_sql
