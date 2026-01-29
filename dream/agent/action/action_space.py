import re

from dream.agent.action.action_utils import extract_index_names
from dream.agent.action.rule_matcher import SQLRuleMatcher
from dream.agent.prompt import build_action_space_prompt


async def action_space_collect(
    root_causes,
    query_info=None,
    *,
    enable_action_define,
    enable_action_pruning,
    configs,
    db,
    memory_manager,
):
    if enable_action_define:
        knob_config = configs.get("KNOB_CONFIG")
        current_knob_values = db.get_current_knob_values(knob_config)
        base_action_space = build_action_space_prompt(
            root_causes, db, knob_config, current_knob_values, query_info
        )
    else:
        base_action_space = _build_basic_action_space(root_causes)

    if enable_action_pruning:
        pruning_guidance = action_pruning(root_causes, query_info, memory_manager)
        refinement_guidance = action_refinement(root_causes, query_info, memory_manager)
        enhanced_action_space = _integrate_pruning_and_refinement(
            base_action_space, pruning_guidance, refinement_guidance
        )
    else:
        enhanced_action_space = base_action_space

    return enhanced_action_space


def action_pruning(root_causes, query_info, memory_manager):
    has_index_root = "missing indexes" in root_causes
    has_query_root = "poorly written queries" in root_causes

    if not has_index_root and not has_query_root:
        return ""

    pruning_guidance = []
    pruning_guidance.append(
        "• Action Pruning: Based on historical failure cases with the same root cause, "
        "the following discrete operations have been proven ineffective, please avoid using them:"
    )

    if has_index_root:
        failed_index_actions = _get_failed_index_actions(root_causes, query_info, memory_manager)
        if failed_index_actions:
            pruning_guidance.append("The ineffective indexes:")
            for index_name in sorted(failed_index_actions):
                pruning_guidance.append(f"{index_name}")

    if has_query_root:
        applicable_rules = _get_applicable_rewrite_rules(query_info, memory_manager)
        if applicable_rules:
            pruning_guidance.append("The ineffective query rewrite rules:")
            for rule in applicable_rules:
                pruning_guidance.append(f"{rule}")

    return "\n".join(pruning_guidance)


def _get_failed_index_actions(root_causes, query_info, memory_manager):
    target_root = memory_manager._normalize_root_causes(root_causes)
    target_island_ids = list(memory_manager.islands.get(target_root, set()))

    if not target_island_ids:
        return set()

    same_root_cases = memory_manager.ids_to_cases(target_island_ids)

    query_id = query_info.query_id
    same_sql_cases = []
    for case in same_root_cases:
        if case.case_info.get("query_info").get("query_id") == query_id:
            same_sql_cases.append(case)

    if not same_sql_cases:
        return set()

    failed_index_actions = set()
    for case in same_sql_cases:
        case_label = case.case_info.get("label")
        if case_label == "negative":
            fix_action = case.case_info.get("fix_action")
            if fix_action:
                index_names = extract_index_names(str(fix_action))
                for index_name in index_names:
                    failed_index_actions.add(index_name)

    return failed_index_actions


def _get_applicable_rewrite_rules(query_info, memory_manager):
    target_root = memory_manager._normalize_root_causes(["poorly written queries"])
    target_island_ids = list(memory_manager.islands.get(target_root, set()))

    if not target_island_ids:
        return []

    same_root_cases = memory_manager.ids_to_cases(target_island_ids)

    query_id = query_info.query_id
    same_sql_cases = []
    for case in same_root_cases:
        if case.case_info.get("query_info").get("query_id") == query_id:
            same_sql_cases.append(case)

    if not same_sql_cases:
        return []

    failed_rules = set()
    original_query = query_info.query

    for case in same_sql_cases:
        case_label = case.case_info.get("label")
        if case_label == "negative":
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


def action_refinement(root_causes, query_info, memory_manager):
    has_knob_root = "inappropriate query knobs" in root_causes
    has_hint_root = "suboptimal plan optimizer" in root_causes

    if not has_knob_root and not has_hint_root:
        return ""

    refinement_guidance = []
    refinement_guidance.append(
        "• Action Refinement: Based on historical success cases with the same root cause, "
        "the following continuous operations have been proven effective, suggest prioritizing exploration:"
    )

    if has_knob_root:
        knob_guidance = _get_knob_refinement_guidance(root_causes, query_info, memory_manager)
        if knob_guidance:
            refinement_guidance.append(knob_guidance)

    if has_hint_root:
        hint_guidance = _get_hint_refinement_guidance(root_causes, query_info, memory_manager)
        if hint_guidance:
            refinement_guidance.append(hint_guidance)

    return "\n".join(refinement_guidance)


def _get_knob_refinement_guidance(root_causes, query_info, memory_manager, min_threshold=10):
    target_root = memory_manager._normalize_root_causes(root_causes)
    target_island_ids = list(memory_manager.islands.get(target_root))

    if not target_island_ids:
        return ""

    similar_cases = memory_manager.retrieve_embedding(query_info, top_n=50)
    similar_same_root_cases = []
    for case in similar_cases:
        case_root = memory_manager._normalize_root_causes(case.case_info.get("root_causes"))
        if case_root == target_root:
            similar_same_root_cases.append(case)

    if not similar_same_root_cases:
        return ""

    positive_cases = []
    for case in similar_same_root_cases:
        approve_time = case.case_info.get("approve_time")
        if approve_time and approve_time > 0:
            positive_cases.append(case)

    if len(positive_cases) < min_threshold:
        return ""

    knob_data = _extract_knob_data(positive_cases)

    if not knob_data:
        return ""

    guidance = []
    guidance.append(
        "\n• Knob Tuning Space Refinement: Based on historical successful cases, "
        "the following knob value ranges are recommended for exploration:"
    )

    for knob_name in knob_data.keys():
        values = knob_data[knob_name]["values"]

        if len(values) < min_threshold:
            continue

        min_value = min(values)
        max_value = max(values)

        guidance.append(
            f"- Knob '{knob_name}': Recommended range [{min_value}, {max_value}],  "
            f"Based on {len(values)} successful historical cases"
        )

    if len(guidance) <= 1:
        return ""
    return "\n".join(guidance)


def _get_hint_refinement_guidance(root_causes, query_info, memory_manager):
    target_root = memory_manager._normalize_root_causes(root_causes)
    target_island_ids = list(memory_manager.islands.get(target_root))

    if not target_island_ids:
        return ""

    same_root_cases = memory_manager.ids_to_cases(target_island_ids)

    query_id = query_info.query_id
    same_sql_cases = []
    for case in same_root_cases:
        if case.case_info.get("query_info").get("query_id") == query_id:
            same_sql_cases.append(case)

    if not same_sql_cases:
        return ""

    hint_data = _extract_hint_usage_data(same_sql_cases)

    if not hint_data:
        return ""

    guidance = []

    positive_hints = []
    for hint_key, hint_stats in hint_data.items():
        if hint_stats["avg_improvement"] > 0:
            positive_hints.append((hint_key, hint_stats))

    if positive_hints:
        guidance.append(
            "\n• Query Hint Refinement: Based on historical hint usage data, the "
            "following hints have shown positive performance impact:"
        )
        positive_hints.sort(key=lambda x: x[1]["avg_improvement"], reverse=True)

        for hint_key, hint_stats in positive_hints:
            guidance.append(f"- Hint '{hint_key}': Average improvement {hint_stats['avg_improvement']:.1f}%")

    return "\n".join(guidance)


def _extract_knob_data(cases):
    knob_data = {}

    for case in cases:
        fix_action = case.case_info.get("fix_action")
        knob_settings = _extract_knob_settings(str(fix_action))
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


def _extract_hint_usage_data(cases):
    hint_data = {}

    for case in cases:
        query = case.case_info.get("query_info").get("query")

        hints = _extract_hints_from_action(str(query))
        if not hints:
            continue

        performance_improvement = case.case_info.get("approve_time")

        for hint in hints:
            hint_key = hint["full_hint"]
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
            hint_data[hint_key]["avg_improvement"] = sum(hint_data[hint_key]["improvements"]) / len(
                hint_data[hint_key]["improvements"]
            )

    return hint_data


def _extract_knob_settings(action_str):
    knob_settings = {}

    set_pattern = r"set\s+(\w+)\s*=\s*([^\s;]+)"
    matches = re.findall(set_pattern, action_str.lower())

    for knob_name, knob_value in matches:
        knob_settings[knob_name] = knob_value

    return knob_settings


def _extract_hints_from_action(action_str):
    hints = []

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


def _build_basic_action_space(root_causes):
    root_causes = root_causes if isinstance(root_causes, list) else [root_causes]
    action_space_prompt = ""

    if "missing indexes" in root_causes:
        action_space_prompt += """
    • Index Space: 
    - The naming convention for newly created indexes must follow the pattern: (table_name)_(col1)_(col2)_idx.
    - Index creation should be strictly guided by the slow_sql queries.
    - Consider the query execution plan to identify missing indexes that could improve performance.
    - Avoid generating duplicate or subsumed indexes, and do not drop primary keys, unique indexes, or any indexes that enforce constraints.
    - Use CREATE INDEX IF NOT EXISTS or DROP INDEX IF EXISTS to create or drop indexes.
    """

    if "inappropriate query knobs" in root_causes:
        action_space_prompt += """
    • Knob Space:  
    - You can adjust database configuration parameters using SET knob = value.
    - Focus on parameters that can improve query performance.
    - Be conservative with changes to avoid system instability.
    - Use SET statement to modify database knobs.
    """

    if "suboptimal plan optimizer" in root_causes:
        action_space_prompt += """
    • Plan Optimizer Space: 
    - Use PostgreSQL comment-style hints to guide query execution.
    - Output ONLY the hint block like: /*+ Set(knob_name value) Set(knob_name value) ... */
    - Do NOT include the SQL query when using hints.
    - Use hints to force specific join methods, scan types, etc.
    """

    if "poorly written queries" in root_causes:
        action_space_prompt += """
    • Query Rewrite Space: 
    - Query Rewrite need to generate a semantically equivalent SELECT statement that produces exactly the same result set as the original query while improving performance.
    - Ensure the rewritten query maintains identical result set and semantic equivalence.
    - Prioritize rules that reduce computational complexity, eliminate redundant operations, or optimize data access patterns.
    - The final output must be a complete, executable SQL statement that preserves all original query semantics while demonstrating measurable performance improvements.
    """

    return action_space_prompt


def _integrate_pruning_and_refinement(base_action_space, pruning_guidance, refinement_guidance):
    enhanced_space = base_action_space

    if pruning_guidance:
        enhanced_space += f"\n\n{pruning_guidance}"

    if refinement_guidance:
        enhanced_space += f"\n\n{refinement_guidance}"

    return enhanced_space
