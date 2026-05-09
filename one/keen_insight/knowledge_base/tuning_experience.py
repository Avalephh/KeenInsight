"""调优经验库接口。"""

from __future__ import annotations

from ..models import KnowledgeEntry, KnowledgeQuery


class TuningExperience:
    """调优经验库。

    用法：
    1. 持久化经过聚类和摘要后的调优经验。
    2. 对外提供面向异常处置的经验检索接口。
    3. 统一管理系统级经验和 SQL 级经验。
    """

    def index_experiences(self, entries: list[KnowledgeEntry]) -> None:
        """建立经验索引。"""
        raise NotImplementedError

    def search(self, query: KnowledgeQuery) -> list[KnowledgeEntry]:
        """检索调优经验。"""
        raise NotImplementedError
