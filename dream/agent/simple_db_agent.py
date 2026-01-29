import asyncio
import logging
import os
import re
import time
from agents import Agent, Runner
from dream.agent.action.action_manager import ActionManager
from dream.agent.action.action_utils import base_information_collect, parse_query_rewrite
from dream.agent.prompt import (
    build_simple_diagnosis_prompt,
    get_diagnostic_agent_instructions,
)
from dream.database.pg_env import PostgresDB
from dream.database.tidb_env import TiDB
from dream.utils.types import QueryInfo

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = True

class SimpleDBAgent:
    def __init__(self, configs=None):
        self.configs = configs
        self.agent_config = configs.get("AGENT_CONFIG")

        db_type = configs.get("DATABASE_TYPE", "postgres")
        if db_type.lower() == "postgres":
            db_config = configs.get("DATABASE_CONFIG")
            self.db = PostgresDB(db_config)
        elif db_type.lower() == "tidb":
            db_config = configs.get("TiDB_CONFIG")
            self.db = TiDB(db_config)

        self.diagnostic_model = self.agent_config.get("diagnostic_agent_model")
        self.api_settings = self.configs.get("API_SETTINGS")

        if self.api_settings:
            openai_config = self.api_settings.get("openai")
            os.environ["OPENAI_API_KEY"] = openai_config.get("api_key")
            os.environ["OPENAI_BASE_URL"] = openai_config.get("base_url")

        self.diagnostic_agent = Agent(
            name="SimpleDiagnosticAgent",
            model=self.diagnostic_model,
            instructions=get_diagnostic_agent_instructions(),
        )

        self.action_manager = ActionManager(
            memory_manager=None,
            db=self.db,
            planner=None,
            configs=configs,
        )

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.cleanup()

    async def cleanup(self):
        try:
            if self.action_manager:
                try:
                    await asyncio.shield(self.action_manager.cleanup())
                except Exception as e:
                    logger.warning(f"Error cleaning up action_manager: {e}")

            if self.db:
                try:
                    if hasattr(self.db, "cleanup") and callable(getattr(self.db, "cleanup")):
                        await self.db.cleanup()
                    elif hasattr(self.db, "close"):
                        self.db.close()
                except Exception as e:
                    logger.warning(f"Error closing database connection: {e}")

            logger.info("SimpleDBAgent resource cleanup completed")

        except Exception as e:
            logger.error(f"Error during resource cleanup: {e}")
        finally:
            pass

    def parse_action_result(self, diagnosis):
        # Extract Explanation
        explanation_match = re.search(r"Explanation:\s*([\s\S]+?)(?=Fix_Action:|$)", diagnosis, re.IGNORECASE)
        explanation = explanation_match.group(1).strip() if explanation_match else ""

        # Extract FIX_ACTION
        fix_match = re.search(r"Fix_Action:\s*([\s\S]+?)Query_Rewrite:", diagnosis, re.IGNORECASE)
        fix_action = fix_match.group(1).strip() if fix_match else ""

        # Extract Query_Rewrite
        is_rewrite_match = re.search(r"Query_Rewrite:\s*(yes|no)", diagnosis, re.IGNORECASE)
        is_rewrite = is_rewrite_match.group(1).strip() if is_rewrite_match else ""

        return explanation, fix_action, is_rewrite

    def extract_fix_action(self, action_result):
        explanation, fix_action, is_rewrite = self.parse_action_result(action_result)
        print("diagnosis output:")
        print("explanation:", explanation)
        print("fix_action:", fix_action)
        print("is_rewrite:", is_rewrite)
        if is_rewrite == "yes":
            fix_action, rewrite_sql = parse_query_rewrite(fix_action)
            return fix_action, rewrite_sql
        else:
            return fix_action, ""

    def read_queries(self, slow_query_path, order):
        queries_order = []
        with open(os.path.join(slow_query_path, order), "r", encoding="utf-8") as f:
            for fname in f:
                fname = fname.strip()
                if fname.endswith(".sql"):
                    queries_order.append(os.path.join(slow_query_path, fname))
        return queries_order

    def clean_query(self, sql):
        sql = re.sub(r"--.*?(\n|$)", "", sql)
        sql = sql.strip()
        sql = re.sub(r"\s+", " ", sql)
        return sql

    def _remove_hints_from_sql(self, sql):
        import re

        sql_without_hints = re.sub(r"/\*\+[\s\S]*?\*/", "", sql)
        sql_without_hints = " ".join(sql_without_hints.split())
        return sql_without_hints.strip()

    async def simple_diagnosis(self, query_info):
        """Simple LLM-based diagnosis without root cause prediction"""
        # Collect base information
        database_config = self.configs.get("DATABASE_CONFIG")
        base_info = base_information_collect(query_info, database_config)

        # Build simple diagnosis prompt
        prompt = build_simple_diagnosis_prompt(query_info, base_info)

        # Get diagnosis from LLM
        result = await Runner.run(starting_agent=self.diagnostic_agent, input=prompt)
        return result.final_output

    async def run(self, slow_query_path, order, duration, no_improvement_threshold=3):
        """
        Execute simplified database optimization process using only LLM diagnosis

        Args:
            slow_query_path: Path to slow query files
            order: Query order file
            duration: Timeout in hours, will continue optimization until timeout
            no_improvement_threshold: Threshold for no improvement attempts
        """
        # Read workload
        queries_order = self.read_queries(slow_query_path, order)

        best_sql_actions = {}
        best_sql_times = {}
        best_sql_texts = {}

        # Record start time
        start_time = time.time()
        round_idx = 0

        while True:
            current_time = time.time()
            elapsed_time = current_time - start_time

            # Check timeout
            if elapsed_time >= duration * 3600:
                logger.info(f"Reached timeout {duration}h, stopping optimization")
                break

            round_idx += 1
            logger.info(f"=== Round {round_idx} Diagnosis ===")
            round_start = time.time()
            round_sql_times = {}
            round_sql_actions = {}

            for i, query_path in enumerate(queries_order):
                logger.info(f"Processing {i+1}/{len(queries_order)} SQL")

                # Read slow SQL
                query = open(query_path, "r", encoding="utf-8").read()
                query_id = query_path.split("/")[-1].replace(".sql", "")
                query = self.clean_query(query)

                # Use best SQL from history if available, otherwise use original
                original_sql = query
                collected_sql = best_sql_texts.get(query_id, original_sql)

                # Apply previous best actions if they exist
                pre_index_names = []
                pre_knob_names = []
                pre_applied = False
                if query_id in best_sql_actions and best_sql_actions[query_id]:
                    pre_fix_action = best_sql_actions[query_id]
                    success, _, _, _ = self.db.execute(pre_fix_action)
                    if success:
                        pre_index_names = self.action_manager.extract_index_names(pre_fix_action)
                        pre_knob_names = self.action_manager.extract_knob_names(pre_fix_action)
                        pre_applied = True

                # Collect query execution information
                result = self.db.run_sql_and_collect_all(collected_sql)

                # Build QueryInfo
                query_info = QueryInfo(
                    query_id=query_id,
                    query=collected_sql,
                    plan_json=result["plan_json"],
                    internal_metrics=result["internal_metrics"],
                    external_metrics=result["external_metrics"],
                    execution_time=result["duration"],
                    is_rewrite=(collected_sql != original_sql),
                )

                # Initialize best records for new SQL
                if query_id not in best_sql_times:
                    best_sql_times[query_id] = query_info.execution_time
                    best_sql_actions[query_id] = ""
                    best_sql_texts[query_id] = collected_sql

                logger.info(f"SQL {query_id} starting simple LLM diagnosis")

                # Simple LLM diagnosis
                action_result = await self.simple_diagnosis(query_info)

                # Extract fix action
                fix_action, rewrite_sql = self.extract_fix_action(action_result)

                # Validate action
                evaluation_result = await self.action_manager.evaluate_action(fix_action, rewrite_sql, query_info)

                # Get final fix content
                fix_action = evaluation_result.get("fix_action")
                rewrite_sql = evaluation_result.get("rewrite_sql")
                if rewrite_sql != "":
                    # Check if SQL was actually rewritten (excluding hints)
                    original_sql = query_info.query.strip()
                    sql_without_hint = self._remove_hints_from_sql(rewrite_sql.strip())

                    if sql_without_hint != original_sql:
                        query_info.query = rewrite_sql
                        query_info.is_rewrite = True
                    else:
                        query_info.query = rewrite_sql
                        query_info.is_rewrite = False

                # Initialize variables
                new_time = None
                old_time = query_info.execution_time

                # Process evaluation results
                if evaluation_result.get("status") == -1:
                    print(evaluation_result.get("msg"))
                    logger.info(f"SQL {query_id} fix failed, moving to next SQL")
                    # Get timeout from DATABASE_CONFIG or use old_time as fallback
                    database_config = self.configs.get("DATABASE_CONFIG", {})
                    new_time = database_config.get("query_timeout") or old_time
                elif evaluation_result.get("status") == 1:
                    logger.info(f"SQL {query_id} fix successful")
                    new_time = evaluation_result.get("new_time", old_time)
                else:
                    logger.info(f"SQL {query_id} fix ineffective, recording negative case")
                    new_time = evaluation_result.get("new_time", old_time)

                # Update best records only if improvement achieved
                if new_time < best_sql_times[query_id]:
                    best_sql_times[query_id] = new_time
                    best_sql_actions[query_id] = fix_action
                    best_sql_texts[query_id] = query_info.query

                round_sql_times[query_id] = new_time
                round_sql_actions[query_id] = fix_action

                logger.info(f"SQL {query_id} original execution time: {old_time}s")
                logger.info(f"SQL {query_id} current execution time: {new_time}s")
                logger.info(f"SQL {query_id} current fix SQL: {rewrite_sql}")
                logger.info(f"SQL {query_id} current fix actions: {fix_action}")

                # Rollback previous actions after new action evaluation
                if pre_applied:
                    self.action_manager.rollback_action(pre_index_names, pre_knob_names)

            round_end = time.time()
            logger.info(f"Round {round_idx} optimization time: {round_end - round_start:.4f}s")
            round_total_time = sum([t for t in round_sql_times.values()])
            logger.info(f"Round {round_idx} workload total execution time: {round_total_time:.4f}s")
            logger.info(f"Round {round_idx} SQL execution times: {round_sql_times}")

            logger.info(f"Up to round {round_idx}, best SQL execution times: {best_sql_times}")
            logger.info(f"Up to round {round_idx}, workload best total execution time: {sum(best_sql_times.values())}")
            logger.info(f"Up to round {round_idx}, best SQL fix actions: {best_sql_actions}")

        # Output final results
        final_time = time.time()
        total_elapsed = final_time - start_time
        logger.info("=== Optimization Complete ===")
        logger.info(f"Total time: {total_elapsed:.2f}s (timeout limit: {duration}h)")
        logger.info(f"Completed rounds: {round_idx}")
        logger.info(f"Best SQL execution times: {best_sql_times}")
        logger.info(f"Workload best total execution time: {sum(best_sql_times.values()):.4f}s")
        logger.info(f"Best SQL fix actions: {best_sql_actions}")
        logger.info("Diagnosis process ended")
