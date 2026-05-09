""" Kernel Events 接口。"""

from __future__ import annotations


class KernelEvents:
    """采集内核事件。

    用法：
    1. 统一采集调度、页缓存、块设备、网络栈等事件。
    2. 补充单纯函数级采集无法覆盖的上下文信息。
    3. 与用户态监控共同组成跨层观测视图。
    """

    def collect_kernel_events(self) -> list[dict[str, object]]:
        """采集内核事件。"""
        raise NotImplementedError

    def correlate_with_resources(
        self, resource_records: list[dict[str, object]], kernel_events: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """把内核事件与资源函数记录关联。"""
        raise NotImplementedError
