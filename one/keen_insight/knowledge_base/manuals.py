"""手册知识仓库接口。"""

from __future__ import annotations

from ..models import KnowledgeEntry, KnowledgeQuery


class Manuals:
    """手册知识仓库。

    用法：
    1. 保存数据库文档、系统手册和调优说明。
    2. 提供语义化检索能力，补充历史经验之外的显式知识。
    3. 在异常处置阶段用于生成解释性更强的建议。
    """

    def index_manuals(self, manual_paths: list[str]) -> None:
        """建立手册索引。"""
        raise NotImplementedError

    def search(self, query: KnowledgeQuery) -> list[KnowledgeEntry]:
        """检索与问题相关的手册条目。"""
        raise NotImplementedError
