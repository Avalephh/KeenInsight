"""知识处理流水线接口。"""

from __future__ import annotations

from ..models import KnowledgeEntry


class ProcessingPipeline:
    """知识处理流水线。

    用法：
    1. 清洗来自历史记录、源码和手册的异构知识。
    2. 做去重、归一化、标签化和证据增强。
    3. 为结构化知识表示构建统一输入。
    """

    def normalize_entries(
        self, entries: list[KnowledgeEntry]
    ) -> list[KnowledgeEntry]:
        """对知识条目做归一化处理。"""
        raise NotImplementedError

    def enrich_entries(
        self, entries: list[KnowledgeEntry]
    ) -> list[KnowledgeEntry]:
        """补充标签、元数据和上下文证据。"""
        raise NotImplementedError
