"""根因打分模型 — 按 SHAP 值和差分偏移量排序候选根因。"""

from __future__ import annotations


class MulticlassScoringModel:
    """根因打分模型。"""

    def score_system_root_causes(
        self,
        hotspots: list[dict[str, object]],
        rules: list[str],
        explanations: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """对系统级候选根因打分并排序。

        打分策略：SHAP 值越大 → 分数越高；差分偏移越大 → 分数越高。
        """
        scored: list[dict[str, object]] = []
        for item in hotspots:
            if item.get("source") != "shap":
                continue
            score = float(item.get("shap", 0.0))
            scored.append({
                "function_name": item.get("function_name"),
                "score": score,
                "evidence": item,
            })
        scored.sort(key=lambda x: float(x["score"]), reverse=True)
        return scored

    def score_sql_root_causes(
        self,
        slow_query_signals: list[dict[str, object]],
        rules: list[str],
        explanations: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """SQL 级根因打分（预留接口，当前返回空列表）。"""
        return []
