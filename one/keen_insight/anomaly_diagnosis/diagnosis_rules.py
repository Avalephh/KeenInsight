"""诊断规则引擎 — 基于阈值告警生成规则描述。"""

from __future__ import annotations

from ..models import MonitoringSnapshot, SQLLifecycle


class DiagnosisRules:
    """诊断规则引擎。"""

    def evaluate_system_rules(
        self,
        snapshot: MonitoringSnapshot,
        hotspots: list[dict[str, object]],
    ) -> list[str]:
        """根据热点和快照生成规则描述字符串列表。"""
        rules: list[str] = []
        for h in hotspots:
            if h.get("source") == "threshold":
                metric = h.get("metric", "unknown")
                direction = h.get("direction", "high")
                rules.append(f"THRESHOLD_VIOLATION:{metric}:{direction}")
        return rules

    def evaluate_sql_rules(
        self,
        sql_lifecycles: list[SQLLifecycle],
        slow_query_signals: list[dict[str, object]],
    ) -> list[str]:
        """SQL 级规则判断（预留接口，当前返回空列表）。"""
        return []
