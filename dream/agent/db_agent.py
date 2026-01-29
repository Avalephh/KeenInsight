import asyncio
import copy
import logging
import os
import random
import re
import time
import numpy as np
from scipy.stats import beta
from dream.agent.action.action_manager import ActionManager
from dream.agent.memory.memory_manager import MemoryManager
from dream.agent.plan.online_predict import RCRankPredictor
from dream.agent.plan.planner import Planner
from dream.database.pg_env import PostgresDB
from dream.database.tidb_env import TiDB
from dream.utils.types import QueryInfo

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = True


class DBAgent:
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

        planner_config = configs.get("PLANNER_CONFIG")
        predictor = RCRankPredictor(planner_config)

        memory_config = configs.get("MEMORY_MANAGER_CONFIG")
        self.memory_manager = MemoryManager(memory_config, predictor=predictor)

        self.planner = Planner(self.memory_manager, predictor=predictor, agent_config=self.agent_config)
        self.action_manager = ActionManager(
            memory_manager=self.memory_manager,
            db=self.db,
            planner=self.planner,
            configs=configs,
        )

        # Dynamic epsilon-greedy parameters
        self.alpha_0 = 1.0
        self.beta_0 = 1.0
        self.p_crit = 0.5
        self.epsilon_min = self.agent_config.get("epsilon", 0.1)
        self.epsilon_max = 0.8
        self.kappa = 0.5
        self.N_th = 3

    def calculate_dynamic_epsilon(self, root_cause_key, state):
        s_r = state.get("successes").get(root_cause_key, 0)  # successes
        n_r = state.get("attempts").get(root_cause_key, 0)  # total attempts

        # Calculate reliability score P_r = 1 - F_Beta(p_crit; alpha_0 + s_r, beta_0 + n_r - s_r)
        if n_r == 0:
            # No attempts yet, use prior
            alpha = self.alpha_0
            beta_param = self.beta_0
        else:
            alpha = self.alpha_0 + s_r
            beta_param = self.beta_0 + n_r - s_r

        P_r = 1 - beta.cdf(self.p_crit, alpha, beta_param)
        U_r = 1 - P_r

        G_nr = 1 / (1 + np.exp(-self.kappa * (n_r - self.N_th)))

        epsilon_r = self.epsilon_min + (self.epsilon_max - self.epsilon_min) * G_nr * U_r

        return epsilon_r

    def decide_exploration_mode(self, query_info, predicted_root, state):
        """
        Decide between exploration and exploitation based on dynamic epsilon-greedy

        Args:
            query_info: Current query information
            predicted_root: Predicted root cause
            state: Current SQL state

        Returns:
            mode: 'explore' or 'exploit'
        """
        predicted_root_key = self.memory_manager._normalize_root_causes(predicted_root)

        # Calculate dynamic epsilon for the predicted root cause
        epsilon_r = self.calculate_dynamic_epsilon(predicted_root_key, state)
        print(f"epsilon_r: {epsilon_r}")

        # Sample p ~ U(0,1) and decide
        p = random.random()
        mode = "explore" if p < epsilon_r else "exploit"

        return mode

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

            logger.info("DBAgent resource cleanup completed")

        except Exception as e:
            logger.error(f"Error during resource cleanup: {e}")
        finally:
            pass

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

    async def run(self, slow_query_path, order, duration, no_improvement_threshold=3):
        """
        Execute database optimization process

        Args:
            slow_query_path: Path to slow query files
            order: Query order file
            duration: Timeout in hours, will continue optimization until timeout
            no_improvement_threshold: Threshold for no improvement attempts, switch root cause when reached
        """
        # Read workload
        queries_order = self.read_queries(slow_query_path, order)

        best_sql_actions = {}
        best_sql_times = {}
        best_sql_texts = {}

        # Track simplified state for each SQL
        # {query_id: {"root_tried": set[str], "current_root": Any, "mode": 'exploit'|'explore', "confidence": Dict[str,float], "attempts": Dict[str,int], "successes": Dict[str,int]}}
        sql_root_cause_states = {}

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
            # Set current round in memory_manager for generating next_query_info_id
            self.memory_manager.set_current_round(round_idx)
            round_start = time.time()
            round_sql_times = {}
            round_sql_actions = {}
            for i, query_path in enumerate(queries_order):
                logger.info(f"Processing {i+1}/{len(queries_order)} SQL")

                # Read slow SQL
                query = open(query_path, "r", encoding="utf-8").read()
                query_id = query_path.split("/")[-1].replace(".sql", "")
                query = self.clean_query(query)

                # Select SQL for diagnosis: prefer best SQL from history (with rewrite/hint), otherwise use original
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
                try:
                    result = self.db.run_sql_and_collect_all(collected_sql)
                except Exception as e:
                    logger.error(f"SQL {query_id} error collecting SQL execution data: {e}", exc_info=True)
                    database_config = self.configs.get("DATABASE_CONFIG", {})
                    timeout_time = database_config.get("query_timeout")
                    round_sql_times[query_id] = timeout_time
                    round_sql_actions[query_id] = ""
                    logger.info(f"SQL {query_id} skipping current SQL, continuing to next")
                    continue

                logger.info(f"SQL {query_id} collected SQL execution time: {result['duration']}s")

                # Build QueryInfo; if collected_sql differs from original_sql, treat as rewrite
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

                # Initialize state
                if query_id not in sql_root_cause_states:
                    sql_root_cause_states[query_id] = {
                        "root_tried": set(),
                        "current_root": None,
                        "mode": "exploit",
                        "confidence": {},
                        "attempts": {},
                        "successes": {},
                        "component_attempts": {},
                    }

                state = sql_root_cause_states[query_id]

                # Predict root cause
                logger.info(f"SQL {query_id} starting root cause prediction")
                # Save state backup for exception recovery
                state_backup = copy.deepcopy(state)
                try:
                    predicted_root, state = self.planner.predict(query_info, state, self.memory_manager)
                except Exception as e:
                    logger.error(f"SQL {query_id} error during root cause prediction: {e}", exc_info=True)
                    sql_root_cause_states[query_id] = state_backup
                    round_sql_times[query_id] = query_info.execution_time
                    round_sql_actions[query_id] = ""
                    logger.info(f"SQL {query_id} skipping current SQL, continuing to next")
                    continue

                # Check if predicted root cause is already in root_tried
                predicted_root_key = self.memory_manager._normalize_root_causes(predicted_root)
                if predicted_root_key in state["root_tried"]:
                    logger.info(f"SQL {query_id} predicted root cause already in root_tried, forcing exploration mode")
                    state["mode"] = "explore"
                else:
                    # Dynamic epsilon-greedy decision
                    state["mode"] = self.decide_exploration_mode(query_info, predicted_root, state)
                    logger.info(f"SQL {query_id} dynamic epsilon decision")

                if state["mode"] == "explore":
                    logger.info(f"SQL {query_id} entering exploration mode (archive similar root causes)")
                    exclude = {str(self.memory_manager._normalize_root_causes(k)) for k in state["root_tried"]}
                    next_root = self.memory_manager.explore_root_from_archive(query_info, exclude_roots=exclude)
                    if not next_root:
                        all_roots = [list(r) for r in self.memory_manager.island_keys]  # List[List[str]]
                        candidates = [r for r in all_roots if str(self.memory_manager._normalize_root_causes(r)) not in exclude]
                        if candidates == []:
                            logger.info(f"SQL {query_id} all root causes have been explored, ending exploration")
                            continue
                        else:
                            next_root = random.choice(candidates)
                    state["current_root"] = next_root
                else:
                    logger.info(f"SQL {query_id} entering exploitation mode (using predicted root cause)")
                    state["current_root"] = predicted_root

                root_causes = state["current_root"]
                if not root_causes:
                    logger.info(f"SQL {query_id} no available root cause this round, skipping to next SQL")
                    continue

                logger.info(f"SQL {query_id} current diagnosis root cause: {root_causes}")

                # Call action_manager.step to process (includes evaluate_action and experience pool update)
                try:
                    evaluation_result = await self.action_manager.step(query_info, root_causes, mode=state["mode"])
                except Exception as e:
                    logger.error(f"SQL {query_id} error during step execution: {e}", exc_info=True)
                    new_time = query_info.execution_time
                    round_sql_times[query_id] = new_time
                    round_sql_actions[query_id] = ""
                    sql_root_cause_states[query_id] = state
                    logger.info(f"SQL {query_id} skipping current SQL, continuing to next")
                    continue

                # Get fix content
                fix_action = evaluation_result.get("fix_action")
                rewrite_sql = evaluation_result.get("rewrite_sql")
                if rewrite_sql != "":
                    # Check if SQL was actually rewritten (excluding hints)
                    original_sql = query_info.query.strip()
                    sql_without_hint = self._remove_hints_from_sql(rewrite_sql.strip())

                    # Only mark as rewrite if SQL without hints differs from original SQL
                    if sql_without_hint != original_sql:
                        query_info.query = rewrite_sql
                        query_info.is_rewrite = True
                    else:
                        # If only hints were added, don't mark as rewrite
                        query_info.query = rewrite_sql
                        query_info.is_rewrite = False

                # Initialize variables
                new_time = None
                old_time = query_info.execution_time

                # Process evaluation results and update root cause state
                if evaluation_result.get("status") == -1:
                    error_msg = evaluation_result.get("msg", "Unknown error")
                    print(f"Error: {error_msg}")
                    logger.info(f"SQL {query_id} fix failed, moving to next SQL")
                    database_config = self.configs.get("DATABASE_CONFIG", {})
                    new_time = database_config.get("query_timeout") or old_time

                    root_causes_list = root_causes if isinstance(root_causes, list) else [root_causes]
                    for rc in root_causes_list:
                            state["component_attempts"][rc] = state["component_attempts"].get(rc, 0) + 1
                    
                    case_id = self.memory_manager.save_case(
                        query_info,
                        fix_action,
                        root_causes,
                        old_time,
                        new_time,
                        "negative",
                        tuning_attempts=state["component_attempts"].copy(),
                    )
                    # Get corrected root cause
                    case = self.memory_manager.get_case(case_id)
                    root_causes = case.case_info.get("root_causes")
                    # Record one invalid attempt for this root cause
                    key = self.memory_manager._normalize_root_causes(root_causes)
                    state["attempts"][key] = state["attempts"].get(key, 0) + 1
                elif evaluation_result.get("status") == 1:
                    logger.info(f"SQL {query_id} fix successful")
                    new_time = evaluation_result.get("new_time")

                    root_causes_list = root_causes if isinstance(root_causes, list) else [root_causes]
                    for rc in root_causes_list:
                            state["component_attempts"][rc] = state["component_attempts"].get(rc, 0) + 1

                    case_id = self.memory_manager.save_case(
                        query_info,
                        fix_action,
                        root_causes,
                        old_time,
                        new_time,
                        "positive",
                        tuning_attempts=state["component_attempts"].copy(),
                    )
                    # Get corrected root cause
                    case = self.memory_manager.get_case(case_id)
                    root_causes = case.case_info.get("root_causes")

                    # Success: accumulate success count
                    key = self.memory_manager._normalize_root_causes(root_causes)
                    state["attempts"][key] = state["attempts"].get(key, 0) + 1
                    state["successes"][key] = state["successes"].get(key, 0) + 1
                    # After success, clear current_root to allow small model to re-diagnose next time (refresh root cause)
                    state["current_root"] = None
                    # state["confidence"] = {}
                else:
                    # Fix ineffective: record negative case and advance source (try again next round)
                    logger.info(f"SQL {query_id} fix ineffective, recording negative case")
                    new_time = evaluation_result.get("new_time", old_time)

                    root_causes_list = root_causes if isinstance(root_causes, list) else [root_causes]
                    for rc in root_causes_list:
                            state["component_attempts"][rc] = state["component_attempts"].get(rc, 0) + 1
                    
                    case_id = self.memory_manager.save_case(
                        query_info,
                        fix_action,
                        root_causes,
                        old_time,
                        new_time,
                        "negative",
                        tuning_attempts=state["component_attempts"].copy(),
                    )
                    # Get corrected root cause
                    case = self.memory_manager.get_case(case_id)
                    root_causes = case.case_info.get("root_causes")
                    # Record one invalid attempt for this root cause
                    key = self.memory_manager._normalize_root_causes(root_causes)
                    state["attempts"][key] = state["attempts"].get(key, 0) + 1

                # When invalid attempt count reaches threshold, mark root cause as tried and clear current_root
                if state.get("attempts", {}).get(key, 0) >= no_improvement_threshold:
                    state["root_tried"].add(key)

                # Update best records only if fix is successful and better
                if new_time < best_sql_times[query_id]:
                    best_sql_times[query_id] = new_time
                    best_sql_actions[query_id] = fix_action
                    best_sql_texts[query_id] = query_info.query
                round_sql_times[query_id] = new_time
                round_sql_actions[query_id] = fix_action
                sql_root_cause_states[query_id] = state

                logger.info(f"SQL {query_id} original execution time: {old_time}s")
                logger.info(f"SQL {query_id} current execution time: {new_time}s")
                logger.info(f"SQL {query_id} current fix SQL: {rewrite_sql}")
                logger.info(f"SQL {query_id} current diagnosis root cause: {root_causes}")
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
