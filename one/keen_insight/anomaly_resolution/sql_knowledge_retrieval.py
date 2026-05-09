"""SQL 级知识检索 — 预留接口，当前返回空列表。"""

from __future__ import annotations

from ..models import DiagnosisResult, KnowledgeEntry


class SQLKnowledgeRetrieval:
    """SQL 级知识检索器（预留接口）。"""

    def retrieve(self, diagnosis: DiagnosisResult) -> list[KnowledgeEntry]:
        """检索 SQL 级处置知识（当前知识库为空）。"""
        return []

    def summarize_retrieved_knowledge(
        self, entries: list[KnowledgeEntry]
    ) -> list[KnowledgeEntry]:
        """摘要 SQL 级检索结果（直接透传）。"""
        return entries
