""" Kprobes 接口。"""

from __future__ import annotations

from ..models import ProbeAttachment


class Kprobes:
    """管理内核态探针。

    用法：
    1. 在资源调度、I/O、网络、文件系统等关键点挂载 kprobes。
    2. 负责内核态探针的安装、卸载和状态维护。
    3. 为资源函数和内核事件采集提供基础能力。
    """

    def attach_kprobes(self, attachments: list[ProbeAttachment]) -> None:
        """挂载内核态探针。"""
        raise NotImplementedError

    def detach_kprobes(self, probe_names: list[str]) -> None:
        """卸载内核态探针。"""
        raise NotImplementedError

    def check_probe_status(self) -> dict[str, str]:
        """查询当前内核态探针状态。"""
        raise NotImplementedError
