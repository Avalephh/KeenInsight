import asyncio
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from agents import Agent, Runner
from agents._config import set_default_openai_api

# from pydantic import BaseModel

# 添加项目根目录到Python路径
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

sys.path.append(str(Path(__file__).parent.parent))

# 导入PostgresDB和QueryInfo
from src.database.pg_env import PostgresDB
from src.utils.types import QueryInfo


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

# PostgreSQL配置
POSTGRES_CONFIG = {
    "user": "postgres",
    "password": "postgres",
    "host": "localhost",
    "port": 5432,
    "dbname": "tpch10G",
    "workload_type": "OLAP",
    "postgres_path": None,
    "postgres_data": None,
    "log_path": None,
}

# 设置大模型API
os.environ["OPENAI_API_KEY"] = ""
os.environ["OPENAI_BASE_URL"] = ""
set_default_openai_api("chat_completions")

# class ActionResult(BaseModel):
#     Explanation: str
#     Fix_Action: str
#     Query_Rewrite: str


class TestPromptManager:
    """SQL测试和修复管理器，参照action_manager.py的结构"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.agent = None
        self.fix_agent = None
        self.exit_stack = AsyncExitStack()
        self._conn = None
        self.postgres_db = None

    async def __aenter__(self):
        """异步上下文管理器的进入方法"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器的退出方法"""

        # 清理数据库连接
        if self._conn:
            try:
                self._conn.close()
            except Exception as e:
                print(f"Warning: Error closing database connection: {e}")

        # 清理 agent
        if self.agent:
            self.agent = None

        if self.fix_agent:
            self.fix_agent = None

        # 清理 exit_stack
        try:
            await self.exit_stack.aclose()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Warning: Error during exit stack cleanup: {e}")

    async def initialize(self):
        """初始化 Agent 和数据库连接"""
        # 初始化PostgresDB
        self.postgres_db = PostgresDB(
            user=POSTGRES_CONFIG["user"],
            password=POSTGRES_CONFIG["password"],
            host=POSTGRES_CONFIG["host"],
            port=POSTGRES_CONFIG["port"],
            dbname=POSTGRES_CONFIG["dbname"],
            workload_type=POSTGRES_CONFIG["workload_type"],
            postgres_path=POSTGRES_CONFIG["postgres_path"],
            postgres_data=POSTGRES_CONFIG["postgres_data"],
            log_path=POSTGRES_CONFIG["log_path"],
        )

        # 初始化数据库连接（保持向后兼容）
        self._conn = psycopg2.connect(**PG_CONN_INFO)

        # # 初始化主Agent
        self.agent = Agent(
            name="SQLFixAgent",
            model="claude-3-5-sonnet-20241022",
            instructions=self._get_agent_instructions(),
            # output_type=ActionResult
        )

        # "gpt-4o-mini"
        # "gpt-4.1"
        # "claude-3-5-sonnet-20241022"
        # "claude-3-7-sonnet-20250219"
        # "claude-sonnet-4-20250514"

        # claude_api_key = "sk-6B29GYJUvOdinhDgD4Be997d5b2a4e399dA5B7F2A275F3B6"
        # claude_client = AsyncOpenAI(base_url="https://api.gpt.ge/v1", api_key=claude_api_key)

        # self.agent = Agent(
        #     name="SQLFixAgent",
        #     instructions=self._get_agent_instructions(),
        #     model=OpenAIChatCompletionsModel(
        #         model="claude-3-7-sonnet-20250219",
        #         openai_client=claude_client,
        #     ),
        #     output_type=ActionResult
        # )

        return self.agent

    def _get_agent_instructions(self) -> str:
        """获取主Agent的指令"""
        return """You are an expert in database performance diagnosis and SQL optimization.
            Your role is to:
            1. Understand detailed database and query execution context.
            2. Identify how the root cause leads to performance degradation.
            3. Output exactly three labeled sections: Explanation, Fix_Action and Query_Rewrite.
            4. Always make the Fix_Action executable directly in the target database.
            5. Never include extra text, comments, or sections beyond the required format.
            6. Always apply the given MCP APIs when producing fixes, if applicable.
            7. Assume the provided execution plan, metrics, and environment info are accurate.
            8. Keep the Explanation concise and technical, avoiding generic descriptions."""

    def get_database_info(self) -> Dict[str, Any]:
        """获取数据库信息"""
        return {
            "db_type": self.postgres_db.db_type,
            "workload_type": self.postgres_db.workload_type,
            "size": self.postgres_db.get_size(),
            "schema": self.postgres_db.fetch_schema_info(),
        }

    @staticmethod
    def extract_index_names(sql: str) -> List[str]:
        """从多条CREATE INDEX语句中提取所有索引名"""
        return re.findall(r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w_]+)", sql, re.IGNORECASE)

    @staticmethod
    def extract_knob_names(sql: str) -> List[str]:
        """从多条SET语句中提取所有knob名"""
        return re.findall(r"SET\s+([a-zA-Z0-9_]+)\s*=", sql, re.IGNORECASE)

    @staticmethod
    def read_sql_file(file_path: str) -> str:
        """读取SQL文件"""
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    def execute_sql_and_time(self, sql: str, timeout: int = 120) -> float:
        """执行SQL并计时"""
        with self._conn.cursor() as cur:
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
                self._conn.commit()
                end = time.time()
                return end - start
            except psycopg2.errors.QueryCanceled as e:
                self._conn.rollback()
                raise TimeoutError(f"SQL执行超时")
            except Exception as e:
                self._conn.rollback()
                raise e

    def get_sql_plan(self, sql: str, timeout: int = 120) -> str:
        """获取SQL执行计划"""
        cmd = f"EXPLAIN ANALYZE {sql}"
        with self._conn.cursor() as cur:
            try:
                cur.execute(f"SET statement_timeout = {int(timeout * 1000)};")
                cur.execute(cmd)
                plan = cur.fetchall()
                return str(plan)
            except psycopg2.errors.QueryCanceled as e:
                self._conn.rollback()
                print(f"SQL执行超时")
                return ""
            except Exception as e:
                print(f"获取SQL计划失败: {e}")
                return ""

    def rollback_action(self, index_names: List[str], knob_names: List[str]):
        """撤销修复动作"""
        # 撤销所有索引
        for index_name in index_names:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(f"DROP INDEX IF EXISTS {index_name}")
                self._conn.commit()
                print(f"已撤销索引: {index_name}")
            except Exception as e:
                self._conn.rollback()
                print(f"撤销索引失败: {e}")

        # 撤销所有knob设置
        for knob_name in knob_names:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(f"RESET {knob_name};")
                self._conn.commit()
                print(f"{knob_name} 已恢复默认值")
            except Exception as e:
                self._conn.rollback()
                print(f"恢复{knob_name}失败: {e}")

    @staticmethod
    def parse_action_result(diagnosis: str):
        """从diagnosis文本中提取Explanation、Fix_Action和Query_Rewrite部分"""

        # 提取Explanation (匹配 Explanation: 或 **Explanation** 或 Explanation)
        explanation_match = re.search(
            r"(?:\*\*?\s*)?Explanation(?:\s*\*\*?)?\s*:?\s*([\s\S]+?)(?=(?:\*\*?\s*)?(?:Fix_Action|SQL\s*Fix|Query_Rewrite|$))",
            diagnosis,
            re.IGNORECASE,
        )
        explanation = explanation_match.group(1).strip() if explanation_match else ""

        # 提取FIX_ACTION (匹配 FIX_ACTION: 或 **SQL Fix** 或 SQL Fix)
        fix_match = re.search(
            r"(?:\*\*?\s*)?(?:Fix_Action|SQL\s*Fix)(?:\s*\*\*?)?\s*:?\s*([\s\S]+?)(?=(?:\*\*?\s*)?(?:Query_Rewrite|Explanation|$))",
            diagnosis,
            re.IGNORECASE,
        )
        fix_action = fix_match.group(1).strip() if fix_match else ""

        # 提取Query_Rewrite (匹配 Query_Rewrite: yes/no)
        is_rewrite_match = re.search(
            r"(?:\*\*?\s*)?Query_Rewrite(?:\s*\*\*?)?\s*:?\s*(yes|no)",
            diagnosis,
            re.IGNORECASE,
        )
        is_rewrite = is_rewrite_match.group(1).strip().lower() if is_rewrite_match else "no"

        # 清理 markdown 符号
        for var_name in ["explanation", "fix_action", "is_rewrite"]:
            val = locals()[var_name]
            val = val.replace("**", "").replace("__", "").strip()
            locals()[var_name] = val

        return explanation, fix_action, is_rewrite

    @staticmethod
    def extract_fix_action(fix_action: str) -> Tuple[str, str]:
        """提取修复动作和主SQL"""
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

    async def call_fix_agent(
        self,
        query_info,
        db_info,
        root_cause,
        action_space_apis,
        historical_cases,
        inspirations,
    ):
        """调用修复Agent"""
        if not self.agent:
            await self.initialize()

        prompt = self._build_prompt(query_info, db_info, root_cause, historical_cases, inspirations)
        print(f"prompt: {prompt}")

        result = await Runner.run(starting_agent=self.agent, input=prompt)
        print(f"claude result: {result.final_output}")
        return result.final_output.strip()

    async def evaluate_action(
        self,
        fix_action: str,
        rewrite_sql: str,
        query_info: QueryInfo,
        max_retry: int = 5,
    ):
        """评估修复动作"""
        # 检查修复SQL能否执行
        success = False
        retry_count = 0
        err = ""

        while not success and retry_count < max_retry:
            try:
                self.execute_sql_and_time(fix_action, timeout=500)
                success = True
            except TimeoutError as e:
                print(f"修复动作执行超时: {e}")
                break
            except Exception as e:
                err = str(e)
                print(f"修复动作执行失败: {e}")

                # # 尝试修复SQL
                # prompt = f"""
                # You have been given a non-executable SQL statement and the exact database error:
                # • Non-executable SQL: {fix_action}
                # • Database Error Message: {err}
                # Your sole objective is to produce a corrected, fully-formed SQL statement (or an ordered sequence of SQL statements) that will run without error.
                # Strict rules — follow exactly:
                # 1) OUTPUT NOTHING BUT THE SQL STATEMENT(S). No explanations, no comments, no markdown, no surrounding quotes.
                # 2) If a single statement suffices, output exactly that statement, terminated by a single semicolon and a newline.
                # 3) If multiple statements are required, output them in execution order, each terminated by a semicolon and a newline.
                # -- Output (strict) --
                # <paste only the corrected fix action statement(s) here, exactly as SQL, nothing else>
                # """
                # result = await Runner.run(starting_agent=self.agent, input=prompt)
                # fix_action = result.final_output
                # print(f"new_fix_action: {fix_action}")
                retry_count += 1

        if not success:
            return {
                "status": -1,
                "msg": f"SQL修复失败: {fix_action}, 错误: {err}",
                "action": fix_action,
                "new_time": -1,
            }

        # 如果有重写SQL，检查结果一致性
        if rewrite_sql:
            # 使用PostgresDB的结果比较功能
            match = self.postgres_db.compare_sql_results(query_info.query, rewrite_sql)
            if not match:
                return {
                    "status": -1,
                    "msg": "修复后结果仍不一致",
                    "action": rewrite_sql,
                    "new_time": -1,
                }

        # 执行优化后的SQL并计时
        try:
            if rewrite_sql:
                new_time = self.execute_sql_and_time(rewrite_sql, timeout=300)
            else:
                new_time = self.execute_sql_and_time(query_info.query, timeout=300)
        except Exception as e:
            return {
                "status": -1,
                "msg": f"优化后SQL执行失败: {e}",
                "action": fix_action,
                "new_time": -1,
            }

        old_time = query_info.execution_time

        # 性能对比
        if (old_time - new_time) / old_time > 0.1:
            return {
                "status": 1,
                "msg": f"修复成功，性能提升，耗时由{old_time:.4f}s降至{new_time:.4f}s",
                "fix_action": fix_action,
                "rewrite_sql": rewrite_sql,
                "new_time": new_time,
            }
        else:
            return {
                "status": 0,
                "msg": f"修复无效，性能未提升或下降，原耗时{old_time:.4f}s，新耗时{new_time:.4f}s",
                "fix_action": fix_action,
                "rewrite_sql": rewrite_sql,
                "new_time": new_time,
            }

    async def process_sql_file(
        self,
        sql_file: str,
        root_cause: str,
        action_space_apis: str,
        historical_cases: str = "",
        inspirations: str = "",
    ):
        """处理单个SQL文件"""
        print(f"处理: {sql_file}")

        # 读取SQL
        sql = self.read_sql_file(sql_file)

        # 使用PostgresDB进行压测获取相关数据
        print("开始压测获取查询信息...")
        result = self.postgres_db.run_sql_and_collect_all(sql, duration=300)

        # 创建QueryInfo对象
        query_info = QueryInfo(
            query_id=0,
            query=result["query"],
            plan_json=result["plan_json"],
            internal_metrics=result["internal_metrics"],
            external_metrics=result["external_metrics"],
            execution_time=result["duration"],
        )

        print(f"优化前执行时间: {query_info.execution_time:.4f}秒")

        # 获取数据库信息
        db_info = self.get_database_info()

        # 调用修复Agent
        try:
            fix_result = await self.call_fix_agent(
                query_info,
                db_info,
                root_cause,
                action_space_apis,
                historical_cases,
                inspirations,
            )
            explanation, fix_action, query_rewrite = self.parse_action_result(fix_result)
            print(f"解释: {explanation}")
            print(f"修复动作: {fix_action}")
            print(f"是否重写SQL: {query_rewrite}")

            # 提取索引名和knob名
            index_names = self.extract_index_names(fix_action)
            knob_names = self.extract_knob_names(fix_action)
            print(f"索引名: {index_names}")
            print(f"knob名: {knob_names}")

            # 处理重写SQL
            rewrite_sql = ""
            if query_rewrite == "yes":
                fix_action, rewrite_sql = self.extract_fix_action(fix_action)
                print(f"修复动作: {fix_action}")
                print(f"重写SQL: {rewrite_sql}")
            else:
                print(f"修复动作: {fix_action}")
            print(f"是否重写SQL: {query_rewrite}")

            # 评估修复动作
            result = await self.evaluate_action(fix_action, rewrite_sql, query_info)

            # 撤销修复动作
            self.rollback_action(index_names, knob_names)

            return result

        except Exception as e:
            print(f"大模型修复失败: {e}")
            return None

    def _build_prompt(self, query_info, db_info, root_cause, historical_cases, inspirations):
        """构建修复提示模板"""
        return f"""
        As an SQL optimization expert, your task is to diagnose and fix the following slow SQL query.
        You have the context of slow SQL and the root cause of the performance degradation as below:

        ## Context
        1. Query Information:
        • SQL: {query_info.query}
        • Execution Plan: {query_info.plan_json}
        • Execution Runtime: {query_info.execution_time} s
        • Internal Metrics: Tuples Returned:{query_info.internal_metrics[0]}, Blocks Hit:{query_info.internal_metrics[1]}, Blocks Read:{query_info.internal_metrics[2]}, Tuples Fetched:{query_info.internal_metrics[3]}, Index Tuples Fetched:{query_info.internal_metrics[4]}, Sequential Tuples Read:{query_info.internal_metrics[5]}, Sequential Scans:{query_info.internal_metrics[6]}, Index Scans:{query_info.internal_metrics[7]}, Heap Blocks Hit:{query_info.internal_metrics[8]}, Heap Blocks Read:{query_info.internal_metrics[9]}, Index Blocks Hit:{query_info.internal_metrics[10]}, Index Blocks Read:{query_info.internal_metrics[11]}

        2. Database Environment Information:
        • Database Type: {db_info.get('db_type')}
        • Workload Type: {db_info.get('workload_type')}
        • Database Size: {db_info.get('size')}
        • Database Schema: {db_info.get('schema')}
        • CPU: {query_info.external_metrics[0]}
        • Read I/O: {query_info.external_metrics[1]}
        • Write I/O: {query_info.external_metrics[2]}
        • Virtual Memory: {query_info.external_metrics[3]}
        • Physical Memory: {query_info.external_metrics[4]}
        • Net Received: {query_info.external_metrics[5]}
        • Net Sent: {query_info.external_metrics[6]}

        3. **Identified Root Cause**:
        {root_cause}

        4. Historical Cases:
        {historical_cases}

        5. Inspirations:
        {inspirations}

        ## Action Space  
        - If the root cause is "inappropriate query knobs", you MUST perform actions within Knob Space using SET knob = value.
        - If the root cause is "suboptimal plan optimizer", you MUST perform actions within Plan Optimizer Space using PostgreSQL comment-style hints (/*+ Set(knob_name value) Set(knob_name value) ... */).
        - If the root cause is "missing indexes", you MUST perform actions within Index Space using CREATE INDEX IF NOT EXISTS or DROP INDEX IF EXISTS.
        - If the root cause is "poorly written queries", you MUST perform actions within Query Rewrite Space using rewrite SQL.
        - **If multiple root causes are identified, you MUST consider all Fix_Action outputs jointly and determine the optimal solution that addresses the combined root causes, while strictly limiting actions to the relevant action spaces of the identified root causes. Do not perform any actions outside of these specified root causes.**

        1. Knob Space:  
        - Knob Space is strictly limited to the following knobs: 
        constraint_exclusion: type: enum, min: null, max: null, default: partition
        cpu_index_tuple_cost: type: float, min: 0.0, max: 10, default: 0.005
        cpu_operator_cost: type: float, min: 0.0, max: 1, default: 0.0025
        cpu_tuple_cost: type: float, min: 0.0, max: 10, default: 0.01
        cursor_tuple_fraction: type: float, min: 0.0, max: 1.0, default: 0.1
        default_statistics_target: type: integer, min: 1, max: 5000, default: 100
        effective_cache_size: type: integer, min: 1, max: 2147483647, default: 524288
        from_collapse_limit: type: integer, min: 1, max: 24, default: 8
        geqo: type: boolean, min: 0, max: 1, default: 0
        hash_mem_multiplier: type: float, min: 1.0, max: 1000.0, default: 2.0
        jit: type: boolean, min: 0, max: 1, default: 1
        jit_above_cost: type: float, min: -1.0, max: 500000, default: 100000.0
        jit_expressions: type: boolean, min: 0, max: 1, default: 1
        jit_inline_above_cost: type: float, min: -1.0, max: 5000000, default: 500000.0
        jit_optimize_above_cost: type: float, min: -1.0, max: 5000000, default: 500000.0
        jit_tuple_deforming: type: boolean, min: 0, max: 1, default: 1
        join_collapse_limit: type: integer, min: 1, max: 24, default: 8
        max_parallel_workers: type: integer, min: 0, max: 1024, default: 8
        max_parallel_workers_per_gather: type: integer, min: 0, max: 1024, default: 2
        min_parallel_index_scan_size: type: integer, min: 0, max: 715827882, default: 64
        min_parallel_table_scan_size: type: integer, min: 0, max: 715827882, default: 1024
        parallel_setup_cost: type: float, min: 0.0, max: 50000.0, default: 1000.0
        parallel_tuple_cost: type: float, min: 0.0, max: 10, default: 0.1
        plan_cache_mode: type: enum, min: null, max: null, default: auto
        random_page_cost: type: float, min: 0.0, max: 100, default: 4.0
        seq_page_cost: type: float, min: 0.0, max: 100, default: 1.0
        work_mem: type: integer, min: 64, max: 2147483647, default: 4096

        2. Plan Optimizer Space: 
        - Plan Optimizer Space is strictly limited to the following knobs: 
        enable_async_append: type: boolean, min: 0, max: 1, default: 1
        enable_bitmapscan: type: boolean, min: 0, max: 1, default: 1
        enable_gathermerge: type: boolean, min: 0, max: 1, default: 1
        enable_hashagg: type: boolean, min: 0, max: 1, default: 1
        enable_hashjoin: type: boolean, min: 0, max: 1, default: 1
        enable_incremental_sort: type: boolean, min: 0, max: 1, default: 1
        enable_indexonlyscan: type: boolean, min: 0, max: 1, default: 1
        enable_indexscan: type: boolean, min: 0, max: 1, default: 1
        enable_material: type: boolean, min: 0, max: 1, default: 1
        enable_memoize: type: boolean, min: 0, max: 1, default: 1
        enable_mergejoin: type: boolean, min: 0, max: 1, default: 1
        enable_nestloop: type: boolean, min: 0, max: 1, default: 1
        enable_parallel_append: type: boolean, min: 0, max: 1, default: 1
        enable_parallel_hash: type: boolean, min: 0, max: 1, default: 1
        enable_partition_pruning: type: boolean, min: 0, max: 1, default: 1
        enable_partitionwise_aggregate: type: boolean, min: 0, max: 1, default: 0
        enable_partitionwise_join: type: boolean, min: 0, max: 1, default: 0
        enable_seqscan: type: boolean, min: 0, max: 1, default: 1
        enable_sort: type: boolean, min: 0, max: 1, default: 1

        3. Index Space: 
        - The naming convention for newly created indexes must follow the pattern: (table_name)_(col1)_(col2)_idx.
        - Index creation should be strictly guided by the slow_sql queries.
        - Avoid generating duplicate or subsumed indexes, and do not drop primary keys, unique indexes, or any indexes that enforce constraints.
        - Existing indexes in the database: 

        4. Query Rewrite Space: 
        - The output must always be a semantically equivalent SELECT statement that produces exactly the same result set as the original query.
        - The following rules are provided in the format ["rule name": "rule description"], consider the rules when rewriting the SQL:
        "name": "SORT_UNION_TRANSPOSE_MATCH_NULL_FETCH", "rewrite_rules_structured": ["conditions": "1. A SQL query performs a `UNION ALL` operation on multiple select statements. 2. Following the `UNION ALL`, there's an `ORDER BY` clause intending to sort the results, with no `OFFSET` clause present in the query.3. Each select statement involved in the `UNION ALL` could potentially benefit from pre-application of the order defined by the `ORDER BY` clause, without violating the intended sort order across the combined result set.", "transformations": "1. Identify the `ORDER BY` clause following a `UNION ALL` that combines multiple select statements and verify there's no associated `OFFSET` directive. 2. For each select statement involved in the `UNION ALL`:   - Apply the `ORDER BY` clause directly to the select statement if doing so does not change the intended order of the final result set. This effectively means prepending an `ORDER BY` to each select statement based on the `ORDER BY` clause initially following the `UNION ALL` operation. 3. Retain the original `ORDER BY` clause following the `UNION ALL`. This ensures that the final result set is ordered as intended, taking into account that individual select statements are now pre-sorted. 4. Implement this transformation only if it's determined that sorting individual select statements before the `UNION ALL` would maintain the overall desired sort order and is expected to either improve performance by reducing sorting overhead or have no negative impact."]

        ## Constraints
        - Allowed optimization methods: change online knobs, add indexes, add query hints, rewrite SQL, or other safe optimization actions.
        - Fix must be executable directly in {db_info.get('db_type')}.
        - The fix should be safe and should not drop or delete data unless explicitly required by the root cause.

        ## Output Format
        You must output ONLY in the following format, with explanation and fix action:

        Explanation:
        <concise technical reason why the root cause is causing the slowdown now>

        Fix_Action:
        <your fix SQL or optimization action here>

        Query_Rewrite:
        <yes or no>

        ## Output Requirements
        - "Explanation" must be a short, precise, technical summary — no generic statements.
        - If the fix rewrites the SQL, "Query_Rewrite" must be "yes", else "no".
        - If rewriting, provide the optimized SQL under Fix_Action.
        - If adding indexes or changing knobs, provide the exact SQL or command.
        - Do not output any extra sections, comments, or steps.
        """


async def main():
    """主函数"""
    sql_dir = "/root/DREAM/data/slow_queries/TPC-H"

    sql_num = "01.sql"

    # issue_types = {
    #     0: "missing indexes",
    #     1: "inappropriate query knobs",
    #     2: "suboptimal plan optimizer",
    #     3: "poorly written queries",
    #     4: "normal"
    # }

    root_cause = "inappropriate query knobs"
    action_space_apis = "indexes_info_collect, indexes_action_space"

    historical_cases = ""
    inspirations = ""

    async with TestPromptManager() as manager:
        sql_file = os.path.join(sql_dir, sql_num)

        result = await manager.process_sql_file(sql_file, root_cause, action_space_apis, historical_cases, inspirations)

        if result:
            if result["status"] == 1:
                print(f"修复成功: {result['msg']}")
            elif result["status"] == 0:
                print(f"修复无效: {result['msg']}")
            else:
                print(f"修复失败: {result['msg']}")


if __name__ == "__main__":
    start_time = time.time()
    asyncio.run(main())
    end_time = time.time()
    print(f"总修复时间: {end_time - start_time:.4f}秒")
