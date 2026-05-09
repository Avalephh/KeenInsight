""" DB Execution Functions 接口。"""

from __future__ import annotations


class DBExecutionFunctions:
    """采集数据库执行函数信息。

    用法：
    1. 关注解析、优化、执行等数据库函数路径。
    2. 将函数级别行为转化为统一的执行记录。
    3. 为 SQL 生命周期重建和异常诊断提供证据。
    """

    def collect_execution_functions(self) -> list[dict[str, object]]:
        """采集数据库执行函数调用记录。"""
        raise NotImplementedError

    def normalize_function_records(
        self, raw_records: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """将原始函数记录转换为统一格式。"""
        raise NotImplementedError
