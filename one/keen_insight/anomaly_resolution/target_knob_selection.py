"""系统级目标参数选择 — 从根因定位结果中提取候选 knob。"""

from __future__ import annotations

from ..models import DiagnosisResult, KnowledgeEntry


class TargetKnobSelection:
    """系统级目标参数选择器。"""

    def select_targets(self, diagnosis: DiagnosisResult) -> list[str]:
        """从 DiagnosisResult.root_causes 中提取候选 knob 列表。"""
        return list(diagnosis.root_causes)

    def rank_targets(
        self,
        candidate_knobs: list[str],
        knowledge: list[KnowledgeEntry],
    ) -> list[str]:
        """对候选 knob 排序（当前按字母序，知识库有内容时可扩展）。"""
        return sorted(set(candidate_knobs))
