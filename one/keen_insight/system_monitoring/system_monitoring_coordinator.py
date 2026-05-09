"""系统监控总协调接口。"""

from __future__ import annotations

from ..models import MonitoringSnapshot, SQLLifecycle
from .adaptive_message_generation import AdaptiveMessageGeneration
from .adaptive_attachment import AdaptiveAttachment
from .db_events import DBEvents
from .db_execution_functions import DBExecutionFunctions
from .kernel_events import KernelEvents
from .kprobes import Kprobes
from .kernel_resource_functions import KernelResourceFunctions
from .sql_lifecycle_reconstruction import SQLLifecycleReconstruction
from .uprobes import Uprobes


class SystemMonitoringCoordinator:
    """系统监控总协调器。

    用法：
    1. 在 main 中作为系统监控模块的统一入口。
    2. 协调探针规划、挂载、采集、消息生成和 SQL 生命周期重建。
    3. 对外输出标准化监控快照与 SQL 生命周期列表。
    """

    def __init__(
        self,
        adaptive_attachment: AdaptiveAttachment,
        user_probe_manager: Uprobes,
        db_function_collector: DBExecutionFunctions,
        db_event_collector: DBEvents,
        kprobes: Kprobes,
        kernel_resource_collector: KernelResourceFunctions,
        kernel_events: KernelEvents,
        message_generator: AdaptiveMessageGeneration,
        sql_reconstructor: SQLLifecycleReconstruction,
    ) -> None:
        self.adaptive_attachment = adaptive_attachment
        self.user_probe_manager = user_probe_manager
        self.db_function_collector = db_function_collector
        self.db_event_collector = db_event_collector
        self.kprobes = kprobes
        self.kernel_resource_collector = kernel_resource_collector
        self.kernel_events = kernel_events
        self.message_generator = message_generator
        self.sql_reconstructor = sql_reconstructor

    def build_snapshot(
        self, workload_profile: dict[str, object]
    ) -> MonitoringSnapshot:
        """执行一次监控流程并输出监控快照。"""
        raise NotImplementedError

    def reconstruct_sql_lifecycles(
        self, snapshot: MonitoringSnapshot
    ) -> list[SQLLifecycle]:
        """基于监控快照重建 SQL 生命周期。"""
        raise NotImplementedError
