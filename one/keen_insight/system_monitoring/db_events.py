""" DB Events 接口。"""

from __future__ import annotations


class DBEvents:
    """采集数据库事件。

    用法：
    1. 获取锁等待、事务、I/O、执行阶段切换等事件。
    2. 与函数级记录关联，形成更完整的数据库视图。
    3. 为消息生成和生命周期重建提供原始素材。
    """

    def collect_db_events(self) -> list[dict[str, object]]:
        """采集数据库事件。"""
        raise NotImplementedError

    def correlate_events(
        self, function_records: list[dict[str, object]], event_records: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """把函数记录与事件记录做关联。"""
        raise NotImplementedError
