"""LLVM 分析接口。"""

from __future__ import annotations


class LLVMAnalysis:
    """LLVM 分析引擎。

    用法：
    1. 对数据库源码进行 IR 层面的静态分析。
    2. 提取更底层的控制和数据依赖信息。
    3. 为控制流与数据流分析提供统一语义中间层。
    """

    def analyze_ir(
        self, code_units: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """对源码对应的 IR 进行分析。"""
        raise NotImplementedError

    def extract_ir_features(
        self, ir_analysis_results: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """提取 IR 层面的关键特征。"""
        raise NotImplementedError
