"""历史调优记录管理接口。"""

from __future__ import annotations


class HistoricalTuningRecords:
    """历史调优记录管理器。

    用法：
    1. 存储过去的工作负载、根因、动作和收益记录。
    2. 为经验复用和聚类分析提供原始样本。
    3. 支持按场景、故障类型和收益进行检索。
    """

    def load_records(self) -> list[dict[str, object]]:
        """加载历史调优记录。"""
        raise NotImplementedError

    def save_record(self, record: dict[str, object]) -> None:
        """保存一条新的调优记录。"""
        raise NotImplementedError
