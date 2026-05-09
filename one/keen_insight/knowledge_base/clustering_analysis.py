"""聚类分析接口。"""

from __future__ import annotations

from ..models import DiagnosisResult


class ClusteringAnalysis:
    """聚类分析引擎。

    用法：
    1. 对历史经验进行聚类，形成可复用的场景簇。
    2. 把相似工作负载和相似根因归并到一起。
    3. 为知识检索阶段提供更精准的召回范围。
    """

    def cluster_patterns(
        self, pattern_summaries: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """对经验模式做聚类分析。"""
        raise NotImplementedError

    def assign_cluster(self, diagnosis: DiagnosisResult) -> str:
        """给当前诊断结果分配最相近的经验簇。"""
        raise NotImplementedError
