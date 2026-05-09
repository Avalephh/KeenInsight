"""SQL 级知识检索接口。"""

from __future__ import annotations

from ..models import DiagnosisResult, KnowledgeEntry


class SQLKnowledgeRetrieval:
    """SQL 级知识检索器。

    用法：
    1. 检索与 SQL 根因相关的优化经验和文档知识。
    2. 支持面向连接顺序、索引、谓词改写等方向的召回。
    3. 为 SQL 优化器和校验器提供上下文。
    """

    def retrieve(self, diagnosis: DiagnosisResult) -> list[KnowledgeEntry]:
        """检索 SQL 级处置知识。"""
        raise NotImplementedError

    def summarize_retrieved_knowledge(
        self, entries: list[KnowledgeEntry]
    ) -> list[KnowledgeEntry]:
        """摘要 SQL 级检索结果。"""
        raise NotImplementedError
