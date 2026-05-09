"""数据流分析接口。"""

from __future__ import annotations


class DataFlowControlFlow:
    """数据流分析器。

    用法：
    1. 分析配置项、资源变量和执行状态的传播路径。
    2. 识别异常根因如何影响执行过程。
    3. 服务于系统级调参和 SQL 级优化建议生成。
    """

    def analyze_data_flow(
        self, code_units: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """执行数据流分析。"""
        raise NotImplementedError

    def map_knobs_to_paths(
        self, data_flow_graphs: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """建立参数与影响路径的映射。"""
        raise NotImplementedError
