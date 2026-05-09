""" Kernel Resource Functions 接口。"""

from __future__ import annotations


class KernelResourceFunctions:
    """采集内核资源函数行为。

    用法：
    1. 关注 CPU、内存、磁盘、网络相关资源函数。
    2. 提取系统调用与资源竞争的高价值信息。
    3. 为系统级热点检测提供底层依据。
    """

    def collect_resource_functions(self) -> list[dict[str, object]]:
        """采集内核资源函数记录。"""
        raise NotImplementedError

    def summarize_resource_usage(
        self, function_records: list[dict[str, object]]
    ) -> dict[str, object]:
        """对资源函数记录做摘要。"""
        raise NotImplementedError
