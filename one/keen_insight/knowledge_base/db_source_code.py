"""数据库源码仓库接口。"""

from __future__ import annotations


class DBSourceCode:
    """数据库源码仓库抽象。

    用法：
    1. 管理数据库源码、函数签名和调用关系入口。
    2. 为 LLVM 分析、数据流分析和代码检索提供输入。
    3. 支持根据函数名、模块名和关键字定位源码。
    """

    def index_source_code(self, source_paths: list[str]) -> None:
        """为数据库源码建立索引。"""
        raise NotImplementedError

    def retrieve_code(self, symbols: list[str]) -> list[dict[str, object]]:
        """检索指定符号对应的源码片段。"""
        raise NotImplementedError
