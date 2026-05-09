"""结构化知识表示构建接口。"""

from __future__ import annotations

from ..models import KnowledgeEntry


class StructuredRepresentation:
    """结构化知识表示构建器。

    用法：
    1. 将异构知识统一为论文框架需要的结构化表示。
    2. 支持按根因、动作、收益、验证方式等维度组织知识。
    3. 作为知识库对外服务前的最终落地层。
    """

    def build(self, entries: list[KnowledgeEntry]) -> list[KnowledgeEntry]:
        """构建结构化知识表示。"""
        raise NotImplementedError

    def link_related_entries(
        self, entries: list[KnowledgeEntry]
    ) -> list[KnowledgeEntry]:
        """将相关知识条目建立显式关联。"""
        raise NotImplementedError
