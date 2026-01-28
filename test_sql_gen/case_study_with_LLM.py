import asyncio
import os
import re
import time

import psycopg2
from agents import Agent, Runner
from agents._config import set_default_openai_api


def delete_all_loggers():
    """清理和重置所有日志记录器"""
    import logging

    loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
    for logger in loggers:
        handlers = logger.handlers[:]
        for handler in handlers:
            logger.removeHandler(handler)
        logger.propagate = True
        logger.setLevel(logging.CRITICAL)


# 清理所有日志记录器
delete_all_loggers()

# 数据库连接参数
PG_CONN_INFO = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "postgres",
    "dbname": "tpch10G",
}

# 设置大模型API
os.environ["OPENAI_API_KEY"] = "sk-Os3W9DMCISbc0cIGA0AdCf971b5e4a98968dF361Ea6285A7"
os.environ["OPENAI_BASE_URL"] = "https://api.gpt.ge/v1/"
set_default_openai_api("chat_completions")

TPCH_SCHEMA = """
table_name  | column_name        | data_type           | is_primary_key | fk_constraint                        | referenced_table | referenced_column
------------+--------------------+---------------------+----------------+--------------------------------------+------------------+-------------------
customer    | c_custkey          | integer             | YES            |                                      |                  |                   
customer    | c_name             | character varying   | NO             |                                      |                  |                   
customer    | c_address          | character varying   | NO             |                                      |                  |                   
customer    | c_nationkey        | integer             | NO             | customer_c_nationkey_fkey            | nation           | n_nationkey        
customer    | c_phone            | character           | NO             |                                      |                  |                   
customer    | c_acctbal          | numeric             | NO             |                                      |                  |                   
customer    | c_mktsegment       | character           | NO             |                                      |                  |                   
customer    | c_comment          | character varying   | NO             |                                      |                  |                   
lineitem    | l_orderkey         | integer             | YES            | lineitem_l_orderkey_fkey             | orders           | o_orderkey         
lineitem    | l_partkey          | integer             | NO             | lineitem_l_partkey_l_suppkey_fkey    | partsupp         | ps_partkey         
lineitem    | l_suppkey          | integer             | NO             | lineitem_l_partkey_l_suppkey_fkey    | partsupp         | ps_suppkey         
lineitem    | l_linenumber       | integer             | YES            |                                      |                  |                   
lineitem    | l_quantity         | numeric             | NO             |                                      |                  |                   
lineitem    | l_extendedprice    | numeric             | NO             |                                      |                  |                   
lineitem    | l_discount         | numeric             | NO             |                                      |                  |                   
lineitem    | l_tax              | numeric             | NO             |                                      |                  |                   
lineitem    | l_returnflag       | character           | NO             |                                      |                  |                   
lineitem    | l_linestatus       | character           | NO             |                                      |                  |                   
lineitem    | l_shipdate         | date                | NO             |                                      |                  |                   
lineitem    | l_commitdate       | date                | NO             |                                      |                  |                   
lineitem    | l_receiptdate      | date                | NO             |                                      |                  |                   
lineitem    | l_shipinstruct     | character           | NO             |                                      |                  |                   
lineitem    | l_shipmode         | character           | NO             |                                      |                  |                   
lineitem    | l_comment          | character varying   | NO             |                                      |                  |                   
nation      | n_nationkey        | integer             | YES            |                                      |                  |                   
nation      | n_name             | character           | NO             |                                      |                  |                   
nation      | n_regionkey        | integer             | NO             | nation_n_regionkey_fkey              | region           | r_regionkey        
nation      | n_comment          | character varying   | NO             |                                      |                  |                   
orders      | o_orderkey         | integer             | YES            |                                      |                  |                   
orders      | o_custkey          | integer             | NO             | orders_o_custkey_fkey                | customer         | c_custkey          
orders      | o_orderstatus      | character           | NO             |                                      |                  |                   
orders      | o_totalprice       | numeric             | NO             |                                      |                  |                   
orders      | o_orderdate        | date                | NO             |                                      |                  |                   
orders      | o_orderpriority    | character           | NO             |                                      |                  |                   
orders      | o_clerk            | character           | NO             |                                      |                  |                   
orders      | o_shippriority     | integer             | NO             |                                      |                  |                   
orders      | o_comment          | character varying   | NO             |                                      |                  |                   
part        | p_partkey          | integer             | YES            |                                      |                  |                   
part        | p_name             | character varying   | NO             |                                      |                  |                   
part        | p_mfgr             | character           | NO             |                                      |                  |                   
part        | p_brand            | character           | NO             |                                      |                  |                   
part        | p_type             | character varying   | NO             |                                      |                  |                   
part        | p_size             | integer             | NO             |                                      |                  |                   
part        | p_container        | character           | NO             |                                      |                  |                   
part        | p_retailprice      | numeric             | NO             |                                      |                  |                   
part        | p_comment          | character varying   | NO             |                                      |                  |                   
partsupp    | ps_partkey         | integer             | YES            | partsupp_ps_partkey_fkey             | part             | p_partkey          
partsupp    | ps_suppkey         | integer             | YES            | partsupp_ps_suppkey_fkey             | supplier         | s_suppkey          
partsupp    | ps_availqty        | integer             | NO             |                                      |                  |                   
partsupp    | ps_supplycost      | numeric             | NO             |                                      |                  |                   
partsupp    | ps_comment         | character varying   | NO             |                                      |                  |                   
region      | r_regionkey        | integer             | YES            |                                      |                  |                   
region      | r_name             | character           | NO             |                                      |                  |                   
region      | r_comment          | character varying   | NO             |                                      |                  |                   
supplier    | s_suppkey          | integer             | YES            |                                      |                  |                   
supplier    | s_name             | character           | NO             |                                      |                  |                   
supplier    | s_address          | character varying   | NO             |                                      |                  |                   
supplier    | s_nationkey        | integer             | NO             | supplier_s_nationkey_fkey            | nation           | n_nationkey        
supplier    | s_phone            | character           | NO             |                                      |                  |                   
supplier    | s_acctbal          | numeric             | NO             |                                      |                  |                   
supplier    | s_comment          | character varying   | NO             |                                      |                  |                   
"""

FIX_PROMPT_TEMPLATE = """
You are an SQL optimization expert. Your task is to provide a precise and executable fix for the following slow SQL query.

## Input
- TPCH schema:
{SCHEMA}

- The Knobs are the default knobs of postgres;

- Slow SQL:
{SLOW_SQL}

- The SQL Plan:
{SQL_PLAN}

You can use change the online knobs, add index, add query hint or rewrite the SQL and other ways to optimize the query.

## Output Format
Please output ONLY in the following format, and do not add any extra explanation or content:

FIX_ACTION:
<your fix SQL or optimization action here>

Query_Rewrite:
<yes or no>

## Output Requirements
- If the fix is to rewrite the SQL, output "Query_Rewrite" with "yes", else output "Query_Rewrite" with "no".
- The fix action should be a valid SQL statement or a clear optimization action that can be directly executed in PostgreSQL.
- If the fix is to rewrite the SQL, provide the optimized SQL statement.
- If the fix is to add an index or change knobs, provide the exact SQL or command.
- Do not output any explanation, comments, or extra text.
"""

# def get_all_sql_files(directory):
#     sql_files = []
#     for root, _, files in os.walk(directory):
#         for file in files:
#             if file.endswith('.sql'):
#                 sql_files.append(os.path.join(root, file))
#     return sql_files


def extract_index_names(sql):
    """从多条CREATE INDEX语句中提取所有索引名"""
    return re.findall(r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w_]+)", sql, re.IGNORECASE)


def extract_knob_names(sql):
    """从多条SET语句中提取所有索引名"""
    return re.findall(r"SET\s+([a-zA-Z0-9_]+)\s*=", sql, re.IGNORECASE)


def read_sql_file(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def execute_sql_and_time(conn, sql, timeout=120):
    """
    使用PostgreSQL的statement_timeout参数控制SQL超时，单位为毫秒。
    """
    with conn.cursor() as cur:
        try:
            # 设置statement_timeout
            cur.execute(f"SET statement_timeout = {int(timeout * 1000)};")
            start = time.time()
            cur.execute(sql)
            if sql.strip().lower().startswith("select"):
                try:
                    cur.fetchall()
                except Exception:
                    pass
            conn.commit()
            end = time.time()
            return end - start
        except psycopg2.errors.QueryCanceled as e:
            conn.rollback()
            raise TimeoutError(f"SQL执行超时")
        except Exception as e:
            conn.rollback()
            raise e


async def call_fix_agent(slow_sql, sql_plan):
    fixAgent = Agent(
        name="SQLFixAgent",
        model="gpt-4.1",
        instructions="You are an SQL optimization expert, able to provide precise fixes for slow SQL.",
    )
    prompt = FIX_PROMPT_TEMPLATE.format(SLOW_SQL=slow_sql, SCHEMA=TPCH_SCHEMA, SQL_PLAN=sql_plan)
    result = await Runner.run(starting_agent=fixAgent, input=prompt)
    return result.final_output.strip()


def parse_fix_action(result):
    fix_match = re.search(r"FIX_ACTION:\s*([\s\S]+?)Query_Rewrite:", result)
    fix_action = fix_match.group(1).strip() if fix_match else ""
    # 只匹配yes或no
    query_rewrite = re.search(r"Query_Rewrite:\s*(yes|no)", result, re.IGNORECASE)
    query_rewrite = query_rewrite.group(1).strip().lower() if query_rewrite else ""
    return fix_action, query_rewrite


def get_sql_plan(sql, timeout=120):
    cmd = f"EXPLAIN ANALYZE {sql}"
    conn = psycopg2.connect(**PG_CONN_INFO)
    with conn.cursor() as cur:
        try:
            cur.execute(f"SET statement_timeout = {int(timeout * 1000)};")
            cur.execute(cmd)
            plan = cur.fetchall()
            return plan
        except psycopg2.errors.QueryCanceled as e:
            conn.rollback()
            print(f"SQL执行超时")
            return ""
        except Exception as e:
            print(f"获取SQL计划失败: {e}")
            return ""


def rollback_action(conn, index_names, knob_names):
    # 撤销所有索引
    for index_name in index_names:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP INDEX IF EXISTS {index_name}")
            conn.commit()
            print(f"已撤销索引: {index_name}")
        except Exception as e:
            conn.rollback()
            print(f"撤销索引失败: {e}")
    for knob_name in knob_names:
        try:
            with conn.cursor() as cur:
                cur.execute(f"RESET {knob_name};")
            conn.commit()
            print(f"{knob_name} 已恢复默认值")
        except Exception as e:
            conn.rollback()
            print(f"恢复{knob_name}失败: {e}")


def extract_fix_action(fix_action: str):
    """
    提取修复动作（所有CREATE/SET等）和主SQL（从第一个WITH/SELECT/INSERT/UPDATE/DELETE开始的所有内容）。
    返回二元组（修复动作, 主SQL）。
    """
    # 按行分割
    lines = fix_action.strip().splitlines()
    sql_start = None
    for idx, line in enumerate(lines):
        l = line.lstrip().upper()
        if l.startswith("WITH") or l.startswith("SELECT") or l.startswith("INSERT") or l.startswith("UPDATE") or l.startswith("DELETE"):
            sql_start = idx
            break
    if sql_start is not None:
        fix_part = "\n".join([line for line in lines[:sql_start] if line.strip()])
        sql_part = "\n".join(lines[sql_start:]).strip()
        return fix_part, sql_part
    else:
        return fix_action.strip(), ""


async def main():
    sql_dir = "/root/DREAM/test_sql_gen/slow_queries/gen_query"
    # sql_files = get_all_sql_files(sql_dir)
    total_before = 0.0
    total_after = 0.0
    conn = psycopg2.connect(**PG_CONN_INFO)
    for i in range(1, 23):
        sql_num = f"q{i}.sql"
        sql_file = os.path.join(sql_dir, sql_num)
        print(f"处理: {sql_file}")
        sql = read_sql_file(sql_file)
        try:
            before_time = execute_sql_and_time(conn, sql, timeout=30)
        except TimeoutError as e:
            print(f"原始SQL执行超时: {e}")
            before_time = 30
        except Exception as e:
            print(f"原始SQL执行失败: {e}")
            continue
        print(f"优化前执行时间: {before_time:.4f}秒")
        total_before += before_time
        if i != 15:
            sql_plan = get_sql_plan(sql, timeout=30)
        else:
            sql_plan = ""
        # 调用大模型修复
        try:
            fix_result = await call_fix_agent(sql, sql_plan)
            fix_action, query_rewrite = parse_fix_action(fix_result)

            # 如果有CREATE INDEX，提取所有索引名
            index_names = extract_index_names(fix_action)
            knob_names = extract_knob_names(fix_action)
            print(f"索引名: {index_names}")
            print(f"knob名: {knob_names}")

            if query_rewrite == "yes":
                fix_action, sql = extract_fix_action(fix_action)
                print(f"修复动作: {fix_action}")
                print(f"重写SQL: {sql}")
                print(f"是否重写SQL: {query_rewrite}")
            else:
                print(f"修复动作: {fix_action}")
                print(f"是否重写SQL: {query_rewrite}")

            # 执行修复动作
            try:
                fix_time = execute_sql_and_time(conn, fix_action, timeout=500)
            except TimeoutError as e:
                print(f"修复动作执行超时: {e}")
            except Exception as e:
                print(f"修复动作执行失败: {e}")
                rollback_action(conn, index_names, knob_names)
                continue
            print(f"修复成功，修复时间: {fix_time:.4f}秒")
            # 再次执行SQL
            try:
                after_time = execute_sql_and_time(conn, sql, timeout=30)
            except TimeoutError as e:
                print(f"优化后SQL执行超时: {e}")
                after_time = 30
            except Exception as e:
                print(f"优化后SQL执行失败: {e}")
                rollback_action(conn, index_names, knob_names)
                continue
            print(f"优化后执行时间: {after_time:.4f}秒")
            total_after += after_time
            rollback_action(conn, index_names, knob_names)
        except Exception as e:
            print(f"大模型修复失败: {e}")
            continue
    conn.close()
    print(f"所有SQL优化前总时间: {total_before:.4f}秒")
    print(f"所有SQL优化后总时间: {total_after:.4f}秒")


if __name__ == "__main__":
    start_time = time.time()
    asyncio.run(main())
    end_time = time.time()
    print(f"总修复时间: {end_time - start_time:.4f}秒")
    # fix_ation = '''
    # CREATE INDEX idx_part_name ON part(p_name);
    # CREATE INDEX idx_nation_name ON nation(n_name);
    # CREATE INDEX idx_partsupp_part_supply ON partsupp(ps_partkey, ps_suppkey, ps_availqty);
    # CREATE INDEX idx_lineitem_suppkey_qty ON lineitem(l_suppkey, l_quantity);

    # SET max_parallel_workers_per_gather = 8;
    # SET max_parallel_workers = 8;
    # SET work_mem = '128MB';

    # WITH forest_parts AS (
    #     SELECT p_partkey
    #     FROM part
    #     WHERE p_name LIKE 'forest%'
    # ),
    # min_qty AS (
    #     SELECT l_suppkey, MIN(l_quantity) AS min_qty
    #     FROM lineitem
    #     GROUP BY l_suppkey
    # ),
    # ps_filtered AS (
    #     SELECT ps.ps_suppkey
    #     FROM partsupp ps
    #     JOIN forest_parts f ON ps.ps_partkey = f.p_partkey
    #     JOIN min_qty mq ON ps.ps_suppkey = mq.l_suppkey
    #     WHERE ps.ps_availqty > mq.min_qty
    # )
    # SELECT s.s_name, s.s_address
    # FROM supplier s
    # JOIN ps_filtered pf ON s.s_suppkey = pf.ps_suppkey
    # JOIN nation n ON s.s_nationkey = n.n_nationkey
    # WHERE n.n_name = 'CANADA'
    # ORDER BY s.s_name;
    # '''

    # fix_action, sql = extract_fix_action(fix_ation)
    # print(f"修复动作: {fix_action}")
    # print(f"主SQL: {sql}")
