""" Adaptive Message Generation 接口。"""

from __future__ import annotations


class AdaptiveMessageGeneration:
    """自适应消息生成器。

    用法：
    1. 汇总用户态和内核态采集结果，形成统一监控消息。
    2. 根据不同异常场景决定消息粒度与字段。
    3. 为 SQL 生命周期重建和后续诊断提供标准输入。
    """

    def generate_messages(
        self,
        db_records: list[dict[str, object]],
        kernel_records: list[dict[str, object]],
    ) -> list[str]:
        """生成标准化监控消息。"""
        raise NotImplementedError

    def compress_messages(self, messages: list[str]) -> list[str]:
        """对消息做聚合或压缩。"""
        raise NotImplementedError
