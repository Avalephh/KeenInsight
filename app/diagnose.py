from __future__ import annotations

import json
import os
import re
from typing import Any

from .db import Database
from .knowledge import list_knowledge
from .llm import LLMClient, LLMError


class UnsafeQuery(ValueError):
    pass


def validate_read_only_query(query: str) -> str:
    """Conservatively validate SQL before embedding it after EXPLAIN.

    PostgreSQL's EXPLAIN without ANALYZE does not execute the statement, but a
    diagnosis service must still avoid accepting arbitrary DDL/DML or multiple
    statements.  This validator intentionally prefers rejection over guessing.
    """
    if not isinstance(query, str):
        raise UnsafeQuery("query must be a string")
    normalized = _strip_sql_comments(query).strip()
    if not normalized or len(normalized) > 100_000:
        raise UnsafeQuery("query is empty or too large")
    if ";" in normalized:
        raise UnsafeQuery("multiple SQL statements are not accepted")
    first = re.match(r"([A-Za-z]+)", normalized)
    if not first or first.group(1).lower() not in {"select", "with", "values"}:
        raise UnsafeQuery("only SELECT, read-only WITH, and VALUES are accepted")
    # A data-modifying CTE or SELECT INTO can mutate state even when the first
    # keyword is SELECT/WITH.  The conservative token check blocks those forms.
    forbidden = re.compile(
        r"\b(insert|update|delete|merge|truncate|alter|drop|create|copy|grant|revoke|call|do|vacuum|refresh|into|nextval|set_config|pg_advisory_lock|pg_advisory_xact_lock)\b"
        r"|\bfor\s+(update|no\s+key\s+update|share|key\s+share)\b",
        re.IGNORECASE,
    )
    if forbidden.search(normalized):
        raise UnsafeQuery("data-modifying or administrative SQL is not accepted")
    return normalized


def explain_query(db: Database, query: str, *, analyze: bool = False) -> dict[str, Any]:
    normalized = validate_read_only_query(query)
    options = "FORMAT JSON, VERBOSE TRUE, COSTS TRUE, SETTINGS TRUE"
    if analyze:
        options = "ANALYZE TRUE, BUFFERS TRUE, FORMAT JSON, VERBOSE TRUE, COSTS TRUE, SETTINGS TRUE"
    statement = f"EXPLAIN ({options}) {normalized}"
    # Keep both plan-only and ANALYZE requests in a read-only transaction.  A
    # SELECT can call a user-defined function, and read-only mode prevents an
    # accidental write even when a function is incorrectly declared stable.
    with db.connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            # A user-triggered plan request must not hold a database session
            # forever because of pg_sleep, a pathological plan, or a lock
            # taken during planning.  ANALYZE remains explicit, but bounded.
            conn.execute("SET LOCAL statement_timeout = '10000ms'")
            conn.execute("SET LOCAL lock_timeout = '2000ms'")
            row = conn.execute(statement).fetchone()
    if not row:
        raise RuntimeError("PostgreSQL returned no execution plan")
    raw = next(iter(row.values()))
    if isinstance(raw, str):
        raw = json.loads(raw)
    # psycopg usually returns the JSON result as a Python list.  Keep the
    # parser tolerant for drivers/configurations that return text.
    if isinstance(raw, list):
        plan_document: Any = raw[0] if raw else {}
    else:
        plan_document = raw
    if not isinstance(plan_document, dict):
        raise RuntimeError("unexpected EXPLAIN JSON result")
    root = plan_document.get("Plan") or {}
    nodes = flatten_plan(root)
    return {
        "query": normalized,
        "analyze": analyze,
        "document": plan_document,
        "root": root,
        "nodes": nodes,
        "summary": summarize_plan(plan_document, nodes),
    }


def grounded_explanation(
    llm_client: LLMClient | None,
    *,
    title: str,
    summary: str,
    root_causes: list[str],
    evidence: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    model_hypothesis: Any = None,
) -> dict[str, Any] | None:
    """Generate a display-only explanation from explicitly supplied evidence.

    The model receives a compact evidence projection and must cite the local
    ``[D#]`` markers.  Its text is never interpreted as an action or SQL; the
    deterministic rule/model result remains authoritative for the control
    plane.
    """

    if not llm_client or not llm_client.configured:
        return None
    evidence_rows: list[dict[str, Any]] = []
    for index, item in enumerate(evidence[:12], 1):
        observed = item.get("observed") if isinstance(item, dict) else {}
        interpretation = item.get("interpretation") if isinstance(item, dict) else ""
        evidence_rows.append(
            {
                "id": "[D%d]" % index,
                "source": str(item.get("source") or "observed evidence")[:240],
                "observed": observed,
                "interpretation": str(interpretation or "")[:800],
            }
        )
    if not evidence_rows:
        return None
    payload = {
        "title": str(title)[:240],
        "summary": str(summary)[:1200],
        "root_causes": [str(item)[:120] for item in root_causes[:12]],
        "evidence": evidence_rows,
        "typed_actions": [
            {
                "action_type": str(item.get("action_type") or "")[:120],
                "target": item.get("target") or {},
                "description": str(item.get("description") or "")[:500],
            }
            for item in actions[:8]
            if isinstance(item, dict)
        ],
        "model_hypothesis": model_hypothesis,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是 PostgreSQL 运维诊断解释器。只能使用用户消息中的观测证据，"
                "用简洁中文解释现象、最有支持的根因和下一步验证。每个关键判断都要引用"
                "对应的 [D#] 证据标记；证据不足时必须明确说证据不足。不要编造数值、"
                "不要输出可执行 SQL，不要把模型假设写成已证实事实，也不要改变类型化操作。"
            ),
        },
        {
            "role": "user",
            "content": "请解释以下已观测诊断，不要超出证据范围：\n" + json.dumps(
                payload, ensure_ascii=False, default=str
            ),
        },
    ]
    try:
        result = llm_client.complete(messages, temperature=0, max_tokens=700)
    except LLMError:
        return None
    text = str(result.get("content") or "").strip()
    cited = sorted(set(re.findall(r"\[D\d+\]", text)))
    valid_ids = {row["id"] for row in evidence_rows}
    cited = [item for item in cited if item in valid_ids]
    if not text or not cited:
        return None
    return {
        "generator": "external-llm-grounded",
        "model": result.get("model") or llm_client.model,
        "text": text,
        "evidence_ids": cited,
        "usage": result.get("usage") or {},
        "latency_ms": result.get("latency_ms"),
        "display_only": True,
    }


def diagnose_incident(
    db: Database,
    incident_id: int,
    *,
    analyze: bool = False,
    llm_client: LLMClient | None = None,
) -> dict[str, Any]:
    incident = db.fetch_one(
        """
        SELECT id, signature, kind, severity, status, summary, evidence,
               diagnosis, metadata, first_seen, last_seen, resolved_at
        FROM keeninsight.incidents WHERE id = %s
        """,
        (incident_id,),
    )
    if not incident:
        raise KeyError(f"incident {incident_id} was not found")

    evidence = incident.get("evidence") or {}
    diagnosis: dict[str, Any] = {
        "method": "deterministic PostgreSQL evidence rules",
        "incident_id": incident_id,
        "root_causes": [],
        "evidence": [],
        "knowledge": [],
        "actions": [],
    }
    kind = incident["kind"]

    if kind == "lock_wait":
        diagnosis["root_causes"].append("lock_wait")
        for row in evidence.get("locks", [])[:20]:
            diagnosis["evidence"].append(
                {
                    "source": "pg_locks + pg_stat_activity",
                    "observed": {
                        "waiting_pid": row.get("waiting_pid"),
                        "blocking_pid": row.get("blocking_pid"),
                        "relation": row.get("relation"),
                        "wait_event": row.get("wait_event"),
                    },
                    "interpretation": "A PostgreSQL backend is waiting for a lock held by another backend.",
                }
            )
    elif kind == "connection_saturation":
        diagnosis["root_causes"].append("connection_saturation")
        diagnosis["evidence"].append(
            {
                "source": "pg_stat_database + pg_settings",
                "observed": {
                    "numbackends": evidence.get("numbackends"),
                    "max_connections": evidence.get("max_connections"),
                    "ratio": evidence.get("ratio"),
                },
                "interpretation": "Current sessions are close to the configured connection limit.",
            }
        )
    elif kind in {"slow_query", "temp_spill"}:
        query = evidence.get("query")
        query_id = evidence.get("query_id")
        if query:
            try:
                plan = explain_query(db, query, analyze=analyze)
                diagnosis["plan"] = plan
                plan_causes, plan_evidence, actions = diagnose_plan(db, plan)
                diagnosis["root_causes"].extend(plan_causes)
                diagnosis["evidence"].extend(plan_evidence)
                diagnosis["actions"].extend(actions)
            except UnsafeQuery as exc:
                diagnosis["evidence"].append(
                    {
                        "source": "pg_stat_statements",
                        "observed": {"query_id": query_id, "query": query},
                        "interpretation": f"The statement was not explained automatically: {exc}.",
                    }
                )
            except Exception as exc:
                diagnosis["evidence"].append(
                    {
                        "source": "EXPLAIN",
                        "observed": {"query_id": query_id},
                        "interpretation": f"PostgreSQL could not produce a plan: {exc}",
                    }
                )
        if kind == "slow_query":
            diagnosis["root_causes"].append("slow_statement")
            diagnosis["evidence"].append(
                {
                    "source": "pg_stat_statements",
                    "observed": {
                        "query_id": query_id,
                        "calls": evidence.get("calls"),
                        "mean_exec_time_ms": evidence.get("mean_exec_time_ms"),
                    },
                    "interpretation": "The statement exceeded the configured mean execution-time rule.",
                }
            )
        if kind == "temp_spill":
            diagnosis["root_causes"].append("temporary_file_spill")
            diagnosis["evidence"].append(
                {
                    "source": "pg_stat_statements",
                    "observed": {
                        "query_id": query_id,
                        "temp_blks_written": evidence.get("temp_blks_written"),
                        "temp_blks_read": evidence.get("temp_blks_read"),
                    },
                    "interpretation": "The statement has accumulated temporary-file block writes.",
                }
            )
    elif kind == "cache_hit_ratio":
        diagnosis["root_causes"].append("low_cache_hit_ratio")
        diagnosis["evidence"].append(
            {
                "source": "pg_stat_database",
                "observed": {
                    "blks_hit": evidence.get("blks_hit"),
                    "blks_read": evidence.get("blks_read"),
                    "ratio": evidence.get("ratio"),
                },
                "interpretation": "The database read more blocks from storage than the configured rule allows.",
            }
        )

    root_causes = list(dict.fromkeys(diagnosis["root_causes"]))
    diagnosis["root_causes"] = root_causes
    diagnosis["knowledge"] = [
        row for cause in root_causes for row in list_knowledge(db, root_cause=cause)
    ]
    llm_analysis = grounded_explanation(
        llm_client,
        title="异常 #%s · %s" % (incident_id, incident.get("kind") or "incident"),
        summary=str(incident.get("summary") or ""),
        root_causes=root_causes,
        evidence=diagnosis["evidence"],
        actions=diagnosis["actions"],
    )
    if llm_analysis:
        diagnosis["llm_analysis"] = llm_analysis
    db.execute(
        "UPDATE keeninsight.incidents SET diagnosis = %s::jsonb WHERE id = %s",
        (json.dumps(diagnosis, default=str), incident_id),
    )
    return {**incident, "diagnosis": diagnosis}


def diagnose_plan(
    db: Database, plan: dict[str, Any]
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    causes: list[str] = []
    evidence: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    nodes = plan.get("nodes") or []

    def append_action(action: dict[str, Any] | None) -> None:
        if not action:
            return
        target = action.get("target") or {}
        action_key = (
            action.get("action_type"),
            target.get("parameter"),
            target.get("index"),
            target.get("scope"),
        )
        if any(
            (
                existing.get("action_type"),
                (existing.get("target") or {}).get("parameter"),
                (existing.get("target") or {}).get("index"),
                (existing.get("target") or {}).get("scope"),
            ) == action_key
            for existing in actions
        ):
            return
        actions.append(action)

    for node in nodes:
        node_type = str(node.get("Node Type", ""))
        batches = hash_batches(node)
        if node_type == "Hash" and batches > 1:
            causes.append("hash_join_spill")
            evidence.append(
                {
                    "source": "EXPLAIN JSON",
                    "observed": {
                        "node_type": node_type,
                        "batches": batches,
                        "hash_batches_field": (
                            "Hash Batches" if "Hash Batches" in node else "Batches"
                        ),
                        "temp_read_blocks": node.get("Temp Read Blocks"),
                        "temp_written_blocks": node.get("Temp Written Blocks"),
                        "peak_memory_usage_kb": node.get("Peak Memory Usage"),
                    },
                    "interpretation": "The hash node used multiple batches; a multi-batch hash can indicate work_mem pressure.",
                }
            )
            action = work_mem_action(db, node)
            append_action(action)
        aggregate_like = node_type == "HashAggregate" or (
            node_type == "Aggregate"
            and node.get("Strategy") in {"Sorted", "Hashed", "Mixed"}
        )
        sort_like = node_type in {"Sort", "WindowAgg"}
        if (sort_like or aggregate_like) and (
            _node_has_temp_blocks(node) or node.get("Sort Space Type") == "Disk"
        ):
            cause = "sort_spill" if sort_like else "aggregate_spill"
            causes.append(cause)
            evidence.append(
                {
                    "source": "EXPLAIN JSON",
                    "observed": {
                        "node_type": node_type,
                        "strategy": node.get("Strategy"),
                        "sort_method": node.get("Sort Method"),
                        "sort_space_type": node.get("Sort Space Type"),
                        "sort_space_used_kb": node.get("Sort Space Used"),
                        "temp_read_blocks": node.get("Temp Read Blocks"),
                        "temp_written_blocks": node.get("Temp Written Blocks"),
                    },
                    "interpretation": (
                        "The sort/window node used temporary disk workspace; low work_mem can force an external sort."
                        if sort_like
                        else "The aggregate node used temporary workspace; low work_mem can force a sorted or spilling aggregate."
                    ),
                }
            )
            if node_type == "Sort" or aggregate_like:
                append_action(work_mem_action(db, node))
        if node_type == "Seq Scan" and node.get("Filter"):
            causes.append("filtered_sequential_scan")
            evidence.append(
                {
                    "source": "EXPLAIN JSON",
                    "observed": {
                        "relation": node.get("Relation Name"),
                        "filter": node.get("Filter"),
                        "plan_rows": node.get("Plan Rows"),
                        "actual_rows": node.get("Actual Rows"),
                    },
                    "interpretation": "A filtered sequential scan was selected; an index may help, but the predicate and workload must be validated before creating one.",
                }
            )
    return list(dict.fromkeys(causes)), evidence, actions


def work_mem_action(db: Database, spill_node: dict[str, Any]) -> dict[str, Any] | None:
    setting = db.fetch_one(
        "SELECT name, setting, unit, context, min_val, max_val FROM pg_settings WHERE name = 'work_mem'"
    )
    if not setting:
        return None
    current_bytes = _setting_bytes(db, setting["setting"], setting.get("unit"))
    peak_kb = _number(spill_node.get("Peak Memory Usage"))
    sort_space_kb = _number(spill_node.get("Sort Space Used"))
    # A recommendation is derived from the observed plan when available.  If
    # the plan was not ANALYZE'd, use a conservative doubling as a proposal and
    # label it as such; the user still has to apply it.  Keep the calculation
    # in the proposal so a reviewer can distinguish an evidence-backed rule
    # from a scenario-specific constant.
    calculation = "double_current_database_work_mem"
    if peak_kb:
        target_bytes = max(current_bytes * 2, int(peak_kb * 1024 * 1.5))
        calculation = "max(2*current_database_work_mem, 1.5*observed_peak_memory)"
    elif sort_space_kb and spill_node.get("Sort Space Type") == "Disk":
        # Disk space is smaller than the in-memory tuple representation.  A
        # bounded five-times margin gives the sort room to change to an
        # in-memory method while the 256MB guardrail prevents an unbounded
        # recommendation.
        target_bytes = max(current_bytes * 2, int(sort_space_kb * 1024 * 5))
        calculation = "max(2*current_database_work_mem, 5*observed_disk_sort_space)"
    else:
        target_bytes = current_bytes * 2
    raw_target_bytes = target_bytes
    target_bytes = max(target_bytes, 64 * 1024)
    target_bytes = min(target_bytes, 256 * 1024 * 1024)
    target_value = f"{max(1, (target_bytes + 1024 * 1024 - 1) // (1024 * 1024))}MB"
    return {
        "action_type": "set_database_parameter",
        "target": {
            "parameter": "work_mem",
            "value": target_value,
            "scope": "database",
        },
        "description": (
            f"Set database-level work_mem to {target_value} for new sessions; validate the spilling query again before keeping it."
        ),
        "validation_required": True,
        "source": "observed EXPLAIN spill/memory fields and pg_settings.work_mem",
        "proposal_evidence": {
            "node_type": spill_node.get("Node Type"),
            "hash_batches": spill_node.get("Hash Batches", spill_node.get("Batches")),
            "temp_read_blocks": spill_node.get("Temp Read Blocks"),
            "temp_written_blocks": spill_node.get("Temp Written Blocks"),
            "peak_memory_usage_kb": peak_kb or None,
            "sort_space_type": spill_node.get("Sort Space Type"),
            "sort_space_used_kb": sort_space_kb or None,
            "current_database_work_mem": {
                "setting": setting.get("setting"),
                "unit": setting.get("unit"),
                "bytes": current_bytes,
            },
            "calculation": calculation,
            "raw_target_bytes_before_guardrail": raw_target_bytes,
            "target_bytes_after_guardrail": target_bytes,
            "guardrail": {
                "minimum_bytes": 64 * 1024,
                "maximum_bytes": 256 * 1024 * 1024,
            },
        },
    }


def parallel_workers_action(
    db: Database, *, plan: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Propose a conservative worker increase from live server capacity.

    The workload runner uses a session-only zero-worker pressure setting.  The
    proposal changes the database default only when PostgreSQL and the host
    report room for one more worker.  It never assumes that a requested worker
    will actually start; the after-phase plan must verify that fact.
    """
    rows = db.fetch_all(
        """
        SELECT name, setting
        FROM pg_settings
        WHERE name IN (
            'max_parallel_workers_per_gather',
            'max_parallel_workers',
            'max_worker_processes'
        )
        """
    )
    values = {row["name"]: _integer_setting(row.get("setting")) for row in rows}
    current = values.get("max_parallel_workers_per_gather")
    global_limit = values.get("max_parallel_workers")
    process_limit = values.get("max_worker_processes")
    if current is None or global_limit is None or process_limit is None:
        return None
    cpu_count = os.cpu_count() or 1
    host_limit = max(1, cpu_count - 1)
    capacity = min(global_limit, process_limit, host_limit)
    target = min(capacity, current + 1)
    if target <= current:
        return None
    plan_nodes = plan.get("nodes") if isinstance(plan, dict) else None
    observed_plan = {
        "root_node": (plan.get("root") or {}).get("Node Type") if isinstance(plan, dict) else None,
        "workers_planned": max(
            [_number(node.get("Workers Planned")) for node in (plan_nodes or [])]
            or [0.0]
        ),
        "workers_launched": max(
            [_number(node.get("Workers Launched")) for node in (plan_nodes or [])]
            or [0.0]
        ),
    }
    return {
        "action_type": "set_database_parameter",
        "target": {
            "parameter": "max_parallel_workers_per_gather",
            "value": str(target),
            "scope": "database",
        },
        "description": (
            "Increase database-level max_parallel_workers_per_gather by one "
            f"to {target}; verify actual workers and latency on the same query."
        ),
        "validation_required": True,
        "source": (
            "observed non-parallel EXPLAIN plan plus pg_settings worker limits "
            f"and host cpu_count={cpu_count}"
        ),
        "proposal_evidence": {
            "observed_plan": observed_plan,
            "settings": {
                "max_parallel_workers_per_gather": current,
                "max_parallel_workers": global_limit,
                "max_worker_processes": process_limit,
            },
            "host_cpu_count": cpu_count,
            "host_worker_limit": host_limit,
            "capacity": capacity,
            "calculation": "min(max_parallel_workers, max_worker_processes, cpu_count-1)",
            "target": target,
        },
    }


def summarize_plan(document: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    plan = document.get("Plan") or {}
    return {
        "planning_time_ms": document.get("Planning Time"),
        "execution_time_ms": document.get("Execution Time"),
        "root_node": plan.get("Node Type"),
        "node_count": len(nodes),
        "relations": sorted({n.get("Relation Name") for n in nodes if n.get("Relation Name")}),
        "scan_nodes": [n.get("Node Type") for n in nodes if "Scan" in str(n.get("Node Type", ""))],
        "join_nodes": [n.get("Node Type") for n in nodes if "Join" in str(n.get("Node Type", ""))],
        "sort_methods": list(
            dict.fromkeys(
                n.get("Sort Method") for n in nodes if n.get("Sort Method")
            )
        ),
        "sort_space_types": list(
            dict.fromkeys(
                n.get("Sort Space Type") for n in nodes if n.get("Sort Space Type")
            )
        ),
        "aggregate_strategies": list(
            dict.fromkeys(
                n.get("Strategy")
                for n in nodes
                if n.get("Node Type") in {"Aggregate", "HashAggregate"}
                and n.get("Strategy")
            )
        ),
        "has_temp_blocks": any(
            _number(n.get("Temp Read Blocks")) > 0 or _number(n.get("Temp Written Blocks")) > 0
            for n in nodes
        ),
    }


def flatten_plan(node: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    result = [node]
    for child in node.get("Plans") or []:
        result.extend(flatten_plan(child))
    return result


def hash_batches(node: dict[str, Any]) -> float:
    """Read the version-specific JSON name used for a Hash node's batches."""
    return _number(node.get("Hash Batches", node.get("Batches")))


def _setting_bytes(db: Database, value: str, unit: str | None = None) -> int:
    normalized = str(value)
    # pg_settings stores integer memory settings and their unit separately
    # (e.g. setting=4096, unit=kB).  pg_size_bytes needs the unit attached.
    if unit and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", normalized):
        normalized += str(unit)
    row = db.fetch_one("SELECT pg_size_bytes(%s) AS bytes", (normalized,))
    return int(row["bytes"]) if row else 0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _node_has_temp_blocks(node: dict[str, Any]) -> bool:
    return any(
        _number(node.get(field)) > 0
        for field in ("Temp Read Blocks", "Temp Written Blocks")
    )


def _integer_setting(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _strip_sql_comments(query: str) -> str:
    query = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    return re.sub(r"--[^\r\n]*", " ", query)
