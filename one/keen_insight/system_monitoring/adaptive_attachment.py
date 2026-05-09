""" Adaptive Attachment 接口。"""

from __future__ import annotations

from ..models import ProbeAttachment


class AdaptiveAttachment:
    """自适应探针挂载策略。

    用法：
    1. 根据当前工作负载动态决定应该挂载哪些探针。
    2. 将探针开销控制与监控覆盖率放在一起权衡。
    3. 同时服务于用户态和内核态的探针规划。
    """

    def plan_user_space_attachments(
        self, workload_profile: dict[str, object]
    ) -> list[ProbeAttachment]:
        """生成用户态探针挂载计划。"""
        raise NotImplementedError

    def plan_kernel_space_attachments(
        self, workload_profile: dict[str, object]
    ) -> list[ProbeAttachment]:
        """生成内核态探针挂载计划。"""
        raise NotImplementedError

    def adjust_strategy(self, runtime_feedback: dict[str, object]) -> None:
        """根据运行反馈调整探针策略。"""
        raise NotImplementedError
