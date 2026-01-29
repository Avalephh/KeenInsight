import os
import re
import sys
from pathlib import Path

# from dataclasses import dataclass
from collections import defaultdict

import sqlparse
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine, inspect, text
from sqlparse.sql import (
    Comparison,
    Function,
    Identifier,
    IdentifierList,
    Parenthesis,
    TokenList,
    Where,
)
from sqlparse.tokens import DML
from sqlparse.tokens import Comparison as TComparison
from sqlparse.tokens import Keyword, Literal, Name, Whitespace, Wildcard

# Add project root directory to Python path
sys.path.append(str(Path(__file__).resolve().parents[4]))

import jpype as jp

from dream.database.pg_env import PostgresDB

# Initialize FastMCP server
mcp = FastMCP("db_diagnosis")


def clean_sql(sql):
    sql = sql.strip()
    sql = re.sub(r"\s+", " ", sql)
    return sql


def extract_tables(parsed, database_config):
    tables = set()

    def _extract(token_list):
        from_seen = False
        for token in token_list.tokens:
            if token.is_group:
                _extract(token)

            if token.ttype is Keyword and token.value.upper() in ("FROM", "JOIN"):
                from_seen = True
                continue

            if from_seen:
                if isinstance(token, IdentifierList):
                    for identifier in token.get_identifiers():
                        name = identifier.get_real_name()
                        if name:
                            tables.add(name)
                    from_seen = False
                elif isinstance(token, Identifier):
                    name = token.get_real_name()
                    if name:
                        tables.add(name)
                    from_seen = False
                elif token.ttype is Whitespace:
                    continue
                else:
                    from_seen = False

    _extract(parsed)

    postgres_db = PostgresDB(
        user=database_config["user"],
        password=database_config["password"],
        host=database_config["host"],
        port=database_config["port"],
        dbname=database_config["dbname"],
        workload_type=database_config["workload_type"],
        postgres_path=database_config["postgres_path"],
        postgres_data=database_config["postgres_data"],
        log_path=database_config["log_path"],
    )
    existing_tables = postgres_db.get_tables()

    return [t for t in tables if t in existing_tables]


def extract_columns(parsed, database_config):
    columns = set()

    def _extract_columns(token_list):
        for token in token_list.tokens:
            # Process SELECT clause
            if token.ttype is DML and token.value.upper() == "SELECT":
                idx, next_token = token_list.token_next(token_list.token_index(token))
                if isinstance(next_token, IdentifierList):
                    for identifier in next_token.get_identifiers():
                        _extract_identifier(identifier)
                else:
                    _extract_identifier(next_token)

            # Recursively process subqueries/nested structures
            elif token.is_group:
                _extract_columns(token)

    def _extract_identifier(identifier):
        if isinstance(identifier, Identifier):
            real_name = identifier.get_real_name()
            if real_name:
                columns.add(real_name)
            # Try to parse column names in function calls, e.g., "SUM(o.o_totalprice) AS total"
            if identifier.tokens:
                for subtoken in identifier.tokens:
                    if isinstance(subtoken, Function):
                        _extract_function(subtoken)
        elif isinstance(identifier, Function):
            _extract_function(identifier)
        elif hasattr(identifier, "ttype") and identifier.ttype == Wildcard:
            columns.add("*")
        elif hasattr(identifier, "value"):
            # Final fallback strategy: directly extract field names via regex
            potential_cols = re.findall(r"\b\w+\.\w+\b", identifier.value)
            for col in potential_cols:
                columns.add(col.split(".")[-1])

    def _extract_function(func_token):
        # Try to extract field names in function body
        inside = func_token.value
        # Capture field names like o.o_totalprice or o_orderdate
        matches = re.findall(r"\b\w+\.\w+\b", inside)
        for match in matches:
            columns.add(match.split(".")[-1])

    _extract_columns(parsed)

    # Connect to database to check if columns exist
    postgres_db = PostgresDB(
        user=database_config["user"],
        password=database_config["password"],
        host=database_config["host"],
        port=database_config["port"],
        dbname=database_config["dbname"],
        workload_type=database_config["workload_type"],
        postgres_path=database_config["postgres_path"],
        postgres_data=database_config["postgres_data"],
        log_path=database_config["log_path"],
    )
    existing_columns = postgres_db.get_columns()

    return [t for t in columns if t in existing_columns and t != "*"]


def is_redundant_comparison(token):
    if not isinstance(token, Comparison):
        return None
    parts = [t for t in token.tokens if not t.is_whitespace]
    if len(parts) != 3:
        return None
    left, op, right = parts
    if op.ttype != TComparison:
        return None
    if left.ttype in Literal and right.ttype in Literal and left.value == right.value:
        return str(token)
    if left.ttype == Name and right.ttype == Name and left.value == right.value:
        return str(token)
    return None


def find_redundant_where_conditions_recursive(token_list):
    """Recursively find all redundant conditions in WHERE"""
    redundant = []
    for token in token_list.tokens:
        # Find WHERE clause
        if isinstance(token, Where):
            for subtoken in token.tokens:
                expr = is_redundant_comparison(subtoken)
                if expr:
                    redundant.append(expr)
                if subtoken.ttype == Keyword and subtoken.value.upper() in (
                    "TRUE",
                    "FALSE",
                ):
                    redundant.append(subtoken.value.upper())
        # If it's a nested structure (subquery, parentheses, etc.), continue recursion
        if isinstance(token, TokenList):
            redundant.extend(find_redundant_where_conditions_recursive(token))
    return redundant


def count_nested_selects(parsed):
    """Recursively calculate nesting level of SELECT"""

    def _count(token_list, level=0):
        max_level = level
        for token in token_list.tokens:
            if token.ttype == DML and token.value.upper() == "SELECT":
                level += 1
                max_level = max(max_level, level)
            elif isinstance(token, TokenList):
                max_level = max(max_level, _count(token, level))
        return max_level

    return _count(parsed)


def extract_subqueries(token_list):
    """Recursively extract all subquery text"""
    subqueries = []

    for token in token_list.tokens:
        # Detect SELECT subquery: Parenthesis starting with (SELECT
        if isinstance(token, Parenthesis):
            inside = token.value.strip("() ").strip()
            if inside.upper().startswith("SELECT"):
                subqueries.append(inside)
        # If there are nested structures, process recursively
        if isinstance(token, TokenList):
            subqueries.extend(extract_subqueries(token))

    return subqueries


def find_duplicate_subqueries(sql):
    """Find duplicate subqueries (exactly the same)"""
    parsed = sqlparse.parse(sql)[0]
    subqueries = extract_subqueries(parsed)
    seen = {}
    duplicates = []

    for sq in subqueries:
        normalized = sqlparse.format(sq, keyword_case="upper", strip_comments=True).strip()
        if normalized in seen:
            duplicates.append(normalized)
        else:
            seen[normalized] = True
    return duplicates


def find_related_views(sql, database_config):
    from dream.database.pg_env import PostgresDB

    postgres_db = PostgresDB(
        user=database_config["user"],
        password=database_config["password"],
        host=database_config["host"],
        port=database_config["port"],
        dbname=database_config["dbname"],
        workload_type=database_config["workload_type"],
        postgres_path=database_config["postgres_path"],
        postgres_data=database_config["postgres_data"],
        log_path=database_config["log_path"],
    )

    views = postgres_db.get_views()

    parsed = sqlparse.parse(sql)[0]
    target_tables = extract_tables(parsed, database_config)
    target_columns = extract_columns(parsed, database_config)

    related_views = []

    for viewname, definition in views:
        definition_parsed = sqlparse.parse(definition)[0]
        view_tables = extract_tables(definition_parsed, database_config)
        view_columns = extract_columns(definition_parsed, database_config)
        if any(tbl in view_tables for tbl in target_tables):
            if any(col in view_columns for col in target_columns):
                definition = clean_sql(definition)
                related_views.append({"viewname": viewname, "definition": definition})

    return related_views


# info collect
@mcp.tool()
async def indexes_info_collect(database_config):
    postgres_db = PostgresDB(
        user=database_config["user"],
        password=database_config["password"],
        host=database_config["host"],
        port=database_config["port"],
        dbname=database_config["dbname"],
        workload_type=database_config["workload_type"],
        postgres_path=database_config["postgres_path"],
        postgres_data=database_config["postgres_data"],
        log_path=database_config["log_path"],
    )

    sql = "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY tablename, indexname;"

    exist_indexs = postgres_db.fetch_results(sql, json=True)

    return exist_indexs


@mcp.tool()
async def query_info_collect(query, database_config):
    token_list = sqlparse.parse(query)[0]
    query_info = {}

    redundant_exprs = find_redundant_where_conditions_recursive(token_list)
    query_info["redundant_exprs"] = redundant_exprs

    nested_select_level = count_nested_selects(token_list)
    query_info["nested_select_level"] = nested_select_level

    duplicate_subqueries = find_duplicate_subqueries(query)
    query_info["duplicate_subqueries"] = duplicate_subqueries

    related_views = find_related_views(query, database_config)
    query_info["related_views"] = related_views

    return query_info


@mcp.tool()
async def system_knobs_info_collect(database_config, knob_config):
    postgres_db = PostgresDB(database_config)

    system_knobs_info = {}

    system_knobs = tuple(knob_config["system_knobs"].keys())
    sql = f"SELECT name, setting FROM pg_settings WHERE name IN {system_knobs};"
    system_knobs_info = postgres_db.fetch_results(sql, json=True)

    return system_knobs_info


@mcp.tool()
async def query_knobs_info_collect(database_config, knob_config):
    postgres_db = PostgresDB(database_config)

    query_knobs_info = {}

    query_knobs = tuple(knob_config["query_knobs"].keys())
    sql = f"SELECT name, setting FROM pg_settings WHERE name IN {query_knobs};"
    query_knobs_info = postgres_db.fetch_results(sql, json=True)

    return query_knobs_info


@mcp.tool()
async def plan_info_collect(query, database_config):
    postgres_db = PostgresDB(
        user=database_config["user"],
        password=database_config["password"],
        host=database_config["host"],
        port=database_config["port"],
        dbname=database_config["dbname"],
        workload_type=database_config["workload_type"],
        postgres_path=database_config["postgres_path"],
        postgres_data=database_config["postgres_data"],
        log_path=database_config["log_path"],
    )

    parsed = sqlparse.parse(query)[0]
    target_tables = extract_tables(parsed, database_config)

    # Get time since last analyze update
    time_since_last_analyze = {}
    for table in target_tables:
        analyze_sql = f"""
            SELECT 
            relname AS table_name, 
            now() - GREATEST(last_analyze, last_autoanalyze) AS time_since_last_analyze
            FROM pg_stat_user_tables 
            WHERE relname = '{table}';
        """
        result = postgres_db.fetch_results(analyze_sql)
        if result:
            table_name, time_since = result[0]
            time_since_last_analyze[table_name] = time_since

    # Get table statistics
    table_stats_info = {}
    table_stats_sql = """
        SELECT
        relname AS table_name,
        reltuples::BIGINT AS estimated_rows,
        relpages AS estimated_pages,
        pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
    FROM
        pg_class c
    JOIN
        pg_namespace n ON n.oid = c.relnamespace
    WHERE
        relkind = 'r'
        AND n.nspname = 'public';
    """
    result = postgres_db.fetch_results(table_stats_sql)
    for row in result:
        table_name, estimated_rows, estimated_pages, total_size = row
        table_stats_info[table_name] = {
            "estimated_rows": estimated_rows,
            "estimated_pages": estimated_pages,
            "total_size": total_size,
        }

    # Get statistics for relevant columns
    column_stats_info = {}
    target_columns = extract_columns(parsed, database_config)
    for column in target_columns:
        column_stats_sql = f"""
            SELECT
                tablename,
                attname,
                null_frac,
                n_distinct,
                most_common_vals,
                most_common_freqs,
                histogram_bounds
            FROM
                pg_stats
            WHERE
                attname = '{column}';
        """
        result = postgres_db.fetch_results(column_stats_sql)
        if result:
            tablename, attname, null_frac, n_distinct, mc_vals, mc_freqs, histogram = result[0]
            column_stats_info[column] = {
                "tablename": tablename,
                "attname": attname,
                "null_frac": null_frac,
                "n_distinct": n_distinct,
                "most_common_vals": mc_vals,
                "most_common_freqs": mc_freqs,
                "histogram_bounds": histogram,
            }

    plan_info = {
        "time_since_last_analyze": time_since_last_analyze,
        "table_stats_info": table_stats_info,
        "column_stats_info": column_stats_info,
    }

    return plan_info


# action space define
@mcp.tool()
async def indexes_action_space(query):
    parsed = sqlparse.parse(query)[0]
    tables = extract_tables(parsed)
    columns = extract_columns(parsed)

    indexes_action_spaces_prompt = f"""
    Please consider creating the most appropriate indexes
    on the most relevant tables {tables} and columns {columns}
    involved in this slow SQL query to improve its performance.
    """

    return indexes_action_spaces_prompt


@mcp.tool()
async def system_knobs_action_space(database_config, knob_config):

    # Can also narrow down to a space that only adjusts knobs related to the query

    postgres_db = PostgresDB(database_config)

    knob_info = {}

    for knob in knob_config["system_knobs"].keys():
        sql = f"""
        SELECT unit, vartype, min_val, max_val, boot_val, short_desc
        FROM pg_settings
        WHERE name = '{knob}';
        """
        result = postgres_db.fetch_results(sql)
        if result:
            unit, vartype, min_val, max_val, boot_val, short_desc = result[0]
            knob_info[knob] = {
                "unit": unit,
                "vartype": vartype,
                "min_val": min_val,
                "max_val": max_val,
                "default_val": boot_val,
                "short_desc": short_desc,
            }

    knobs_action_space_prompt = f"""
    Please adjust the following knobs to optimize the performance of the slow query.  
    Each system knob is listed with its configurable range and metadata. Only consider system knobs provided below.

    System Knob Configuration Space:
    {knob_info}
    """

    return knobs_action_space_prompt


@mcp.tool()
async def rewrite_action_space(sql, database_config):
    """
    读取user_selected_rules.txt，判断哪些规则可以应用于当前SQL，返回可用规则名列表。
    sql: 需要分析的SQL语句
    其余参数为数据库连接信息
    """
    import os

    # 使用相对路径，基于当前模块所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    rules_path = os.path.join(current_dir, "user_selected_rules.txt")
    if not os.path.exists(rules_path):
        return f"规则文件不存在，请检查路径：{rules_path}"
    with open(rules_path, "r") as f:
        rulelist = [line.strip() for line in f if line.strip()]

    # 启动JVM（如果未启动）
    query_rewrite_dir = current_dir  # 使用当前模块所在目录
    local_lib_dir = os.path.join(query_rewrite_dir, "libs")
    classpath_file = os.path.join(query_rewrite_dir, "classpath.txt")
    with open(classpath_file, "r") as f:
        classpath = f.readline().strip().split(":")
    classpath.extend([os.path.join(local_lib_dir, jar) for jar in os.listdir(local_lib_dir) if jar.endswith(".jar")])
    if not jp.isJVMStarted():
        jp.startJVM("-Xss4M", jvmpath=jp.getDefaultJVMPath(), classpath=classpath)

    from java.sql import DriverManager
    from org.apache.calcite.adapter.jdbc import JdbcSchema
    from org.apache.calcite.jdbc import CalciteConnection
    from org.apache.calcite.sql2rel import SqlToRelConverter
    # from org.apache.calcite.sql.dialect import PostgresqlSqlDialect
    from org.apache.calcite.sql.parser import SqlParser
    from org.apache.calcite.sql.parser.babel import SqlBabelParserImpl
    from org.apache.calcite.sql.validate import SqlConformanceEnum
    from org.apache.calcite.tools import Frameworks
    from org.apache.calcite.util import SourceStringReader

    # 连接数据库，构建schema
    conn = DriverManager.getConnection("jdbc:calcite:fun=standard,postgresql")
    calcite_conn = conn.unwrap(CalciteConnection)
    root_schema = calcite_conn.getRootSchema()

    dbtype = database_config["db_type"]
    dbname = database_config["dbname"]
    user = database_config["user"]
    passwd = database_config["password"]
    host = database_config["host"]
    port = database_config["port"]

    if dbtype == "mysql":
        data_source = JdbcSchema.dataSource(
            f"jdbc:mysql://127.0.0.1:{port}/{dbname}?useSSL=false",
            "com.mysql.jdbc.Driver",
            user,
            passwd,
        )
    else:
        data_source = JdbcSchema.dataSource(
            f"jdbc:postgresql://{host}:{port}/{dbname}",
            "org.postgresql.Driver",
            user,
            passwd,
        )
    schema = root_schema.add(dbname, JdbcSchema.create(root_schema, dbname, data_source, None, None))
    parserConfig = SqlParser.configBuilder().setConformance(SqlConformanceEnum.BABEL).setParserFactory(SqlBabelParserImpl.FACTORY).setCaseSensitive(False).build()
    converterConfig = SqlToRelConverter.config().withExpand(True)
    config = Frameworks.newConfigBuilder().defaultSchema(schema).parserConfig(parserConfig).sqlToRelConverterConfig(converterConfig).build()
    planner = Frameworks.getPlanner(config)
    # dialect = PostgresqlSqlDialect.DEFAULT

    # 解析SQL，生成rel_node
    planner.close()
    planner.reset()
    sql_node = planner.parse(SourceStringReader(sql))
    sql_node = planner.validate(sql_node)
    rel_root = planner.rel(sql_node)
    rel_node = rel_root.project()

    # 遍历规则，判断哪些可用
    available_rules = []
    for rule_str in rulelist:
        try:
            rule = eval(rule_str)
            if rule.getOperand().matches(rel_node):
                available_rules.append(rule_str)
        except Exception as e:
            print("rule match error:", e)
            continue

    rewrite_action_space_prompt = f"""
    Below is a list of optimization rules based on Apache Calcite. 
    For each rule, the conditions and transformations are described. 
    When given a SQL query, your task is to analyze the query, 
    determine which of these rules might apply, and rewrite the query if beneficial.

    Rules_Space:
    {available_rules}

    Please notice that the {dbtype} database has it's own rewrtie modules, you should consider to outperform the default rewrite modules of the {dbtype} database.
    """

    return rewrite_action_space_prompt


@mcp.tool()
async def query_knobs_action_space(database_config, knob_config):

    postgres_db = PostgresDB(database_config)

    knob_info = {}

    for knob in knob_config["query_knobs"].keys():
        sql = f"""
        SELECT unit, vartype, min_val, max_val, boot_val, short_desc
        FROM pg_settings
        WHERE name = '{knob}';
        """
        result = postgres_db.fetch_results(sql)
        if result:
            unit, vartype, min_val, max_val, boot_val, short_desc = result[0]
            knob_info[knob] = {
                "unit": unit,
                "vartype": vartype,
                "min_val": min_val,
                "max_val": max_val,
                "default_val": boot_val,
                "short_desc": short_desc,
            }

    query_knobs_action_space_prompt = f"""
    Please adjust the following knobs to optimize the performance of the slow query.  
    Each query knob is listed with its configurable range and metadata. Only consider query knobs provided below.

    Query Knob Configuration Space:
    {knob_info}
    """

    return query_knobs_action_space_prompt


@mcp.tool()
async def plan_action_space(query, knob_config):
    query = query.lower()

    hint_actions = []

    # hint_actions += ["/*+ Set(GUC-param value) */"]

    # 1. Query Knob
    for knob in knob_config["query_knobs"].keys():
        hint_actions += [f"/*+ Set({knob} on) */", f"/*+ Set({knob} off) */"]

    # 2. Table Scan Method
    if "from" in query:
        hint_actions += [
            "/*+ Rows(table table[ table...] correction) */",
            "/*+ Parallel(table <# of workers> [soft|hard]) */" "/*+ SeqScan(table) */",
            "/*+ IndexScan(table[ index...]) */",
            "/*+ BitmapScan(table[ index...]) */",
            "/*+ IndexOnlyScan(table[ index...]) */",
        ]

    # 3. Join Method
    if "join" in query:
        hint_actions += [
            "/*+ NestLoop(table table[ table...]) */",
            "/*+ HashJoin(table table[ table...]) */",
            "/*+ MergeJoin(table table[ table...]) */",
            "/*+ Leading(table table[ table...]) */",
            "/*+ Memoize(table table[ table...]) */",
            "/*+ NoMemoize(table table[ table...]) */",
        ]

    hint_actions = list(set(hint_actions))

    # set类hint必须被使用，其他hint可以不使用
    set_hints = [hint for hint in hint_actions if hint.startswith("/*+ Set(")]
    other_hints = [hint for hint in hint_actions if not hint.startswith("/*+ Set(")]

    plan_action_space_prompt = f"""
    Please consider the following hints to optimize the performance of the slow query.
    You must use at least one of the following Set hints:
    {set_hints}
    You may optionally use any of the following hints:
    {other_hints}
    """

    return plan_action_space_prompt


# action generate (no use)
async def update_statistics(query):
    query = clean_sql(query)
    parsed = sqlparse.parse(query)[0]
    tables = extract_tables(parsed)

    engine = create_engine("postgresql://postgres:postgres@localhost:5432/tpch10G")

    result = {
        "action": "update_statistics",
        "tables_analyzed": [],
        "executed_commands": [],
    }

    # execute ANALYZE for each table
    with engine.connect() as conn:
        DEAD_TUPLE_RATIO_THRESHOLD = 0.1  # dead tuple ratio >10% suggest VACUUM
        MOD_SINCE_ANALYZE_THRESHOLD = 1000  # modified rows since last ANALYZE >1000 suggest ANALYZE

        for table in tables:
            # get the statistics from pg_stat_all_tables
            stats = conn.execute(
                text(
                    """
                SELECT
                  n_live_tup,
                  n_dead_tup,
                  n_mod_since_analyze,
                  last_vacuum,
                  last_autoanalyze,
                  last_analyze,
                  last_autoanalyze
                FROM pg_stat_all_tables s
                JOIN pg_class c ON s.relid = c.oid
                WHERE c.relkind = 'r' AND s.schemaname = current_schema() AND s.relname = :tbl
                """
                ),
                {"tbl": table},
            ).fetchone()

            # if the table does not exist or has no statistics, skip
            if not stats:
                continue

            n_live, n_dead, n_mod, last_vac, last_auto_vac, last_an, last_auto_an = stats
            dead_pct = (n_dead / (n_live + n_dead)) if (n_live + n_dead) > 0 else 0

            result["tables_checked"].append(
                {
                    "table": table,
                    "n_live_tup": n_live,
                    "n_dead_tup": n_dead,
                    "dead_ratio": round(dead_pct, 4),
                    "n_mod_since_analyze": n_mod,
                    "last_vacuum": str(last_vac),
                    "last_auto_vacuum": str(last_auto_vac),
                    "last_analyze": str(last_an),
                    "last_auto_analyze": str(last_auto_an),
                }
            )

            # whether to VACUUM and ANALYZE
            if dead_pct > DEAD_TUPLE_RATIO_THRESHOLD and n_mod > MOD_SINCE_ANALYZE_THRESHOLD:
                result["vacuum_commands"].append(f"VACUUM ANALYZE {table};")

            # whether to VACUUM
            elif dead_pct > DEAD_TUPLE_RATIO_THRESHOLD:
                result["vacuum_commands"].append(f"VACUUM {table};")

            # whether to ANALYZE
            elif n_mod > MOD_SINCE_ANALYZE_THRESHOLD:
                result["analyze_commands"].append(f"ANALYZE {table};")

    return result


async def join_order_optimization(query):
    """优化查询的连接顺序，通过测试不同的连接策略找出最优执行计划

    Args:
        query: SQL查询语句

    Returns:
        Dict[str, Any]: 包含优化结果的字典
    """
    # 清理SQL语句
    query = clean_sql(query)

    # 解析SQL语句
    parsed = sqlparse.parse(query)[0]

    # 提取涉及的表
    tables = extract_tables(parsed)

    # 连接数据库
    engine = create_engine("postgresql://postgres:postgres@localhost:5432/tpch10G")

    result = {
        "action": "join_order_optimization",
        "original_query": query,
        "tables_involved": tables,
        "table_statistics": [],
        "join_conditions": [],
        "optimized_query": query,
    }

    if len(tables) < 2:
        result["explanation"] = "查询只涉及单表，无需优化连接顺序"
        return result

    with engine.connect() as conn:
        # 1. 收集表统计信息
        for table in tables:
            stats = conn.execute(
                text(
                    """
                SELECT 
                    reltuples::bigint as row_estimate,
                    relpages::bigint as page_estimate,
                    n_live_tup as live_rows,
                    n_dead_tup as dead_rows
                FROM pg_class c
                LEFT JOIN pg_stat_all_tables s ON c.oid = s.relid
                WHERE c.relname = :table
                """
                ),
                {"table": table},
            ).fetchone()

            if stats:
                result["table_statistics"].append(
                    {
                        "table": table,
                        "rows": stats[0],
                        "pages": stats[1],
                        "live_rows": stats[2],
                        "dead_rows": stats[3],
                    }
                )

        # 2. 提取连接条件
        join_conditions = []
        join_pattern = r"JOIN\s+(\w+)\s+(?:\w+\s+)?ON\s+(.+?)(?=(?:JOIN|WHERE|GROUP|ORDER|LIMIT|$))"
        matches = re.finditer(join_pattern, query, re.IGNORECASE | re.DOTALL)

        for match in matches:
            table = match.group(1)
            condition = match.group(2).strip()
            join_conditions.append({"table": table, "condition": condition})

        result["join_conditions"] = join_conditions

        # 3. 分析连接类型和选择性
        join_analysis = []
        for join in join_conditions:
            # 提取连接列
            join_cols = re.findall(r"(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)", join["condition"])

            for left_table, left_col, right_table, right_col in join_cols:
                # 获取连接列的选择性
                for table, col in [(left_table, left_col), (right_table, right_col)]:
                    try:
                        selectivity = conn.execute(
                            text(
                                """
                            SELECT n_distinct
                            FROM pg_stats
                            WHERE tablename = :table
                            AND attname = :column
                            """
                            ),
                            {"table": table, "column": col},
                        ).scalar()

                        join_analysis.append(
                            {
                                "table": table,
                                "column": col,
                                "selectivity": selectivity if selectivity else 0,
                            }
                        )
                    except Exception:
                        pass

        # 4. 优化连接顺序
        # 基于表大小和连接选择性进行排序
        sorted_tables = sorted(
            result["table_statistics"],
            key=lambda x: (x.get("rows", float("inf")), x.get("pages", float("inf"))),
        )

        # 生成优化后的查询
        if sorted_tables:
            # 提取SELECT和WHERE部分
            select_pattern = r"(SELECT\s+.+?)\s+FROM"
            select_match = re.search(select_pattern, query, re.IGNORECASE | re.DOTALL)
            select_clause = select_match.group(1) if select_match else "SELECT *"

            where_pattern = r"WHERE\s+(.+?)(?=(GROUP|ORDER|LIMIT|$))"
            where_match = re.search(where_pattern, query, re.IGNORECASE)
            where_clause = f"WHERE {where_match.group(1)}" if where_match else ""

            # 构建优化后的查询
            optimized_query = f"{select_clause}\nFROM {sorted_tables[0]['table']} t1\n"

            for i, join in enumerate(join_conditions):
                optimized_query += f"JOIN {join['table']} t{i+2} ON {join['condition']}\n"

            if where_clause:
                optimized_query += where_clause

            # 保留原查询的GROUP BY, ORDER BY, LIMIT等子句
            remaining_clauses = re.search(r"(?:GROUP|ORDER|LIMIT).+$", query, re.IGNORECASE)
            if remaining_clauses:
                optimized_query += "\n" + remaining_clauses.group(0)

            result["optimized_query"] = clean_sql(optimized_query)

            # 估算成本降低
            original_cost = conn.execute(text(f"EXPLAIN (FORMAT JSON) {query}")).scalar()
            optimized_cost = conn.execute(text(f"EXPLAIN (FORMAT JSON) {result['optimized_query']}")).scalar()

            if original_cost and optimized_cost:
                cost_reduction = ((float(original_cost) - float(optimized_cost)) / float(original_cost)) * 100
                result["estimated_cost_reduction"] = f"{cost_reduction:.1f}%"

        # 5. 添加优化建议
        hints = []

        # 检查表大小差异
        if len(sorted_tables) > 1:
            size_ratio = sorted_tables[-1]["rows"] / sorted_tables[0]["rows"]
            if size_ratio > 10:
                hints.append(f"建议将小表 {sorted_tables[0]['table']} 作为驱动表")

        # 检查连接列的选择性
        for analysis in join_analysis:
            if analysis["selectivity"] and analysis["selectivity"] < 0.1:
                hints.append(f"表 {analysis['table']} 的连接列 {analysis['column']} 具有高选择性，适合早期过滤")

        result["optimization_hints"] = hints

    return result


async def optimize_index(query):
    """优化数据库索引

    Args:
        query: SQL查询语句

    Returns:
        Dict[str, Any]: 包含索引优化建议的字典，包括需要创建和删除的索引
    """
    # 清理SQL语句
    query = clean_sql(query)

    # 解析SQL语句
    parsed = sqlparse.parse(query)[0]

    # 提取涉及的表名
    tables = extract_tables(parsed)

    # 连接数据库（这里使用示例连接字符串，实际使用时需要替换）
    engine = create_engine("postgresql://postgres:postgres@localhost:5432/tpch10G")
    inspector = inspect(engine)

    result = {
        "action": "optimize_index",
        "tables_analyzed": [],
        "create_index_statements": [],
        "drop_index_statements": [],
    }

    for table in tables:
        table_info = {
            "table_name": table,
            "existing_indexes": [],
            "recommended_indexes": [],
        }

        # 获取现有索引
        existing_indexes = inspector.get_indexes(table)
        table_info["existing_indexes"] = existing_indexes

        # 分析查询中的WHERE子句和JOIN条件
        where_columns = []
        join_columns = []

        # 提取WHERE子句中的列
        where_match = re.search(r"WHERE\s+(.+?)(?=(GROUP|ORDER|LIMIT|$))", query, re.IGNORECASE)
        if where_match:
            where_conditions = where_match.group(1)
            where_columns = re.findall(r"(\w+)\s*[=<>]", where_conditions)

        # 提取JOIN条件中的列
        join_match = re.search(r"JOIN.+?ON\s+(.+?)(?=(WHERE|GROUP|ORDER|LIMIT|$))", query, re.IGNORECASE)
        if join_match:
            join_conditions = join_match.group(1)
            join_columns = re.findall(r"(\w+)\s*=", join_conditions)

        # 合并需要索引的列
        needed_columns = list(set(where_columns + join_columns))

        # 检查哪些列需要新建索引
        for col in needed_columns:
            if not any(col in idx.get("column_names", []) for idx in existing_indexes):
                index_name = f"idx_{table}_{col}"
                create_stmt = f"CREATE INDEX {index_name} ON {table} ({col});"
                result["create_index_statements"].append(create_stmt)
                table_info["recommended_indexes"].append({"name": index_name, "columns": [col]})

        # 检查是否有可能需要删除的索引
        used_columns = set(needed_columns)
        for idx in existing_indexes:
            if not any(col in used_columns for col in idx.get("column_names", [])):
                drop_stmt = f"DROP INDEX {idx['name']};"
                result["drop_index_statements"].append(drop_stmt)

        result["tables_analyzed"].append(table_info)

    return result


async def optimize_repeatedly_subqueries(query):
    """优化重复执行的子查询

    Args:
        query: 包含重复子查询的SQL语句

    Returns:
        Dict[str, Any]: 包含优化后查询的字典
    """
    # 清理SQL语句
    query = clean_sql(query)

    # 解析SQL语句
    parsed = sqlparse.parse(query)[0]

    result = {
        "action": "optimize_subqueries",
        "original_query": query,
        "optimized_query": query,
        "explanation": "",
    }

    # 检查是否存在相同的子查询
    subqueries = []
    for token in parsed.tokens:
        if isinstance(token, sqlparse.sql.Token) and token.ttype is None:
            # 提取所有子查询
            matches = re.finditer(r"\(SELECT[^()]*(?:\([^()]*\)[^()]*)*\)", str(token), re.IGNORECASE)
            for match in matches:
                subqueries.append(match.group())

    # 检查重复的子查询
    subquery_count = defaultdict(int)
    for sq in subqueries:
        subquery_count[sq] += 1

    repeated_subqueries = {sq: count for sq, count in subquery_count.items() if count > 1}

    if not repeated_subqueries:
        result["explanation"] = "未发现重复执行的子查询"
        return result

    # 优化重复子查询
    optimized_query = query
    for subquery, count in repeated_subqueries.items():
        # 1. 尝试使用CTE优化
        cte_name = f"cte_{len(result.get('ctes', []))}"
        cte_query = f"WITH {cte_name} AS {subquery} "
        new_query = cte_query + optimized_query.replace(subquery, cte_name)

        # 2. 如果子查询在WHERE子句中，尝试转换为JOIN
        if "WHERE" in optimized_query:
            try:
                # 提取子查询的选择列
                subquery_cols = re.search(r"SELECT\s+(.*?)\s+FROM", subquery, re.IGNORECASE).group(1)
                # 提取子查询的表名
                subquery_table = re.search(r"FROM\s+(.*?)(?:\s+WHERE|\s*$)", subquery, re.IGNORECASE).group(1)

                # 构建JOIN查询
                join_condition = re.search(
                    rf"WHERE.*?{re.escape(subquery)}.*?(?:AND|$)",
                    optimized_query,
                    re.IGNORECASE,
                )
                if join_condition:
                    join_query = optimized_query.replace(
                        subquery,
                        f"(SELECT DISTINCT {subquery_cols} FROM {subquery_table})",
                    )
                    if "EXPLAIN" in join_query.upper():
                        result["join_alternative"] = join_query
            except Exception:
                pass

        optimized_query = new_query

    result["optimized_query"] = clean_sql(optimized_query)
    result["explanation"] = f"发现{len(repeated_subqueries)}个重复执行的子查询，已使用CTE优化。"

    return result


async def optimize_query_knob(query):
    """收集并返回数据库指定knob的当前值和范围信息

    Args:
        query: SQL查询语句（本实现中不做处理，仅为接口兼容）

    Returns:
        Dict[str, Any]: 包含knob当前值、范围、类型等信息的字典
    """
    from sqlalchemy import create_engine, text

    # 定义需要调优的knob及其范围和类型
    knob_config = {
        "work_mem": {"lower": 1, "upper": 1024, "type": "int/continuous"},
        "maintenance_work_mem": {"lower": 16, "upper": 1024, "type": "int/continuous"},
        # "shared_buffers": {"lower": 16, "upper": 2048, "type": "int/continuous"},
        "effective_cache_size": {
            "lower": 512,
            "upper": 32768,
            "type": "int/continuous",
        },
        "max_parallel_workers": {"lower": 0, "upper": 96, "type": "int/continuous"},
        "max_parallel_workers_per_gather": {
            "lower": 0,
            "upper": 8,
            "type": "int/continuous",
        },
        "parallel_tuple_cost": {
            "lower": 0.01,
            "upper": 1.0,
            "type": "float/continuous",
        },
        "parallel_setup_cost": {
            "lower": 100,
            "upper": 10000,
            "type": "float/continuous",
        },
        "random_page_cost": {"lower": 1.0, "upper": 10.0, "type": "float/continuous"},
        "seq_page_cost": {"lower": 0.1, "upper": 4.0, "type": "float/continuous"},
        "cpu_tuple_cost": {"lower": 0.001, "upper": 0.1, "type": "float/continuous"},
        "cpu_operator_cost": {
            "lower": 0.0001,
            "upper": 0.01,
            "type": "float/continuous",
        },
        "enable_seqscan": {"type": "categorical", "value": [0, 1]},
        "enable_indexscan": {"type": "categorical", "value": [0, 1]},
        "enable_indexonlyscan": {"type": "categorical", "value": [0, 1]},
        "enable_bitmapscan": {"type": "categorical", "value": [0, 1]},
        "enable_hashjoin": {"type": "categorical", "value": [0, 1]},
        "enable_mergejoin": {"type": "categorical", "value": [0, 1]},
        "enable_nestloop": {"type": "categorical", "value": [0, 1]},
        "enable_hashagg": {"type": "categorical", "value": [0, 1]},
        "geqo": {"type": "categorical", "value": [0, 1]},
        "geqo_threshold": {"lower": 2, "upper": 100, "type": "int/continuous"},
        "join_collapse_limit": {"lower": 1, "upper": 64, "type": "int/continuous"},
        "from_collapse_limit": {"lower": 1, "upper": 64, "type": "int/continuous"},
        "default_statistics_target": {
            "lower": 10,
            "upper": 1000,
            "type": "int/continuous",
        },
        "log_min_duration_statement": {
            "lower": -1,
            "upper": 60000,
            "type": "int/continuous",
        },
    }

    # 连接数据库，收集当前knob值
    engine = create_engine("postgresql://postgres:postgres@localhost:5432/tpch10G")
    knob_values = {}
    with engine.connect() as conn:
        for knob, meta in knob_config.items():
            try:
                result = conn.execute(text(f"SHOW {knob}")).fetchone()
                if result is not None:
                    knob_values[knob] = str(result[0])
                else:
                    knob_values[knob] = None
            except Exception:
                knob_values[knob] = None

    # 组装返回结果
    knob_info = {}
    for knob, meta in knob_config.items():
        knob_info[knob] = {"current": knob_values.get(knob), **meta}

    return {
        "action": "collect_knob_info",
        "knobs": knob_info,
        "instructions": "please use the knob_info to optimize the query, and do not use other knobs",
    }


async def rewrite_query(query):
    """重写SQL查询以提高性能，根据规则自动判断是否需要改写

    Args:
        query: 原始SQL查询

    Returns:
        Dict[str, Any]: 包含查询重写结果、触发的规则等信息
    """
    import re

    import sqlparse

    # 1. 解析SQL，提取关键信息
    cleaned_query = clean_sql(query)
    parsed = sqlparse.parse(cleaned_query)[0]
    query_info = {
        "has_select_star": bool(re.search(r"SELECT\s+\*", cleaned_query, re.IGNORECASE)),
        "has_order_by": bool(re.search(r"ORDER\s+BY", cleaned_query, re.IGNORECASE)),
        "has_limit": bool(re.search(r"LIMIT", cleaned_query, re.IGNORECASE)),
        "has_subquery": bool(re.search(r"\(SELECT", cleaned_query, re.IGNORECASE)),
        "has_group_by": bool(re.search(r"GROUP\s+BY", cleaned_query, re.IGNORECASE)),
        "has_distinct": bool(re.search(r"DISTINCT", cleaned_query, re.IGNORECASE)),
        "tables": extract_tables(parsed),
    }

    # 2. 规则定义与检测
    rewrite_rules = []
    optimized_query = cleaned_query
    triggered_rules = []

    # 规则1：SELECT * 改为 SELECT 列名
    if query_info["has_select_star"]:
        # 尝试将SELECT *替换为SELECT 具体列名
        for table in query_info["tables"]:
            optimized_query = re.sub(
                r"SELECT\s+\*",
                f"SELECT /* 建议替换为具体列名，如 {table}.col1, {table}.col2 */",
                optimized_query,
                flags=re.IGNORECASE,
            )
        triggered_rules.append("避免使用SELECT *，建议显式指定列名")
        rewrite_rules.append({"rule": "no_select_star", "desc": "避免SELECT *，提升查询效率和可维护性"})

    # 规则2：ORDER BY无LIMIT时建议加LIMIT防止全表排序
    if query_info["has_order_by"] and not query_info["has_limit"]:
        optimized_query += " LIMIT 100"
        triggered_rules.append("ORDER BY未加LIMIT，建议加LIMIT防止全表排序")
        rewrite_rules.append(
            {
                "rule": "order_by_without_limit",
                "desc": "ORDER BY建议加LIMIT，避免大结果集排序",
            }
        )

    # 规则3：子查询可尝试用JOIN重写（这里只做标记，实际需复杂分析）
    if query_info["has_subquery"]:
        triggered_rules.append("存在子查询，部分场景可尝试用JOIN重写提升性能")
        rewrite_rules.append({"rule": "subquery_to_join", "desc": "子查询可尝试用JOIN重写，提升性能"})
        # 不自动改写，仅给出建议

    # 规则4：GROUP BY无索引建议加索引（这里只做标记）
    if query_info["has_group_by"]:
        triggered_rules.append("GROUP BY字段建议加索引")
        rewrite_rules.append({"rule": "group_by_index", "desc": "GROUP BY字段建议加索引，提升分组效率"})

    # 规则5：DISTINCT建议评估是否必要
    if query_info["has_distinct"]:
        triggered_rules.append("DISTINCT操作建议评估是否必要，避免不必要的去重开销")
        rewrite_rules.append({"rule": "distinct_check", "desc": "DISTINCT建议评估是否必要，避免性能损耗"})

    # 3. 返回结果
    return {
        "action": "rewrite_query",
        "original_query": query,
        "optimized_query": optimized_query if triggered_rules else query,
        "triggered_rules": triggered_rules,
        "rewrite_rules": rewrite_rules,
        "query_info": query_info,
    }


if __name__ == "__main__":
    # 初始化并运行服务器
    mcp.run(transport="stdio")
