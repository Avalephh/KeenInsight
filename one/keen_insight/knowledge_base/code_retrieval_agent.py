"""代码检索代理接口。"""

from __future__ import annotations

from ..models import DiagnosisResult, KnowledgeEntry


class CodeRetrievalAgent:
    """代码检索代理。

    用法：
    1. 将诊断结果映射到相关的源码位置和执行路径。
    2. 协调源码仓库、LLVM 分析和流分析组件。
    3. 输出可被知识处理流水线使用的代码证据。
    """

    def retrieve_relevant_code(
        self, diagnosis: DiagnosisResult
    ) -> list[dict[str, object]]:
        """检索与当前根因相关的源码片段。"""
        raise NotImplementedError

    def build_code_evidence(
        self, code_units: list[dict[str, object]]
    ) -> list[KnowledgeEntry]:
        """把代码信息转换为知识条目。"""
        raise NotImplementedError
