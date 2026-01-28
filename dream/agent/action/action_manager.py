import asyncio
import os
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import nest_asyncio
import numpy as np
import yaml
from agents import Agent, Runner
from agents._config import set_default_openai_api
from agents.mcp import MCPServerSse, MCPServerStdio
from scipy.stats import spearmanr

from dream.agent.action import diagnose_tools
from dream.agent.action.diagnose_tools import SQLRuleMatcher

from dream.agent.prompt import *

nest_asyncio.apply()


# Avoid printing retry logs during LLM API calls
def delete_all_loggers():
    import logging

    loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
    for logger in loggers:
        handlers = logger.handlers[:]
        for handler in handlers:
            logger.removeHandler(handler)
        logger.propagate = True
        logger.setLevel(logging.CRITICAL)


delete_all_loggers()


class ServerConfig:
    def __init__(self, name, server_type, params):
        self.name = name
        self.server_type = server_type
        self.params = params

    @classmethod
    def from_dict(cls, config):
        server_type = MCPServerStdio if config["server_type"] == "stdio" else MCPServerSse
        if "cwd" in config["params"] and config["params"]["cwd"] == ".":
            config["params"]["cwd"] = os.path.dirname(os.path.abspath(__file__))
        return cls(name=config["name"], server_type=server_type, params=config["params"])


class MultiServerManager:
    def __init__(self, configs):
        self.configs = configs
        self.servers = {}

    @asynccontextmanager
    async def manage_servers(self):
        try:
            for config in self.configs:
                server = await config.server_type(name=config.name, params=config.params).__aenter__()
                self.servers[config.name] = server
            yield list(self.servers.values())
        except Exception as e:
            for server in self.servers.values():
                try:
                    await server.__aexit__(type(e), e, e.__traceback__)
                except Exception:
                    pass
            raise
        finally:
            for server in self.servers.values():
                try:
                    await server.__aexit__(None, None, None)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"Warning: Error cleaning up server: {e}")
            self.servers.clear()


class ActionManager:
    def __init__(self, memory_manager, db, planner, configs):
        self.db = db
        self.memory_manager = memory_manager
        self.planner = planner
        self.agent_config = configs.get("AGENT_CONFIG")
        self.configs = configs

        self.agent = None
        self.server_manager = None
        self.exit_stack = AsyncExitStack()
        self._servers = []

        self.fix_model = self.agent_config.get("fix_agent_model")
        self.diagnostic_model = self.agent_config.get("diagnostic_agent_model")
        # Get sql_timeout from DATABASE_CONFIG.query_timeout, fallback to AGENT_CONFIG.sql_execution_timeout
        database_config = configs.get("DATABASE_CONFIG", {})
        self.sql_timeout = database_config.get("query_timeout")
        self.config_path = self.agent_config.get("action_server_config")
        self.fix_timeout = self.agent_config.get("fix_timeout")

        # Ablation experiment parameters
        self.enable_retrieval = self.agent_config.get("enable_retrieval", True)
        self.enable_action_define = self.agent_config.get("enable_action_define", True)
        self.enable_action_pruning = self.agent_config.get("enable_action_pruning", True)

        self.api_settings = self.configs.get("API_SETTINGS")

        if self.api_settings:
            openai_config = self.api_settings.get("openai")
            os.environ["OPENAI_API_KEY"] = openai_config.get("api_key")
            os.environ["OPENAI_BASE_URL"] = openai_config.get("base_url")
            set_default_openai_api("chat_completions")

        self.fixAgent = Agent(
            name="FixAgent",
            model=self.fix_model,
            instructions=get_fix_agent_instructions(),
        )

    async def __aenter__(self):
        """Async context manager entry"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.agent:
            self.agent = None

        try:
            await self.exit_stack.aclose()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Warning: Error during exit stack cleanup: {e}")

        self._servers = []
        self.server_manager = None

    async def initialize(self):
        """Initialize MCP servers and Agent"""
        with open(self.config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        configs = [ServerConfig.from_dict(service_config) for service_config in config_data["services"]]
        self.server_manager = MultiServerManager(configs)

        self._servers = await self.exit_stack.enter_async_context(self.server_manager.manage_servers())

        self.agent = Agent(
            name="DBDiagnosticAgent",
            model=self.diagnostic_model,
            instructions=get_diagnostic_agent_instructions(),
            mcp_servers=self._servers,
        )
        return self.agent

    async def cleanup(self):
        try:
            await self.__aexit__(None, None, None)
        except Exception as e:
            print(f"Warning: Error during cleanup: {e}")

    async def step(self, query_info, root_cause, mode="exploit"):
        """Analyze slow query and generate fix action workflow"""
        if not self.agent:
            await self.initialize()

        # Dynamic workflow:
        # 1. information collect
        # 2. match_history
        # 3. action_define
        # 4. action_generate
        # 5. evaluate_action

        database_config = self.configs.get("DATABASE_CONFIG")
        base_info = diagnose_tools.base_information_collect(query_info, database_config)

        # Retrieve cases based on enable_retrieval flag
        if self.enable_retrieval:
            retrieval = self.memory_manager.retrieve_cases(query_info, root_cause, mode=mode)
            positives = retrieval.get("positive", [])
            negatives = retrieval.get("negative", [])
        else:
            positives = []
            negatives = []

        action_space = await self.action_space_collect(root_cause, query_info)

        action_result = await self.action_generate(root_cause, base_info, action_space, mode, positives, negatives)

        # Extract fix action and rewrite sql
        fix_action, rewrite_sql = self.extract_fix_action(action_result)

        # Validate action
        evaluation_result = await self.evaluate_action(fix_action, rewrite_sql, query_info)

        # Use approve_time as reward (performance improvement: old_time - new_time)
        # approve_time represents the time saved, which is the actual reward signal
        reward = evaluation_result.get("approve_time", 0.0)
        case = positives + negatives

        # Save experience (independent of retrieval mode, only based on enable_save_samples)
        # Note: Experience tuple (s_t, o_t, B_t, r_t, s_{t+1}, B_{t+1}, done)
        # The save_experience method will automatically link experiences with the same query_id:
        # - If there's a pending experience with the same query_id, it will be updated with current state as next state
        # - Current experience will be saved, and if it doesn't have next state, it will be tracked as pending
        if self.memory_manager.enable_save_samples and case:
            for case_info in case:
                # root_causes should be extracted from case_info or use the root_cause parameter
                case_root_causes = case_info.get("root_causes", [])
                if not case_root_causes and root_cause:
                    case_root_causes = root_cause if isinstance(root_cause, list) else [root_cause]
                self.memory_manager.save_experience(
                    query_info,  # Pass QueryInfo object directly, not __dict__
                    case_info, 
                    case_root_causes,
                    reward,
                    next_query_info=None,  # Will be automatically set when next experience with same query_id is saved
                    next_root_causes=None,
                    done=False
                )

        # Only update experience pool and network in dynamic retrieval mode
        # Note: The add_experience method will automatically link experiences with the same query_id:
        # - If there's a pending experience with the same query_id, it will be updated with current state as next state
        # - Current experience will be added, and if it doesn't have next state, it will be tracked as pending
        if self.memory_manager.retrieval_mode == "dynamic" and case:
            for case_info in case:
                case_root_causes = case_info.get("root_causes", [])
                if not case_root_causes and root_cause:
                    case_root_causes = root_cause if isinstance(root_cause, list) else [root_cause]
                self.memory_manager.retriever.add_experience(
                    query_info,  # Pass QueryInfo object directly, not __dict__
                    case_info,
                    case_root_causes,
                    reward,
                    next_query_info=None,  # Will be automatically set when next experience with same query_id is added
                    next_root_causes=None,
                    done=False
                )
            # Update network using TD learning
            self.memory_manager.retriever.update_network()

        return evaluation_result

    @staticmethod
    def extract_index_names(sql):
        return re.findall(r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w_]+)", sql, re.IGNORECASE)

    @staticmethod
    def extract_knob_names(sql):
        return re.findall(r"SET\s+([a-zA-Z0-9_]+)\s*=", sql, re.IGNORECASE)

    def rollback_action(self, index_names, knob_names):
        # undo all index settings
        for index_name in index_names:
            try:
                with self.db.connection.cursor() as cur:
                    cur.execute("SET statement_timeout = 0")
                    cur.execute(f"DROP INDEX IF EXISTS {index_name}")
                self.db.connection.commit()
                print(f"Rolled back index: {index_name}")
            except Exception as e:
                self.db.connection.rollback()
                print(f"Failed to rollback index: {e}")

        # undo all knob settings
        for knob_name in knob_names:
            try:
                with self.db.connection.cursor() as cur:
                    cur.execute("SET statement_timeout = 0")
                    cur.execute(f"RESET {knob_name};")
                self.db.connection.commit()
                print(f"{knob_name} restored to default value")
            except Exception as e:
                self.db.connection.rollback()
                print(f"Failed to restore {knob_name}: {e}")

    async def evaluate_action(self, fix_action, rewrite_sql, query_info):
        """
        1. Check if fix action can be executed: if not, return to let LLM regenerate
        2. Check if results before and after fix action are consistent: if not, return to let LLM regenerate
        3. Check if fix action brings performance improvement
        """
        # Preprocess fix_action / rewrite_sql before execution to get final fix_action and initial fix_sql
        fix_action, fix_sql = await self.derive_fix_action_and_sql(fix_action, rewrite_sql, query_info.query)

        print("Executing fix_action")

        # 1. Check if fix SQL can be executed (if there are DDL/SET fixes, need to execute first)
        if fix_action != "":
            success, _, _, err = self.db.execute(fix_action, timeout=self.fix_timeout)
        else:
            success = True
            err = ""
        print("Fix_action execution completed")

        retry_count = 0
        max_retry = 5
        while not success and retry_count < max_retry:
            prompt = build_fix_prompt(fix_action, err)
            result = await Runner.run(starting_agent=self.fixAgent, input=prompt)
            new_fix_action = result.final_output
            print(f"new_fix_action: {new_fix_action}")
            new_fix_action, new_fix_sql = await self.derive_fix_action_and_sql(new_fix_action, rewrite_sql, query_info.query)
            success, _, _, err = self.db.execute(new_fix_action, timeout=self.fix_timeout)
            fix_action = new_fix_action
            fix_sql = new_fix_sql
            retry_count += 1
        if not success:
            old_time = query_info.execution_time
            approve_time = 0.0  # No improvement if fix failed
            return {
                "status": -1,
                "msg": f"SQL fix failed: {fix_action}, error: {err}",
                "fix_action": fix_action,
                "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
                "new_time": self.sql_timeout,
                "old_time": old_time,
                "approve_time": approve_time,  # reward = approve_time (0.0 for failed fix)
            }

        old_time = query_info.execution_time

        print("Executing performance test")
        # Execute performance test
        success, _, new_time, err = self.db.execute(fix_sql, timeout=self.sql_timeout)
        print("Performance test completed")

        if fix_action != "":
            index_names = self.extract_index_names(fix_action)
            knob_names = self.extract_knob_names(fix_action)
            self.rollback_action(index_names, knob_names)

        # Calculate approve_time (reward): old_time - new_time
        # This represents the performance improvement (time saved)
        approve_time = old_time - new_time if success else 0.0

        if not success:
            if err and isinstance(err, str) and ("timeout" in err.lower() or "querycanceled" in err):
                return {
                    "status": 0,
                    "msg": f"Fix ineffective, performance not improved or degraded, original time {old_time:.4f}s, new time {new_time:.4f}s",
                    "fix_action": fix_action,
                    "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
                    "new_time": new_time,
                    "old_time": old_time,
                    "approve_time": approve_time,  # reward = approve_time
                }
            return {
                "status": -1,
                "msg": f"Performance test SQL execution failed: {err}",
                "fix_action": fix_action,
                "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
                "new_time": self.sql_timeout,
                "old_time": old_time,
                "approve_time": approve_time,  # reward = approve_time (0.0 for failed execution)
            }

        # 4. Performance comparison and subsequent processing
        if (old_time - new_time) / old_time > 0.1:
            return {
                "status": 1,
                "msg": f"Fix successful, performance improved, time reduced from {old_time:.4f}s to {new_time:.4f}s",
                "fix_action": fix_action,
                "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
                "new_time": new_time,
                "old_time": old_time,
                "approve_time": approve_time,  # reward = approve_time
            }
        else:
            return {
                "status": 0,
                "msg": f"Fix ineffective, performance not improved or degraded, original time {old_time:.4f}s, new time {new_time:.4f}s",
                "fix_action": fix_action,
                "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
                "new_time": new_time,
                "old_time": old_time,
                "approve_time": approve_time,  # reward = approve_time (can be negative if performance degraded)
            }

    async def derive_fix_action_and_sql(self, fix_action, rewrite_sql, origin_sql):
        # extract hint
        match = re.search(r"/\*\+[\s\S]*?\*/", fix_action)
        if match:
            hint = match.group(0).strip()
            # remove the hint from fix_action
            clean_fix_action = (fix_action[: match.start()] + fix_action[match.end() :]).strip()
            if rewrite_sql != "":
                rewrite_sql = await self.check_sql_rewrite(origin_sql, rewrite_sql)
                target_sql = rewrite_sql
            else:
                target_sql = origin_sql
            fix_sql = self._inject_hint_into_sql(hint, target_sql)
            return clean_fix_action, fix_sql
        else:
            # no hint
            clean_fix_action = fix_action
            if rewrite_sql != "":
                rewrite_sql = await self.check_sql_rewrite(origin_sql, rewrite_sql)
                fix_sql = rewrite_sql
            else:
                fix_sql = origin_sql
            return clean_fix_action, fix_sql

    def _inject_hint_into_sql(self, hint, sql):
        pattern = r"\b(WITH|SELECT|EXPLAIN)\b"

        def replace_match(match):
            # check if in parentheses
            start_pos = match.start()
            keyword = match.group(1).upper()

            # count the number of left and right parentheses
            left_count = sql[:start_pos].count("(")
            right_count = sql[:start_pos].count(")")

            if left_count > right_count:
                return match.group(0)

            # special handling: if SELECT and in view definition (previous "as" keyword)
            if keyword == "SELECT":
                # simple check: if previous "as" keyword, not inject hint
                before_text = sql[:start_pos].lower().strip()
                if before_text.endswith("as"):
                    return match.group(0)

            # special handling: if WITH, not inject hint (only inject before main SELECT)
            if keyword == "WITH":
                return match.group(0)

            # if previous hint, not add again
            if start_pos > 0:
                prefix = sql[:start_pos].strip()
                if prefix.endswith(hint.strip()):
                    return match.group(0)

            return hint + " " + match.group(0)

        result = re.sub(pattern, replace_match, sql, flags=re.IGNORECASE)
        return result

    async def check_sql_rewrite(self, origin_sql, rewrite_sql):
        print("start QED check")
        qed_match = self.db.check_sql_equivalence(origin_sql, rewrite_sql)
        print(f"QED equivalence check: {qed_match}")

        if qed_match:
            return rewrite_sql

        retry_count = 0
        max_retry = 5
        match = self.db.compare_sql_results(origin_sql, rewrite_sql)
        print(f"Result set comparison match: {match}")

        while not match and retry_count < max_retry:
            prompt = build_rewrite_prompt(origin_sql, rewrite_sql)
            result = await Runner.run(starting_agent=self.fixAgent, input=prompt)
            new_rewrite_sql = result.final_output
            print(f"new_rewrite_sql: {new_rewrite_sql}")
            rewrite_sql = new_rewrite_sql
            retry_count += 1
            match = self.db.compare_sql_results(origin_sql, rewrite_sql)
            print(f"Result set comparison match: {match}")

        if not match:
            print("Failed to rewrite SQL, return original SQL")
            return origin_sql

        return rewrite_sql

    async def action_space_collect(self, root_causes, query_info=None):
        """
        Dynamically collect action space, perform pruning and refinement based on historical cases
        """
        # Build base action space based on enable_action_define flag
        if self.enable_action_define:
            knob_config = self.configs.get("KNOB_CONFIG")
            current_knob_values = self.db.get_current_knob_values(knob_config)
            base_action_space = build_action_space_prompt(root_causes, self.db, knob_config, current_knob_values, query_info)
        else:
            # No action define: provide basic action space without database information
            base_action_space = self._build_basic_action_space(root_causes)

        # Action Pruning: Avoid ineffective operations based on historical cases with same root cause
        if self.enable_action_pruning:
            pruning_guidance = self.action_pruning(root_causes, query_info)
            # Action Refinement: Guide effective search direction based on historical cases with same root cause
            refinement_guidance = self.action_refinement(root_causes, query_info)
            # Integrate pruning and refinement guidance into action space
            enhanced_action_space = self._integrate_pruning_and_refinement(base_action_space, pruning_guidance, refinement_guidance)
        else:
            # No pruning: return base action space without pruning and refinement
            enhanced_action_space = base_action_space

        return enhanced_action_space

    def action_pruning(self, root_causes, query_info):
        has_index_root = "missing indexes" in root_causes
        has_query_root = "poorly written queries" in root_causes

        if not has_index_root and not has_query_root:
            return ""

        pruning_guidance = []
        pruning_guidance.append("• Action Pruning: Based on historical failure cases with the same root cause, the following discrete operations have been proven ineffective, please avoid using them:")

        if has_index_root:
            failed_index_actions = self._get_failed_index_actions(root_causes, query_info)
            if failed_index_actions:
                pruning_guidance.append("The ineffective indexes:")
                for index_name in sorted(failed_index_actions):
                    pruning_guidance.append(f"{index_name}")

        if has_query_root:
            applicable_rules = self._get_applicable_rewrite_rules(query_info)
            if applicable_rules:
                pruning_guidance.append("The ineffective query rewrite rules:")
                for rule in applicable_rules:
                    pruning_guidance.append(f"{rule}")

        return "\n".join(pruning_guidance)

    def _get_failed_index_actions(self, root_causes, query_info):
        """
        Retrieve historical fix_actions with the same root cause from memory_manager, extract failed index names
        """
        # Get historical cases with same root cause
        target_root = self.memory_manager._normalize_root_causes(root_causes)
        target_island_ids = list(self.memory_manager.islands.get(target_root, set()))

        if not target_island_ids:
            return set()

        # Get cases with same root cause
        same_root_cases = self.memory_manager.ids_to_cases(target_island_ids)

        # Further filter cases with same SQL
        query_id = query_info.query_id
        same_sql_cases = []
        for case in same_root_cases:
            if case.case_info.get("query_info").get("query_id") == query_id:
                same_sql_cases.append(case)

        # If no cases with same SQL, use all cases with same root cause
        if not same_sql_cases:
            return set()

        # Extract failed index names
        failed_index_actions = set()
        for case in same_sql_cases:
            # Only process negative cases or cases with no performance improvement
            case_label = case.case_info.get("label")
            if case_label == "negative":
                fix_action = case.case_info.get("fix_action")
                if fix_action:
                    index_names = self.extract_index_names(str(fix_action))
                    for index_name in index_names:
                        failed_index_actions.add(index_name)

        return failed_index_actions

    def _get_applicable_rewrite_rules(self, query_info):
        target_root = self.memory_manager._normalize_root_causes(["poorly written queries"])
        target_island_ids = list(self.memory_manager.islands.get(target_root, set()))

        if not target_island_ids:
            return []

        same_root_cases = self.memory_manager.ids_to_cases(target_island_ids)

        query_id = query_info.query_id
        same_sql_cases = []
        for case in same_root_cases:
            if case.case_info.get("query_info").get("query_id") == query_id:
                same_sql_cases.append(case)

        if not same_sql_cases:
            return []

        failed_rules = set()
        # Use current query as original query (original queries of same SQL cases should be identical)
        original_query = query_info.query

        for case in same_sql_cases:
            case_label = case.case_info.get("label")
            if case_label == "negative":
                # Get rewritten query from historical case
                rewritten_query = case.case_info.get("query_info").get("query")

                rule_matcher = SQLRuleMatcher()

                applicable_rules = rule_matcher.match_rules(original_query)
                for rule in applicable_rules:
                    if rule_matcher._rule_used_in_rewrite(rule, original_query, rewritten_query):
                        failed_rules.add(rule["name"])

        failed_rule_info = []
        for rule_name in failed_rules:
            rule_matcher = SQLRuleMatcher()
            rule_info = rule_matcher.get_rule_info(rule_name)
            if rule_info:
                failed_rule_info.append(rule_info)

        return failed_rule_info

    def action_refinement(self, root_causes, query_info):
        has_knob_root = "inappropriate query knobs" in root_causes
        has_hint_root = "suboptimal plan optimizer" in root_causes

        if not has_knob_root and not has_hint_root:
            return ""

        refinement_guidance = []
        refinement_guidance.append("• Action Refinement: Based on historical success cases with the same root cause, the following continuous operations have been proven effective, suggest prioritizing exploration:")

        if has_knob_root:
            knob_guidance = self._get_knob_refinement_guidance(root_causes, query_info)
            if knob_guidance:
                refinement_guidance.append(knob_guidance)

        if has_hint_root:
            hint_guidance = self._get_hint_refinement_guidance(root_causes, query_info)
            if hint_guidance:
                refinement_guidance.append(hint_guidance)

        return "\n".join(refinement_guidance)

    def _get_knob_refinement_guidance(self, root_causes, query_info, min_threshold=10):
        target_root = self.memory_manager._normalize_root_causes(root_causes)
        target_island_ids = list(self.memory_manager.islands.get(target_root))

        if not target_island_ids:
            return ""

        same_root_cases = self.memory_manager.ids_to_cases(target_island_ids)

        # Use embedding similarity to retrieve similar SQL cases
        similar_cases = self.memory_manager.retrieve_embedding(query_info, top_n=50)
        # Filter similar cases with same root cause
        similar_same_root_cases = []
        for case in similar_cases:
            case_root = self.memory_manager._normalize_root_causes(case.case_info.get("root_causes"))
            if case_root == target_root:
                similar_same_root_cases.append(case)

        if not similar_same_root_cases:
            return ""

        # Filter cases with approve_time > 0
        positive_cases = []
        for case in similar_same_root_cases:
            approve_time = case.case_info.get("approve_time")
            if approve_time and approve_time > 0:
                positive_cases.append(case)

        # Need cases greater than specified threshold to provide suggestions
        if len(positive_cases) < min_threshold:
            return ""

        knob_data = self._extract_knob_data(positive_cases)

        if not knob_data:
            return ""

        guidance = []
        guidance.append("\n• Knob Tuning Space Refinement: Based on historical successful cases, the following knob value ranges are recommended for exploration:")

        for knob_name in knob_data.keys():
            values = knob_data[knob_name]["values"]

            if len(values) < min_threshold:
                continue

            # Find maximum and minimum values
            min_value = min(values)
            max_value = max(values)

            guidance.append(f"- Knob '{knob_name}': Recommended range [{min_value}, {max_value}],  Based on {len(values)} successful historical cases")

        if len(guidance) <= 1:  # Only title, no specific suggestions
            return ""
        else:
            return "\n".join(guidance)

    def _get_hint_refinement_guidance(self, root_causes, query_info):
        target_root = self.memory_manager._normalize_root_causes(root_causes)
        target_island_ids = list(self.memory_manager.islands.get(target_root))

        if not target_island_ids:
            return ""

        same_root_cases = self.memory_manager.ids_to_cases(target_island_ids)

        query_id = query_info.query_id
        same_sql_cases = []
        for case in same_root_cases:
            if case.case_info.get("query_info").get("query_id") == query_id:
                same_sql_cases.append(case)

        if not same_sql_cases:
            return ""

        hint_data = self._extract_hint_usage_data(same_sql_cases)

        if not hint_data:
            return ""

        guidance = []

        positive_hints = []
        for hint_key, hint_stats in hint_data.items():
            if hint_stats["avg_improvement"] > 0:
                positive_hints.append((hint_key, hint_stats))

        if positive_hints:
            guidance.append("\n• Query Hint Refinement: Based on historical hint usage data, the following hints have shown positive performance impact:")
            positive_hints.sort(key=lambda x: x[1]["avg_improvement"], reverse=True)

            for hint_key, hint_stats in positive_hints:
                guidance.append(f"- Hint '{hint_key}': Average improvement {hint_stats['avg_improvement']:.1f}%")

        return "\n".join(guidance)

    def _extract_knob_data(self, cases):
        knob_data = {}

        for case in cases:
            fix_action = case.case_info.get("fix_action")
            knob_settings = self._extract_knob_settings(str(fix_action))
            approve_time = case.case_info.get("approve_time")

            for knob_name, knob_value in knob_settings.items():
                if knob_name not in knob_data:
                    knob_data[knob_name] = {
                        "values": [],
                        "performance": [],
                        "best_value": None,
                        "best_performance": -float("inf"),
                    }

                knob_data[knob_name]["values"].append(knob_value)
                knob_data[knob_name]["performance"].append(approve_time)

                if approve_time > knob_data[knob_name]["best_performance"]:
                    knob_data[knob_name]["best_performance"] = approve_time
                    knob_data[knob_name]["best_value"] = knob_value

        return knob_data

    def _extract_hint_usage_data(self, cases):
        hint_data = {}

        for case in cases:
            query = case.case_info.get("query_info").get("query")

            hints = self._extract_hints_from_action(str(query))
            if not hints:
                continue

            performance_improvement = case.case_info.get("approve_time")

            for hint in hints:
                hint_key = hint["full_hint"]  # Use full hint as key
                if hint_key not in hint_data:
                    hint_data[hint_key] = {
                        "knob": hint["knob"],
                        "value": hint["value"],
                        "improvements": [],
                        "usage_count": 0,
                        "avg_improvement": 0,
                    }

                hint_data[hint_key]["improvements"].append(performance_improvement)
                hint_data[hint_key]["usage_count"] += 1
                hint_data[hint_key]["avg_improvement"] = sum(hint_data[hint_key]["improvements"]) / len(hint_data[hint_key]["improvements"])

        return hint_data

    def _extract_knob_settings(self, action_str):
        knob_settings = {}

        set_pattern = r"set\s+(\w+)\s*=\s*([^\s;]+)"
        matches = re.findall(set_pattern, action_str.lower())

        for knob_name, knob_value in matches:
            knob_settings[knob_name] = knob_value

        return knob_settings

    def _extract_hints_from_action(self, action_str):
        hints = []

        # Pattern to match /*+ ... */
        hint_pattern = r"/\*\+([^*]+)\*/"
        matches = re.findall(hint_pattern, action_str)

        for match in matches:
            set_pattern = r"Set\(([^)]+)\)"
            set_matches = re.findall(set_pattern, match)

            for set_match in set_matches:
                parts = set_match.strip().split()
                if len(parts) >= 2:
                    knob = parts[0]
                    value = " ".join(parts[1:])
                    hints.append(
                        {
                            "knob": knob,
                            "value": value,
                            "full_hint": f"Set({knob} {value})",
                        }
                    )

        return hints

    def _build_basic_action_space(self, root_causes):
        """
        Build basic action space without database information (for no_action_define ablation)
        """
        root_causes = root_causes if isinstance(root_causes, list) else [root_causes]
        action_space_prompt = ""

        # Index related
        if "missing indexes" in root_causes:
            action_space_prompt += """
    • Index Space: 
    - The naming convention for newly created indexes must follow the pattern: (table_name)_(col1)_(col2)_idx.
    - Index creation should be strictly guided by the slow_sql queries.
    - Consider the query execution plan to identify missing indexes that could improve performance.
    - Avoid generating duplicate or subsumed indexes, and do not drop primary keys, unique indexes, or any indexes that enforce constraints.
    - Use CREATE INDEX IF NOT EXISTS or DROP INDEX IF EXISTS to create or drop indexes.
    """

        # Knob related
        if "inappropriate query knobs" in root_causes:
            action_space_prompt += """
    • Knob Space:  
    - You can adjust database configuration parameters using SET knob = value.
    - Focus on parameters that can improve query performance.
    - Be conservative with changes to avoid system instability.
    - Use SET statement to modify database knobs.
    """

        # Execution plan/optimizer related
        if "suboptimal plan optimizer" in root_causes:
            action_space_prompt += """
    • Plan Optimizer Space: 
    - Use PostgreSQL comment-style hints to guide query execution.
    - Output ONLY the hint block like: /*+ Set(knob_name value) Set(knob_name value) ... */
    - Do NOT include the SQL query when using hints.
    - Use hints to force specific join methods, scan types, etc.
    """

        # SQL readability/rewrite related
        if "poorly written queries" in root_causes:
            action_space_prompt += """
    • Query Rewrite Space: 
    - Query Rewrite need to generate a semantically equivalent SELECT statement that produces exactly the same result set as the original query while improving performance.
    - Ensure the rewritten query maintains identical result set and semantic equivalence.
    - Prioritize rules that reduce computational complexity, eliminate redundant operations, or optimize data access patterns.
    - The final output must be a complete, executable SQL statement that preserves all original query semantics while demonstrating measurable performance improvements.
    """

        return action_space_prompt

    def _integrate_pruning_and_refinement(self, base_action_space, pruning_guidance, refinement_guidance):
        """
        Integrate pruning and refinement guidance into action space
        """
        enhanced_space = base_action_space

        if pruning_guidance:
            enhanced_space += f"\n\n{pruning_guidance}"

        if refinement_guidance:
            enhanced_space += f"\n\n{refinement_guidance}"

        return enhanced_space

    def _analyze_knob_pattern(self, x, y):
        x, y = np.array(x), np.array(y)
        if len(x) < 3:
            return "insufficient data", None

        # Calculate Spearman correlation
        corr, _ = spearmanr(x, y)
        if corr < -0.5:
            return "monotonic decreasing", corr
        elif corr > 0.5:
            return "monotonic increasing", corr

        # Find local extrema points
        try:
            minima = argrelextrema(y, np.less)[0]
            maxima = argrelextrema(y, np.greater)[0]

            if len(minima) > 0:
                best_idx = minima[np.argmin(y[minima])]
                return f"local minimum around {x[best_idx]}", (x[best_idx], y[best_idx])
            elif len(maxima) > 0:
                worst_idx = maxima[np.argmax(y[maxima])]
                return f"local maximum around {x[worst_idx]}", (
                    x[worst_idx],
                    y[worst_idx],
                )
            else:
                return "no clear pattern", None
        except Exception:
            return "insufficient data", None

    async def action_generate(self, root_cause, base_info, action_space, mode, positives, negatives):
        prompt = build_action_prompt(root_cause, base_info, action_space, mode, positives, negatives)
        result = await Runner.run(starting_agent=self.agent, input=prompt)
        return result.final_output

    def extract_fix_action(self, action_result):
        """Extract fix_action and rewrite_sql from action_result"""
        explanation, fix_action, is_rewrite = self.parse_action_result(action_result)
        print("Diagnosis output:")
        print("Explanation:", explanation)
        print("Fix action:", fix_action)
        print("Is rewrite:", is_rewrite)
        if is_rewrite == "yes":
            fix_action, rewrite_sql = self.parse_query_rewrite(fix_action)
            return fix_action, rewrite_sql
        else:
            return fix_action, ""

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

    def parse_query_rewrite(self, fix_action):
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
