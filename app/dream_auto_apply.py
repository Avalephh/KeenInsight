#!/usr/bin/env python3
"""Parse and route validated DREAM actions for repeat SQL executions.

PostgreSQL can apply plan hints and planner GUCs to an unchanged statement
through pg_hint_plan's hint table.  A semantically rewritten SQL statement,
however, has to pass through a caller that can replace the statement text.
This module keeps those two application mechanisms explicit while sharing one
strict parser between the bridge and the experiment controller.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Tuple


SESSION_SETTING_NAMES = frozenset({
    "work_mem",
    "maintenance_work_mem",
    "temp_buffers",
    "max_parallel_workers",
    "max_parallel_workers_per_gather",
    "parallel_setup_cost",
    "parallel_tuple_cost",
    "random_page_cost",
    "seq_page_cost",
    "cpu_index_tuple_cost",
    "cpu_operator_cost",
    "cpu_tuple_cost",
    "effective_cache_size",
    "default_statistics_target",
    "cursor_tuple_fraction",
    "join_collapse_limit",
    "from_collapse_limit",
    "geqo",
    "geqo_threshold",
    "enable_bitmapscan",
    "enable_hashagg",
    "enable_hashjoin",
    "enable_indexonlyscan",
    "enable_indexscan",
    "enable_material",
    "enable_mergejoin",
    "enable_nestloop",
    "enable_seqscan",
    "enable_sort",
    "jit",
})

# pg_hint_plan's Set() directive changes a GUC while PostgreSQL is planning
# the statement and restores it before execution.  Planner switches and cost
# constants are therefore suitable for direct hint-table application.  Memory
# limits and other executor/session settings must be installed on the backend
# that actually executes the SQL, otherwise an improvement can be reported as
# active while PostgreSQL silently runs with the old value.
PG_HINT_PLAN_SETTING_NAMES = frozenset({
    "max_parallel_workers_per_gather",
    "parallel_setup_cost",
    "parallel_tuple_cost",
    "random_page_cost",
    "seq_page_cost",
    "cpu_index_tuple_cost",
    "cpu_operator_cost",
    "cpu_tuple_cost",
    "effective_cache_size",
    "cursor_tuple_fraction",
    "join_collapse_limit",
    "from_collapse_limit",
    "geqo",
    "geqo_threshold",
    "enable_bitmapscan",
    "enable_hashagg",
    "enable_hashjoin",
    "enable_indexonlyscan",
    "enable_indexscan",
    "enable_material",
    "enable_mergejoin",
    "enable_nestloop",
    "enable_seqscan",
    "enable_sort",
})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SETTING_VALUE = re.compile(r"^[A-Za-z0-9_.+/%-]+$")
_HINT_COMMENT = re.compile(r"/\*\+([\s\S]*?)\*/")
_ALL_COMMENTS = re.compile(r"--[^\n]*|/\*[\s\S]*?\*/")
_SQL_STRING = re.compile(r"'(?:''|[^'])*'")
_LEADING_SET = re.compile(
    r"^\s*SET(?:\s+(?:LOCAL|SESSION))?\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|TO)\s*"
    r"((?:'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[^;])+);\s*",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"^\s*```(?:sql|postgresql)?\s*([\s\S]*?)\s*```\s*$", re.IGNORECASE)


def _strip_code_fence(value: str) -> str:
    text = str(value or "").strip()
    match = _CODE_FENCE.match(text)
    return match.group(1).strip() if match else text


def _setting_value(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        quote = text[0]
        text = text[1:-1]
        text = text.replace(quote * 2, quote)
    text = text.strip()
    if not text or not _SETTING_VALUE.fullmatch(text):
        raise ValueError("unsupported DREAM session setting value: {!r}".format(value))
    return text


def split_leading_settings(value: str) -> Tuple[Dict[str, str], str]:
    """Return supported leading SET statements and the remaining SQL/action."""

    settings: Dict[str, str] = {}
    remaining = _strip_code_fence(value)
    while remaining:
        match = _LEADING_SET.match(remaining)
        if not match:
            break
        name = match.group(1).lower()
        if name not in SESSION_SETTING_NAMES:
            raise ValueError("unsupported DREAM session setting: {}".format(name))
        normalized = _setting_value(match.group(2))
        previous = settings.get(name)
        if previous is not None and previous != normalized:
            raise ValueError("conflicting DREAM session setting: {}".format(name))
        settings[name] = normalized
        remaining = remaining[match.end() :].strip()
    return settings, remaining


def is_read_only_statement(value: str) -> bool:
    """Conservatively accept one SELECT/WITH/VALUES/EXPLAIN statement."""

    text = _strip_code_fence(value)
    text = _ALL_COMMENTS.sub(" ", text).strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text or ";" in text:
        return False
    without_strings = _SQL_STRING.sub(" ", text).lower().strip()
    if not without_strings.startswith(("select", "with", "values", "explain")):
        return False
    forbidden = (
        r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|"
        r"copy|call|do|vacuum|refresh|set|reset|begin|commit|rollback|prepare|"
        r"execute|lock|into|nextval|setval)\b"
    )
    return re.search(forbidden, without_strings) is None


def _structural_sql(value: str) -> str:
    text = _ALL_COMMENTS.sub(" ", _strip_code_fence(value))
    return " ".join(text.lower().split()).strip().rstrip(";").strip()


def _extract_hints(value: str) -> Tuple[List[str], str]:
    text = _strip_code_fence(value)
    hints = [match.group(1).strip() for match in _HINT_COMMENT.finditer(text) if match.group(1).strip()]
    return hints, _HINT_COMMENT.sub(" ", text).strip()


def _merge_settings(target: Dict[str, str], source: Mapping[str, Any]) -> None:
    for raw_name, raw_value in source.items():
        name = str(raw_name).lower()
        if not _IDENTIFIER.fullmatch(name) or name not in SESSION_SETTING_NAMES:
            raise ValueError("unsupported DREAM session setting: {}".format(name))
        value = _setting_value(str(raw_value))
        previous = target.get(name)
        if previous is not None and previous != value:
            raise ValueError("conflicting DREAM session setting: {}".format(name))
        target[name] = value


def parse_dream_candidate(fix_action: str, rewrite_sql: str, original_sql: str) -> Dict[str, Any]:
    """Classify one DREAM result without executing or publishing it.

    DDL and arbitrary action text are retained as unsupported details instead
    of being executed.  A read-only rewrite is eligible for the bridge-managed
    execution path.  Hint-only actions and planner-only SET actions are
    eligible for direct PostgreSQL application through pg_hint_plan; executor
    settings stay on the bridge-managed path.
    """

    fix_hints, fix_without_hints = _extract_hints(fix_action)
    rewrite_hints, rewrite_without_hints = _extract_hints(rewrite_sql)
    settings: Dict[str, str] = {}
    fix_settings, fix_remaining = split_leading_settings(fix_without_hints)
    rewrite_settings, rewrite_remaining = split_leading_settings(rewrite_without_hints)
    _merge_settings(settings, fix_settings)
    _merge_settings(settings, rewrite_settings)

    unsupported: List[str] = []
    semantic_rewrite = ""
    if rewrite_remaining:
        if is_read_only_statement(rewrite_remaining):
            semantic_rewrite = rewrite_remaining
        else:
            unsupported.append(rewrite_remaining)
    if fix_remaining:
        if not semantic_rewrite and is_read_only_statement(fix_remaining):
            semantic_rewrite = fix_remaining
        else:
            unsupported.append(fix_remaining)

    original_shape = _structural_sql(original_sql)
    if semantic_rewrite and _structural_sql(semantic_rewrite) == original_shape:
        semantic_rewrite = ""

    hints: List[str] = []
    for hint in fix_hints + rewrite_hints:
        if hint not in hints:
            hints.append(hint)

    settings_are_direct = bool(settings) and set(settings).issubset(PG_HINT_PLAN_SETTING_NAMES)
    if semantic_rewrite:
        apply_kind = "rewrite_with_settings" if settings else "rewrite_sql"
        if hints:
            apply_kind = "rewrite_with_hint"
        apply_scope = "bridge_managed"
    elif hints and settings and settings_are_direct:
        apply_kind = "plan_hint_with_settings"
        apply_scope = "postgresql_direct"
    elif hints:
        apply_kind = "plan_hint" if not settings else "plan_hint_with_session_settings"
        apply_scope = "postgresql_direct" if not settings else "bridge_managed"
    elif settings and settings_are_direct:
        apply_kind = "session_settings"
        apply_scope = "postgresql_direct"
    elif settings:
        apply_kind = "session_settings"
        apply_scope = "bridge_managed"
    else:
        apply_kind = "unsupported" if unsupported else "no_action"
        apply_scope = "none"

    execution_sql = semantic_rewrite
    if apply_scope == "bridge_managed" and not execution_sql:
        execution_sql = _strip_code_fence(original_sql).strip()
    if execution_sql and hints and not _HINT_COMMENT.search(execution_sql):
        execution_sql = "/*+ {} */ {}".format(" ".join(hints), execution_sql)

    return {
        "apply_kind": apply_kind,
        "apply_scope": apply_scope,
        "hints": hints,
        "session_settings": settings,
        "direct_session_settings": settings if settings_are_direct else {},
        "rewrite_sql": semantic_rewrite,
        "execution_sql": execution_sql,
        "unsupported_actions": unsupported,
        "has_action": bool(hints or settings or semantic_rewrite or unsupported),
    }


def settings_hint_phrase(settings: Mapping[str, Any]) -> str:
    """Encode validated session settings as pg_hint_plan Set directives."""

    normalized: Dict[str, str] = {}
    _merge_settings(normalized, settings)
    return " ".join("Set({} {})".format(name, normalized[name]) for name in sorted(normalized))


def direct_hint_phrase(candidate: Mapping[str, Any], normalized_settings: Mapping[str, Any]) -> str:
    phrases = [str(value).strip() for value in candidate.get("hints", []) if str(value).strip()]
    setting_phrase = settings_hint_phrase(normalized_settings)
    if setting_phrase:
        phrases.append(setting_phrase)
    return " ".join(phrases).strip()
