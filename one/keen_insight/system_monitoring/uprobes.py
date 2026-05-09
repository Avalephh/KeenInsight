""" Uprobes 接口。"""

from __future__ import annotations

from ..models import ProbeAttachment


class Uprobes:
    """管理用户态探针。

    用法：
    1. 在数据库进程的关键函数上挂载 uprobes。
    2. 统一维护用户态探针的安装、卸载与状态检查。
    3. 为后续数据库执行函数和事件采集提供入口。
    """

    def attach_uprobes(self, attachments: list[ProbeAttachment]) -> None:
        """挂载用户态探针。"""
        raise NotImplementedError

    def detach_uprobes(self, probe_names: list[str]) -> None:
        """卸载用户态探针。"""
        raise NotImplementedError

    def check_probe_status(self) -> dict[str, str]:
        """查询当前用户态探针状态。"""
        raise NotImplementedError
