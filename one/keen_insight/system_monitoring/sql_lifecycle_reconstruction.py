""" SQL Lifecycle Reconstruction 接口。"""

from __future__ import annotations

from ..models import MonitoringSnapshot, SQLLifecycle


class SQLLifecycleReconstruction:
    """SQL 生命周期重建器。

    用法：
    1. 把监控消息还原为 SQL 的完整执行链路。
    2. 关联解析、优化、执行、等待、提交等阶段。
    3. 输出面向诊断模块的结构化 SQL 生命周期对象。
    """

    def reconstruct(self, snapshot: MonitoringSnapshot) -> list[SQLLifecycle]:
        """根据监控快照重建 SQL 生命周期。"""
        raise NotImplementedError

    def link_events_to_sql(
        self, sql_id: str, related_records: list[dict[str, object]]
    ) -> SQLLifecycle:
        """把相关事件和函数记录绑定到指定 SQL。"""
        raise NotImplementedError
