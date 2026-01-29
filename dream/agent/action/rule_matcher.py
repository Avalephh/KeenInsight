import json
import os
import re


class SQLRuleMatcher:
    def __init__(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.rules_file = os.path.join(current_dir, "rewrite_rules.jsonl")
        self.rules = self._load_rules()

    def _load_rules(self):
        rules = []
        with open(self.rules_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rule = json.loads(line.strip())
                    rules.append(rule)
        return rules

    def _has_union_all(self, sql):
        return bool(re.search(r"\bUNION\s+ALL\b", sql, re.IGNORECASE))

    def _has_order_by(self, sql):
        return bool(re.search(r"\bORDER\s+BY\b", sql, re.IGNORECASE))

    def _has_window_functions(self, sql):
        return bool(re.search(r"\bOVER\s*\(", sql, re.IGNORECASE))

    def _has_distinct_aggregates(self, sql):
        return bool(re.search(r"\b(COUNT|SUM|MIN|MAX|AVG)\s*\(\s*DISTINCT\b", sql, re.IGNORECASE))

    def _has_subqueries(self, sql):
        return bool(re.search(r"\(\s*SELECT\b", sql, re.IGNORECASE))

    def _has_joins(self, sql):
        return bool(re.search(r"\b(INNER|LEFT|RIGHT|FULL)\s+JOIN\b", sql, re.IGNORECASE))

    def _has_where_clauses(self, sql):
        return bool(re.search(r"\bWHERE\b", sql, re.IGNORECASE))

    def _has_group_by(self, sql):
        return bool(re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE))

    def _has_constant_expressions(self, sql):
        order_by_match = re.search(
            r"\bORDER\s+BY\b(.+?)(?:\bLIMIT\b|\bOFFSET\b|$)",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if order_by_match:
            order_clause = order_by_match.group(1)
            return bool(re.search(r"\b\d+\b|\'[^\']*\'", order_clause))
        return False

    def _has_redundant_projections(self, sql):
        select_count = len(re.findall(r"\bSELECT\b", sql, re.IGNORECASE))
        return select_count > 1

    def match_rules(self, sql):
        applicable_rules = []

        if self._has_union_all(sql) and self._has_order_by(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["SORT_UNION_TRANSPOSE", "UNION"]))

        if self._has_subqueries(sql) and self._has_where_clauses(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["FILTER_MERGE", "FILTER_SUB_QUERY"]))

        if self._has_joins(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["JOIN"]))

        if self._has_joins(sql) and self._has_where_clauses(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["JOIN_CONDITION_PUSH"]))

        if self._has_distinct_aggregates(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["AGGREGATE_EXPAND_DISTINCT"]))

        if self._has_window_functions(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["PROJECT_WINDOW_TRANSPOSE", "WINDOW"]))

        if self._has_redundant_projections(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["PROJECT_REMOVE", "PROJECT_MERGE"]))

        if self._has_constant_expressions(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["SORT_REMOVE_CONSTANT", "REDUCE_EXPRESSIONS"]))

        if self._has_group_by(sql):
            applicable_rules.extend(self._get_rules_by_pattern(["AGGREGATE"]))

        seen = set()
        unique_rules = []
        for rule in applicable_rules:
            if rule["name"] not in seen:
                seen.add(rule["name"])
                unique_rules.append(rule)

        return unique_rules

    def _get_rules_by_pattern(self, patterns):
        matched_rules = []
        for rule in self.rules:
            rule_name = rule["name"].upper()
            for pattern in patterns:
                if pattern.upper() in rule_name:
                    matched_rules.append(rule)
                    break
        return matched_rules

    def get_rule_descriptions(self, sql):
        applicable_rules = self.match_rules(sql)

        if not applicable_rules:
            return "    - No specific rewrite rules identified for this query structure."

        descriptions = []
        for rule in applicable_rules:
            descriptions.append(f'    - "{rule["name"]}": {rule["conditions"]} → {rule["transformations"]}')

        return "\n".join(descriptions)

    def get_rule_info(self, rule_name):
        for rule in self.rules:
            if rule["name"] == rule_name:
                return rule
        return None

    def _rule_used_in_rewrite(self, rule, original_query, rewritten_query):
        rule_name = rule["name"]

        if "SORT_UNION_TRANSPOSE" in rule_name:
            return self._check_union_order_by_push(original_query, rewritten_query)
        elif "PROJECT_CORRELATE_TRANSPOSE" in rule_name:
            return self._check_correlated_subquery_projection(original_query, rewritten_query)
        elif "FILTER_MERGE" in rule_name:
            return self._check_where_merge(original_query, rewritten_query)
        elif "JOIN_ADD_REDUNDANT_SEMI_JOIN" in rule_name:
            return self._check_semi_join_addition(original_query, rewritten_query)
        elif "AGGREGATE_EXPAND_DISTINCT_AGGREGATES" in rule_name:
            return self._check_distinct_aggregate_expansion(original_query, rewritten_query)
        elif "AGGREGATE_UNION_TRANSPOSE" in rule_name:
            return self._check_aggregate_union_transpose(original_query, rewritten_query)
        elif "JOIN_CONDITION_PUSH" in rule_name:
            return self._check_join_condition_push(original_query, rewritten_query)
        elif "SORT_REMOVE_CONSTANT_KEYS" in rule_name:
            return self._check_order_by_constant_removal(original_query, rewritten_query)
        elif "AGGREGATE_JOIN_REMOVE" in rule_name:
            return self._check_unnecessary_join_removal(original_query, rewritten_query)
        elif "PROJECT_WINDOW_TRANSPOSE" in rule_name:
            return self._check_window_projection_transpose(original_query, rewritten_query)
        elif "FILTER_VALUES_MERGE" in rule_name:
            return self._check_values_filter_merge(original_query, rewritten_query)
        elif "FILTER_SUB_QUERY_TO_CORRELATE" in rule_name:
            return self._check_subquery_to_correlate(original_query, rewritten_query)
        elif "UNION_REMOVE" in rule_name:
            return self._check_redundant_union_removal(original_query, rewritten_query)
        elif "UNION_TO_DISTINCT" in rule_name:
            return self._check_union_to_distinct(original_query, rewritten_query)
        elif "SORT_REMOVE" in rule_name:
            return self._check_redundant_sort_removal(original_query, rewritten_query)
        elif "PROJECT_REMOVE" in rule_name:
            return self._check_redundant_projection_removal(original_query, rewritten_query)
        elif "JOIN_TO_CORRELATE" in rule_name:
            return self._check_join_to_correlate(original_query, rewritten_query)
        elif "INTERSECT_TO_DISTINCT" in rule_name:
            return self._check_intersect_to_distinct(original_query, rewritten_query)

        return False

    def _check_union_order_by_push(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        if "union" in original_lower and "union" in rewritten_lower:
            original_order_by_count = original_lower.count("order by")
            rewritten_order_by_count = rewritten_lower.count("order by")
            return original_order_by_count != rewritten_order_by_count
        return False

    def _check_correlated_subquery_projection(self, original_query, rewritten_query):
        return (
            "select" in original_query.lower()
            and "select" in rewritten_query.lower()
            and "exists" in original_query.lower()
            or "in " in original_query.lower()
        )

    def _check_where_merge(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        original_where_count = original_lower.count("where")
        rewritten_where_count = rewritten_lower.count("where")
        return original_where_count > rewritten_where_count

    def _check_semi_join_addition(self, original_query, rewritten_query):
        rewritten_lower = rewritten_query.lower()
        return "exists" in rewritten_lower or " in " in rewritten_lower

    def _check_distinct_aggregate_expansion(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "distinct" in original_lower and "distinct" not in rewritten_lower and "group by" in rewritten_lower

    def _check_aggregate_union_transpose(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        agg_functions = ["count", "sum", "avg", "min", "max"]
        original_agg_count = sum(original_lower.count(func) for func in agg_functions)
        rewritten_agg_count = sum(rewritten_lower.count(func) for func in agg_functions)
        return rewritten_agg_count > original_agg_count

    def _check_join_condition_push(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "where" in original_lower and "on " in rewritten_lower

    def _check_order_by_constant_removal(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        if "order by" in original_lower and "order by" in rewritten_lower:
            original_order_by = original_query[original_query.lower().find("order by") :]
            rewritten_order_by = rewritten_query[rewritten_query.lower().find("order by") :]
            return len(rewritten_order_by) < len(original_order_by)
        return False

    def _check_unnecessary_join_removal(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        original_join_count = original_lower.count("join")
        rewritten_join_count = rewritten_lower.count("join")
        return rewritten_join_count < original_join_count

    def _check_window_projection_transpose(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "over(" in original_lower and "over(" in rewritten_lower and "select" in original_lower and "select" in rewritten_lower

    def _check_values_filter_merge(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "values" in original_lower and "values" in rewritten_lower

    def _check_subquery_to_correlate(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "select" in original_lower and "join" in rewritten_lower

    def _check_redundant_union_removal(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        original_union_count = original_lower.count("union")
        rewritten_union_count = rewritten_lower.count("union")
        return rewritten_union_count < original_union_count

    def _check_union_to_distinct(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "union" in original_lower and "distinct" in rewritten_lower and "union" not in rewritten_lower

    def _check_redundant_sort_removal(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "order by" in original_lower and "order by" not in rewritten_lower

    def _check_redundant_projection_removal(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        original_select_count = original_lower.count("select")
        rewritten_select_count = rewritten_lower.count("select")
        return rewritten_select_count < original_select_count

    def _check_join_to_correlate(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "join" in original_lower and ("exists" in rewritten_lower or " in " in rewritten_lower)

    def _check_intersect_to_distinct(self, original_query, rewritten_query):
        original_lower = original_query.lower()
        rewritten_lower = rewritten_query.lower()

        return "intersect" in original_lower and "group by" in rewritten_lower and "count" in rewritten_lower
