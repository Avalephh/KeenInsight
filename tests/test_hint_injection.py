#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 _inject_hint_into_sql 函数的正确性
"""

import re


def _inject_hint_into_sql(hint: str, sql: str) -> str:
    """将 hint 注入到 SQL 中的每个顶层 SELECT（含 WITH 后主 SELECT）或 EXPLAIN 语句前。

    使用正则表达式匹配，在顶层 SELECT 或 EXPLAIN 前插入 hint。
    """
    # 使用简单的单词边界匹配，然后通过位置判断是否在括号内
    pattern = r"\b(WITH|SELECT|EXPLAIN)\b"

    def replace_match(match):
        # 检查是否在括号内
        start_pos = match.start()
        keyword = match.group(1).upper()

        # 计算到当前位置的左括号和右括号数量
        left_count = sql[:start_pos].count("(")
        right_count = sql[:start_pos].count(")")

        # 如果在括号内，不注入hint
        if left_count > right_count:
            return match.group(0)

        # 特殊处理：如果是 SELECT 且在视图定义中（前面有 "as" 关键字），不注入
        if keyword == "SELECT":
            # 简单检查：如果前面有 "as" 关键字，不注入 hint
            before_text = sql[:start_pos].lower().strip()
            if before_text.endswith("as"):
                return match.group(0)

        # 特殊处理：如果是 WITH，不注入 hint（只在主 SELECT 前注入）
        if keyword == "WITH":
            return match.group(0)

        # 如果前面已经有 hint，不重复添加
        if start_pos > 0:
            prefix = sql[:start_pos].strip()
            if prefix.endswith(hint.strip()):
                return match.group(0)

        return hint + " " + match.group(0)

    # 替换所有匹配项
    result = re.sub(pattern, replace_match, sql, flags=re.IGNORECASE)
    return result


def test_hint_injection():
    """测试 hint 注入功能"""

    hint = "/*+ Set(enable_hashagg on) Set(enable_mergejoin on) Set(enable_indexscan on) Set(enable_nestloop off) Set(enable_seqscan off) Set(enable_gathermerge off) */"

    # 测试用例1: 简单的 SELECT 语句
    sql1 = "SELECT * FROM users WHERE id = 1;"
    expected1 = "/*+ Set(enable_hashjoin off) */ SELECT * FROM users WHERE id = 1;"
    result1 = _inject_hint_into_sql(hint, sql1)
    print(f"测试1 - 简单SELECT:")
    print(f"  输入: {sql1}")
    print(f"  期望: {expected1}")
    print(f"  结果: {result1}")
    print(f"  通过: {result1 == expected1}")
    print()

    # 测试用例2: WITH 子句
    sql2 = "WITH temp AS (SELECT id FROM users) SELECT * FROM temp;"
    expected2 = "WITH temp AS (SELECT id FROM users) /*+ Set(enable_hashjoin off) */ SELECT * FROM temp;"
    result2 = _inject_hint_into_sql(hint, sql2)
    print(f"测试2 - WITH子句:")
    print(f"  输入: {sql2}")
    print(f"  期望: {expected2}")
    print(f"  结果: {result2}")
    print(f"  通过: {result2 == expected2}")
    print()

    # 测试用例3: EXPLAIN 语句
    sql3 = "EXPLAIN SELECT * FROM users WHERE id = 1;"
    expected3 = "/*+ Set(enable_hashjoin off) */ EXPLAIN SELECT * FROM users WHERE id = 1;"
    result3 = _inject_hint_into_sql(hint, sql3)
    print(f"测试3 - EXPLAIN语句:")
    print(f"  输入: {sql3}")
    print(f"  期望: {expected3}")
    print(f"  结果: {result3}")
    print(f"  通过: {result3 == expected3}")
    print()

    # 测试用例4: 子查询中的 SELECT（不应该被注入）
    sql4 = "SELECT * FROM (SELECT id FROM users) AS sub;"
    expected4 = "/*+ Set(enable_hashjoin off) */ SELECT * FROM (SELECT id FROM users) AS sub;"
    result4 = _inject_hint_into_sql(hint, sql4)
    print(f"测试4 - 子查询:")
    print(f"  输入: {sql4}")
    print(f"  期望: {expected4}")
    print(f"  结果: {result4}")
    print(f"  通过: {result4 == expected4}")
    print()

    # 测试用例5: 多个语句
    sql5 = "SELECT * FROM users; SELECT * FROM orders;"
    expected5 = "/*+ Set(enable_hashjoin off) */ SELECT * FROM users; /*+ Set(enable_hashjoin off) */ SELECT * FROM orders;"
    result5 = _inject_hint_into_sql(hint, sql5)
    print(f"测试5 - 多个语句:")
    print(f"  输入: {sql5}")
    print(f"  期望: {expected5}")
    print(f"  结果: {result5}")
    print(f"  通过: {result5 == expected5}")
    print()

    # 测试用例6: 已经包含 hint 的语句（不应该重复添加）
    sql6 = "/*+ Set(enable_hashjoin off) */ SELECT * FROM users;"
    expected6 = "/*+ Set(enable_hashjoin off) */ SELECT * FROM users;"
    result6 = _inject_hint_into_sql(hint, sql6)
    print(f"测试6 - 已有hint:")
    print(f"  输入: {sql6}")
    print(f"  期望: {expected6}")
    print(f"  结果: {result6}")
    print(f"  通过: {result6 == expected6}")
    print()

    # 测试用例7: 复杂的 WITH 和子查询
    sql7 = """with revenue0 (supplier_no, total_revenue) as
        (select
            l_suppkey,
            sum(l_extendedprice * (1 - l_discount))
        from
            lineitem
        where
            l_shipdate >= date '1993-05-01'
            and l_shipdate < date '1993-05-01' + interval '3' month
        group by
            l_suppkey)
    select
        s_suppkey,
        s_name,
        s_address,
        s_phone,
        total_revenue
    from
        supplier,
        revenue0
    where
        s_suppkey = supplier_no
        and total_revenue = (
            select
                max(total_revenue)
            from
                revenue0
        )
    order by
        s_suppkey
    limit 1;
    """
    expected7 = """
    /*+ Set(enable_hashjoin off) */
    with revenue0 (supplier_no, total_revenue) as
        (select
            l_suppkey,
            sum(l_extendedprice * (1 - l_discount))
        from
            lineitem
        where
            l_shipdate >= date '1993-05-01'
            and l_shipdate < date '1993-05-01' + interval '3' month
        group by
            l_suppkey)
    select
        s_suppkey,
        s_name,
        s_address,
        s_phone,
        total_revenue
    from
        supplier,
        revenue0
    where
        s_suppkey = supplier_no
        and total_revenue = (
            select
                max(total_revenue)
            from
                revenue0
        )
    order by
        s_suppkey
    limit 1;
    """
    result7 = _inject_hint_into_sql(hint, sql7)
    print(f"测试7 - 复杂WITH:")
    print(f"  输入: {sql7}")
    print(f"  期望: {expected7}")
    print(f"  结果: {result7}")
    print(f"  通过: {result7 == expected7}")
    print()

    # 测试用例8: 复杂的 WITH 和子查询
    sql8 = """create or replace view revenue0_PID (supplier_no, total_revenue) as
	select
		l_suppkey,
		sum(l_extendedprice * (1 - l_discount))
	from
		lineitem
	where
		l_shipdate >= date '1994-09-01'
		and l_shipdate < date '1994-09-01' + interval '3' month
	group by
		l_suppkey;

    select
        s_suppkey,
        s_name,
        s_address,
        s_phone,
        total_revenue
    from
        supplier,
        revenue0_PID r0
    where
        s_suppkey = supplier_no
        and total_revenue = (
            select
                max(total_revenue)
            from
                revenue0_PID r1
        )
    order by
        s_suppkey;

    drop view revenue0_PID;
    """

    expected8 = """create or replace view revenue0_PID (supplier_no, total_revenue) as
	select
		l_suppkey,
		sum(l_extendedprice * (1 - l_discount))
	from
		lineitem
	where
		l_shipdate >= date '1994-09-01'
		and l_shipdate < date '1994-09-01' + interval '3' month
	group by
		l_suppkey;

    /*+ Set(enable_hashjoin off) */
    select
        s_suppkey,
        s_name,
        s_address,
        s_phone,
        total_revenue
    from
        supplier,
        revenue0_PID r0
    where
        s_suppkey = supplier_no
        and total_revenue = (
            select
                max(total_revenue)
            from
                revenue0_PID r1
        )
    order by
        s_suppkey;

    drop view revenue0_PID;
    """
    result8 = _inject_hint_into_sql(hint, sql8)
    print(f"测试8 - 复杂WITH:")
    print(f"  输入: {sql8}")
    print(f"  期望: {expected8}")
    print(f"  结果: {result8}")
    print(f"  通过: {result8 == expected8}")
    print()


if __name__ == "__main__":
    test_hint_injection()
