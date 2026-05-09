"""工作负载模式摘要接口。"""

from __future__ import annotations


class Summarization:
    """工作负载模式摘要器。

    用法：
    1. 从历史记录中提炼出工作负载模式、根因模式和动作模式。
    2. 将原始经验压缩为更易检索的摘要知识。
    3. 为聚类分析和经验库构建提供标准输入。
    """

    def summarize_patterns(
        self, analyzed_records: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """生成工作负载与调优模式摘要。"""
        raise NotImplementedError

    def build_gain_profiles(
        self, pattern_summaries: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """构建收益画像。"""
        raise NotImplementedError
