"""SQL 语法与语义校验 — 预留接口，当前直接通过。"""

from __future__ import annotations


class SyntaxSemanticValidation:
    """语法与语义校验器（预留接口）。"""

    def validate_sql_syntax(self, sql_text: str) -> bool:
        """校验 SQL 语法（当前始终返回 True）。"""
        return True

    def validate_sql_semantics(
        self, original_sql: str, optimized_sql: str
    ) -> bool:
        """校验优化前后 SQL 语义一致性（当前始终返回 True）。"""
        return True
