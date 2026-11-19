"""Evidence-grounded, typed PostgreSQL tuning actions.

Tuning actions are typed, validated against PostgreSQL metadata, and persisted
before application. The validation record explains the observed proposal and
whether operator approval is required; ``requires_user_apply`` is never
treated as permission to execute arbitrary SQL. This semantic contract is also
indexed by the grounded RAG layer so implementation questions resolve to this
module instead of only to a dashboard description.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .audit import record_audit
from .db import Database
from .pg import index_exists, jsonable, relation_columns, relation_exists, relation_kind, safe_identifier


ALLOWED_DATABASE_PARAMETERS = {"work_mem", "max_parallel_workers_per_gather"}
ALLOWED_ACTION_TYPES = {"set_database_parameter", "create_index", "drop_index", "refresh_statistics"}
PROTECTED_SCHEMAS = {"pg_catalog", "information_schema", "keeninsight"}


class ActionValidationError(ValueError):
    pass


def create_action(
    db: Database, proposal: dict[str, Any], *, incident_id: int | None = None
) -> dict[str, Any]:
    """Validate a typed proposal and persist it as a user-reviewable action."""
    action_type = proposal.get("action_type")
    target = proposal.get("target") or {}
    if action_type not in ALLOWED_ACTION_TYPES:
        raise ActionValidationError(f"unsupported action type: {action_type!r}")
    if action_type == "set_database_parameter":
        target, before, rollback = _validate_setting_target(db, target)
    elif action_type == "create_index":
        target, before, rollback = _validate_index_target(db, target)
    elif action_type == "drop_index":
        target, before, rollback = _validate_drop_index_target(db, target)
    else:
        target, before, rollback = _validate_statistics_target(db, target)

    description = str(proposal.get("description") or _description(action_type, target))
    # A workload-origin proposal is tied to the exact measured baseline that
    # produced it.  Without this provenance predicate, a stale ``proposed``
    # row from an interrupted run could be reused for a later baseline with
    # the same target, and feedback would correctly reject the comparison as
    # coming from the wrong run.  Incident/manual proposals keep the original
    # structural deduplication behavior.
    proposal_source = proposal.get("source") or {}
    source_run_id = proposal_source.get("run_id")
    if source_run_id is not None:
        existing = db.fetch_one(
            """
            SELECT id, incident_id, action_type, target, description, status,
                   before_state, after_state, rollback_state, validation,
                   error, proposed_at, applied_at, rolled_back_at
            FROM keeninsight.tuning_actions
            WHERE incident_id IS NOT DISTINCT FROM %s
              AND action_type = %s
              AND target = %s::jsonb
              AND validation->'proposal_source'->>'run_id' = %s
              AND status NOT IN ('rolled_back', 'failed', 'rejected', 'completed')
            ORDER BY proposed_at DESC LIMIT 1
            """,
            (incident_id, action_type, json.dumps(target), str(source_run_id)),
        )
    else:
        existing = db.fetch_one(
            """
            SELECT id, incident_id, action_type, target, description, status,
                   before_state, after_state, rollback_state, validation,
                   error, proposed_at, applied_at, rolled_back_at
            FROM keeninsight.tuning_actions
            WHERE incident_id IS NOT DISTINCT FROM %s
              AND action_type = %s
              AND target = %s::jsonb
              AND status NOT IN ('rolled_back', 'failed', 'rejected', 'completed')
            ORDER BY proposed_at DESC LIMIT 1
            """,
            (incident_id, action_type, json.dumps(target)),
        )
    if existing:
        return existing
    validation = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [
            "typed action allowlist",
            "target exists",
            "target value or columns accepted by PostgreSQL metadata",
        ],
        "requires_user_apply": True,
    }
    if proposal.get("source") is not None:
        validation["proposal_source"] = proposal["source"]
    if proposal.get("proposal_evidence") is not None:
        # Preserve the compact derivation supplied by the diagnosis layer.
        # This is intentionally separate from the executable target: the
        # target is validated again below, while this record explains why it
        # was proposed from the observed plan/settings.
        validation["proposal_evidence"] = proposal["proposal_evidence"]
    row = db.fetch_one(
        """
        INSERT INTO keeninsight.tuning_actions
            (incident_id, action_type, target, description, before_state,
             rollback_state, validation)
        VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s::jsonb)
        RETURNING id, incident_id, action_type, target, description, status,
                  before_state, after_state, rollback_state, validation,
                  error, proposed_at, applied_at, rolled_back_at
        """,
        (
            incident_id,
            action_type,
            json.dumps(target),
            description,
            json.dumps(before),
            json.dumps(rollback),
            json.dumps(jsonable(validation)),
        ),
    )
    if not row:
        raise RuntimeError("failed to persist tuning action")
    record_audit(db, "action_proposed", "tuning_action", row["id"], {"target": target})
    return row


def get_action(db: Database, action_id: int) -> dict[str, Any] | None:
    return db.fetch_one(
        """
        SELECT id, incident_id, action_type, target, description, status,
               before_state, after_state, rollback_state, validation,
               error, proposed_at, applied_at, rolled_back_at
        FROM keeninsight.tuning_actions WHERE id = %s
        """,
        (action_id,),
    )


def apply_action(db: Database, action_id: int) -> dict[str, Any]:
    action = get_action(db, action_id)
    if not action:
        raise KeyError(f"action {action_id} was not found")
    # Both persistent changes and idempotent maintenance operations are safe
    # to re-read after a duplicate browser request.  ANALYZE is represented as
    # ``completed`` because it has no meaningful inverse, but a repeated apply
    # must still be a no-op rather than turning a successful action into an
    # error.
    if action["status"] in {"applied", "completed"}:
        return action
    if action["status"] != "proposed":
        raise ActionValidationError(
            f"action {action_id} has status {action['status']!r} and cannot be applied"
        )

    # Claim the row atomically.  DDL is deliberately executed outside the
    # transaction that changes this state, so a second browser request cannot
    # apply the same action concurrently.
    claimed = db.fetch_one(
        """
        UPDATE keeninsight.tuning_actions
        SET status = 'applying', error = NULL
        WHERE id = %s AND status = 'proposed'
        RETURNING id, incident_id, action_type, target, description, status,
                  before_state, after_state, rollback_state, validation,
                  error, proposed_at, applied_at, rolled_back_at
        """,
        (action_id,),
    )
    if not claimed:
        current = get_action(db, action_id)
        if current and current["status"] in {"applied", "completed"}:
            return current
        if current:
            raise ActionValidationError(
                f"action {action_id} has status {current['status']!r} and is already being processed"
            )
        raise KeyError(f"action {action_id} was not found")
    action = claimed
    try:
        if action["action_type"] == "set_database_parameter":
            after = _apply_database_parameter(db, action["target"])
        elif action["action_type"] == "create_index":
            after = _apply_index(db, action["target"])
        elif action["action_type"] == "drop_index":
            after = _apply_drop_index(db, action["target"], action["before_state"])
        elif action["action_type"] == "refresh_statistics":
            after = _apply_statistics(db, action["target"])
        else:  # defensive: create_action already rejects this
            raise ActionValidationError(f"unsupported action type: {action['action_type']}")
        validation = dict(action.get("validation") or {})
        validation["apply_verified_at"] = datetime.now(timezone.utc).isoformat()
        validation["apply_verification"] = after.get("verification", {})
        final_status = (
            "completed" if action["action_type"] == "refresh_statistics" else "applied"
        )
        db.execute(
            """
            UPDATE keeninsight.tuning_actions
            SET status = %s, after_state = %s::jsonb,
                validation = %s::jsonb, applied_at = now(), error = NULL
            WHERE id = %s
            """,
            (final_status, json.dumps(after), json.dumps(validation), action_id),
        )
        record_audit(
            db,
            "action_completed" if final_status == "completed" else "action_applied",
            "tuning_action",
            action_id,
            after,
        )
    except Exception as exc:
        db.execute(
            "UPDATE keeninsight.tuning_actions SET status = 'failed', error = %s WHERE id = %s",
            (str(exc), action_id),
        )
        record_audit(db, "action_failed", "tuning_action", action_id, {"error": str(exc)})
        raise
    return get_action(db, action_id) or action


def rollback_action(db: Database, action_id: int) -> dict[str, Any]:
    action = get_action(db, action_id)
    if not action:
        raise KeyError(f"action {action_id} was not found")
    if action["status"] == "rolled_back":
        return action
    if action["status"] == "completed":
        raise ActionValidationError(
            f"action {action_id} is an idempotent maintenance operation and has no rollback; clean the demo fixture if needed"
        )
    if action["status"] != "applied":
        raise ActionValidationError(
            f"action {action_id} has status {action['status']!r} and cannot be rolled back"
        )

    # Reuse the transient `applying` state as a lock while the actual
    # CREATE/DROP or ALTER/RESET operation runs outside a database transaction.
    claimed = db.fetch_one(
        """
        UPDATE keeninsight.tuning_actions
        SET status = 'applying', error = NULL
        WHERE id = %s AND status = 'applied'
        RETURNING id, incident_id, action_type, target, description, status,
                  before_state, after_state, rollback_state, validation,
                  error, proposed_at, applied_at, rolled_back_at
        """,
        (action_id,),
    )
    if not claimed:
        current = get_action(db, action_id)
        if current and current["status"] == "rolled_back":
            return current
        if current:
            raise ActionValidationError(
                f"action {action_id} has status {current['status']!r} and is already being processed"
            )
        raise KeyError(f"action {action_id} was not found")
    action = claimed

    try:
        if action["action_type"] == "set_database_parameter":
            rollback = _rollback_database_parameter(db, action["target"], action["rollback_state"])
        elif action["action_type"] == "create_index":
            rollback = _rollback_index(db, action["target"], action["before_state"], action["after_state"])
        elif action["action_type"] == "drop_index":
            rollback = _rollback_dropped_index(db, action["target"], action["before_state"], action["after_state"])
        else:
            raise ActionValidationError(f"unsupported action type: {action['action_type']}")
        db.execute(
            """
            UPDATE keeninsight.tuning_actions
            SET status = 'rolled_back', rollback_state = %s::jsonb,
                rolled_back_at = now(), error = NULL
            WHERE id = %s
            """,
            (json.dumps(rollback), action_id),
        )
        record_audit(db, "action_rolled_back", "tuning_action", action_id, rollback)
    except Exception as exc:
        db.execute(
            "UPDATE keeninsight.tuning_actions SET status = 'failed', error = %s WHERE id = %s",
            (str(exc), action_id),
        )
        record_audit(db, "rollback_failed", "tuning_action", action_id, {"error": str(exc)})
        raise
    return get_action(db, action_id) or action


def reject_action(db: Database, action_id: int) -> dict[str, Any]:
    action = get_action(db, action_id)
    if not action:
        raise KeyError(f"action {action_id} was not found")
    if action["status"] == "rejected":
        return action
    if action["status"] != "proposed":
        raise ActionValidationError(
            f"action {action_id} has status {action['status']!r} and cannot be rejected"
        )
    row = db.fetch_one(
        """
        UPDATE keeninsight.tuning_actions
        SET status = 'rejected', error = 'rejected by operator'
        WHERE id = %s AND status = 'proposed'
        RETURNING id
        """,
        (action_id,),
    )
    if not row:
        current = get_action(db, action_id)
        if current:
            raise ActionValidationError(
                f"action {action_id} has status {current['status']!r} and is already being processed"
            )
        raise KeyError(f"action {action_id} was not found")
    record_audit(db, "action_rejected", "tuning_action", action_id, {})
    return get_action(db, action_id) or action


def list_actions(db: Database, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if status:
        return db.fetch_all(
            """
            SELECT id, incident_id, action_type, target, description, status,
                   before_state, after_state, rollback_state, validation,
                   error, proposed_at, applied_at, rolled_back_at
            FROM keeninsight.tuning_actions
            WHERE status = %s ORDER BY proposed_at DESC LIMIT %s
            """,
            (status, max(1, min(limit, 500))),
        )
    return db.fetch_all(
        """
        SELECT id, incident_id, action_type, target, description, status,
               before_state, after_state, rollback_state, validation,
               error, proposed_at, applied_at, rolled_back_at
        FROM keeninsight.tuning_actions
        ORDER BY proposed_at DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )


def _validate_setting_target(
    db: Database, target: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    parameter = target.get("parameter")
    value = str(target.get("value", "")).strip()
    if parameter not in ALLOWED_DATABASE_PARAMETERS:
        raise ActionValidationError(
            f"database parameter {parameter!r} is not in the safe allowlist"
        )
    if not value or ";" in value or "\x00" in value:
        raise ActionValidationError("invalid PostgreSQL setting value")
    setting = db.fetch_one(
        """
        SELECT name, setting, unit, context, min_val, max_val, source,
               reset_val, pending_restart
        FROM pg_settings WHERE name = %s
        """,
        (parameter,),
    )
    if not setting:
        raise ActionValidationError(f"PostgreSQL setting {parameter!r} is unavailable")
    if setting["context"] not in {"user", "database", "sighup"}:
        raise ActionValidationError(
            f"setting {parameter!r} has unsupported context {setting['context']!r}"
        )
    if parameter == "work_mem":
        proposed_value = _size_bytes(db, value)
        if proposed_value <= 0:
            raise ActionValidationError(f"could not parse {value!r} as a positive memory value")
        min_value = _optional_size_bytes(db, setting.get("min_val"), setting.get("unit"))
        max_value = _optional_size_bytes(db, setting.get("max_val"), setting.get("unit"))
    else:
        try:
            proposed_value = int(value, 10)
        except (TypeError, ValueError) as exc:
            raise ActionValidationError(f"{value!r} is not a valid integer setting") from exc
        value = str(proposed_value)
        try:
            min_value = int(setting["min_val"]) if setting.get("min_val") is not None else None
            max_value = int(setting["max_val"]) if setting.get("max_val") is not None else None
        except (TypeError, ValueError) as exc:
            raise ActionValidationError(
                f"PostgreSQL returned invalid bounds for setting {parameter!r}"
            ) from exc
    if min_value is not None and proposed_value < min_value:
        raise ActionValidationError(f"{value} is below PostgreSQL's minimum {setting['min_val']}")
    if max_value is not None and proposed_value > max_value:
        raise ActionValidationError(f"{value} is above PostgreSQL's maximum {setting['max_val']}")

    db_name = db.fetch_one("SELECT current_database() AS database_name")["database_name"]
    override = _database_override(db, db_name, parameter)
    normalized = {"parameter": parameter, "value": value, "scope": "database", "database": db_name}
    before = {
        "effective_setting": setting["setting"],
        "source": setting["source"],
        "database_override": override,
    }
    rollback = {
        "database": db_name,
        "parameter": parameter,
        "previous_database_override": override,
    }
    return normalized, before, rollback


def _validate_index_target(
    db: Database, target: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    schema_name = str(target.get("schema", "public"))
    table_name = str(target.get("table", ""))
    index_name = str(target.get("index", ""))
    columns = target.get("columns")
    if not all(safe_identifier(value) for value in (schema_name, table_name, index_name)):
        raise ActionValidationError("schema, table, and index must be simple PostgreSQL identifiers")
    if (
        schema_name in PROTECTED_SCHEMAS
        or schema_name.startswith("pg_")
    ):
        raise ActionValidationError(
            f"index creation is not allowed in protected schema {schema_name!r}"
        )
    if not isinstance(columns, list) or not columns or not all(
        isinstance(value, str) and safe_identifier(value) for value in columns
    ):
        raise ActionValidationError("columns must be a non-empty list of safe identifiers")
    if len(set(columns)) != len(columns):
        raise ActionValidationError("index columns must be unique")
    if not relation_exists(db, schema_name, table_name):
        raise ActionValidationError(f"relation {schema_name}.{table_name} does not exist")
    if relation_kind(db, schema_name, table_name) != "r":
        raise ActionValidationError(
            f"index target {schema_name}.{table_name} is not an ordinary table"
        )
    actual_columns = {row["column_name"] for row in relation_columns(db, schema_name, table_name)}
    missing = sorted(set(columns) - actual_columns)
    if missing:
        raise ActionValidationError(f"index columns do not exist: {missing}")
    exists = index_exists(db, schema_name, index_name)
    normalized = {
        "schema": schema_name,
        "table": table_name,
        "index": index_name,
        "columns": columns,
        "method": "btree",
    }
    before = {"exists": exists}
    rollback = {"drop_created_index_only": not exists}
    if exists:
        raise ActionValidationError(
            f"index {schema_name}.{index_name} already exists; no create action is needed"
        )
    return normalized, before, rollback


def _validate_drop_index_target(
    db: Database, target: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate a reviewed removal of an unused, non-constraint index."""
    schema_name = str(target.get("schema", "public"))
    index_name = str(target.get("index", ""))
    if not all(safe_identifier(value) for value in (schema_name, index_name)):
        raise ActionValidationError("schema and index must be simple PostgreSQL identifiers")
    if schema_name in PROTECTED_SCHEMAS or schema_name.startswith("pg_"):
        raise ActionValidationError(
            f"index removal is not allowed in protected schema {schema_name!r}"
        )
    row = db.fetch_one(
        """
        SELECT c.oid::text AS index_oid, c.relname AS index_name,
               table_class.relname AS table_name,
               COALESCE(i.indisvalid, FALSE) AS indisvalid,
               COALESCE(i.indisprimary, FALSE) AS indisprimary,
               COALESCE(i.indisunique, FALSE) AS indisunique,
               COALESCE(i.indisexclusion, FALSE) AS indisexclusion,
               COALESCE(s.idx_scan, 0)::bigint AS idx_scan,
               pg_get_indexdef(c.oid) AS index_definition
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_index i ON i.indexrelid = c.oid
        LEFT JOIN pg_class table_class ON table_class.oid = i.indrelid
        LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = c.oid
        WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'i'
        """,
        (schema_name, index_name),
    )
    if not row:
        raise ActionValidationError(f"index {schema_name}.{index_name} does not exist")
    if not row.get("indisvalid"):
        raise ActionValidationError("refusing to remove an invalid index")
    if row.get("indisprimary") or row.get("indisunique") or row.get("indisexclusion"):
        raise ActionValidationError("refusing to remove a primary, unique, or exclusion index")
    if int(row.get("idx_scan") or 0) != 0:
        raise ActionValidationError(
            "index has observed scans; only a zero-scan index can enter the removal proposal"
        )
    expected_table = target.get("table")
    if expected_table and str(expected_table) != str(row.get("table_name")):
        raise ActionValidationError("index table does not match PostgreSQL metadata")
    normalized = {
        "schema": schema_name,
        "table": row.get("table_name"),
        "index": index_name,
        "method": "drop_index_concurrently",
    }
    before = {
        "exists": True,
        "index_oid": row.get("index_oid"),
        "index_definition": row.get("index_definition"),
        "idx_scan": int(row.get("idx_scan") or 0),
    }
    rollback = {
        "recreate_index_definition": row.get("index_definition"),
        "index_oid": row.get("index_oid"),
    }
    return normalized, before, rollback


def _validate_statistics_target(
    db: Database, target: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate a narrow, non-index ANALYZE target.

    ANALYZE changes planner metadata rather than application rows.  It is
    restricted to an ordinary user table and is recorded as a completed
    maintenance action because PostgreSQL has no meaningful inverse for a
    refreshed histogram.
    """
    schema_name = str(target.get("schema", "keeninsight_demo"))
    table_name = str(target.get("table", ""))
    if not all(safe_identifier(value) for value in (schema_name, table_name)):
        raise ActionValidationError("schema and table must be simple PostgreSQL identifiers")
    if schema_name in PROTECTED_SCHEMAS or schema_name.startswith("pg_"):
        raise ActionValidationError(
            f"statistics refresh is not allowed in protected schema {schema_name!r}"
        )
    if not relation_exists(db, schema_name, table_name):
        raise ActionValidationError(f"relation {schema_name}.{table_name} does not exist")
    if relation_kind(db, schema_name, table_name) != "r":
        raise ActionValidationError(
            f"statistics target {schema_name}.{table_name} is not an ordinary table"
        )
    normalized = {"schema": schema_name, "table": table_name}
    before = _statistics_state(db, schema_name, table_name)
    rollback = {
        "operation": "not_applicable",
        "reason": "ANALYZE refreshes planner metadata and has no meaningful inverse",
    }
    return normalized, before, rollback


def _apply_database_parameter(db: Database, target: dict[str, Any]) -> dict[str, Any]:
    database = target["database"]
    parameter = target["parameter"]
    value = target["value"]
    _require_identifier(database)
    _require_identifier(parameter)
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER DATABASE {} SET {} TO {}").format(
                sql.Identifier(database), sql.Identifier(parameter), sql.Literal(value)
            )
        )
    verification = _fresh_setting(db.dsn, parameter)
    return {"setting": verification.get("setting"), "verification": verification}


def _rollback_database_parameter(
    db: Database, target: dict[str, Any], rollback_state: dict[str, Any]
) -> dict[str, Any]:
    database = target["database"]
    parameter = target["parameter"]
    previous = rollback_state.get("previous_database_override")
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        if previous:
            conn.execute(
                sql.SQL("ALTER DATABASE {} SET {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(parameter), sql.Literal(previous)
                )
            )
        else:
            conn.execute(
                sql.SQL("ALTER DATABASE {} RESET {}").format(
                    sql.Identifier(database), sql.Identifier(parameter)
                )
            )
    verification = _fresh_setting(db.dsn, parameter)
    return {"restored_database_override": previous, "verification": verification}


def _apply_index(db: Database, target: dict[str, Any]) -> dict[str, Any]:
    schema_name = target["schema"]
    table_name = target["table"]
    index_name = target["index"]
    columns = target["columns"]
    if _fresh_index_identity(db.dsn, schema_name, index_name):
        raise ActionValidationError(
            f"index {schema_name}.{index_name} appeared after the proposal; refusing to adopt it"
        )
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE INDEX CONCURRENTLY {} ON {} ({})").format(
                # PostgreSQL accepts a schema-qualified table here, but the
                # index name itself is resolved in the table's schema and
                # must not be rendered as "schema"."index" in CREATE INDEX.
                sql.Identifier(index_name),
                sql.Identifier(schema_name, table_name),
                sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            )
        )
    identity = _fresh_index_identity(db.dsn, schema_name, index_name)
    if not identity or not identity.get("indisvalid"):
        raise RuntimeError("PostgreSQL did not report the created index")
    return {
        "created": True,
        "index_oid": identity["index_oid"],
        "index_definition": identity["index_definition"],
        "verification": {"index_exists": True, "index_valid": identity["indisvalid"]},
    }


def _apply_drop_index(
    db: Database, target: dict[str, Any], before_state: dict[str, Any]
) -> dict[str, Any]:
    current = _fresh_index_identity(db.dsn, target["schema"], target["index"])
    if not current:
        raise ActionValidationError("index disappeared after the removal proposal")
    if (
        str(current.get("index_oid")) != str(before_state.get("index_oid"))
        or current.get("index_definition") != before_state.get("index_definition")
    ):
        raise ActionValidationError(
            "index identity changed after the removal proposal; refusing to drop it"
        )
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP INDEX CONCURRENTLY {} ").format(
                sql.Identifier(target["schema"], target["index"])
            )
        )
    exists = bool(_fresh_index_identity(db.dsn, target["schema"], target["index"]))
    if exists:
        raise RuntimeError("PostgreSQL still reports the removed index")
    return {
        "dropped": True,
        "index_definition": before_state.get("index_definition"),
        "verification": {"index_exists": False},
    }


def _apply_statistics(db: Database, target: dict[str, Any]) -> dict[str, Any]:
    schema_name = target["schema"]
    table_name = target["table"]
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ANALYZE {}.{}").format(
                sql.Identifier(schema_name), sql.Identifier(table_name)
            )
        )
    state = _statistics_state(db, schema_name, table_name)
    return {
        "refreshed": True,
        "statistics": state,
        "verification": {
            "relation_exists": relation_exists(db, schema_name, table_name),
            "last_analyze": state.get("last_analyze"),
        },
    }


def _rollback_index(
    db: Database,
    target: dict[str, Any],
    before_state: dict[str, Any],
    after_state: dict[str, Any],
) -> dict[str, Any]:
    if before_state.get("exists"):
        raise ActionValidationError("refusing to drop an index that predated this action")
    if not after_state.get("created"):
        raise ActionValidationError("refusing to drop an index not recorded as created")
    current = _fresh_index_identity(db.dsn, target["schema"], target["index"])
    if not current:
        return {
            "dropped_created_index": True,
            "verification": {"index_exists": False},
        }
    if (
        str(current.get("index_oid")) != str(after_state.get("index_oid"))
        or current.get("index_definition") != after_state.get("index_definition")
    ):
        raise ActionValidationError(
            "refusing to drop an index whose PostgreSQL identity changed after the action"
        )
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(
                sql.Identifier(target["schema"], target["index"])
            )
        )
    exists = bool(_fresh_index_identity(db.dsn, target["schema"], target["index"]))
    return {"dropped_created_index": not exists, "verification": {"index_exists": exists}}


def _rollback_dropped_index(
    db: Database,
    target: dict[str, Any],
    before_state: dict[str, Any],
    after_state: dict[str, Any],
) -> dict[str, Any]:
    if not before_state.get("exists") or not before_state.get("index_definition"):
        raise ActionValidationError("no trusted index definition is available for rollback")
    if _fresh_index_identity(db.dsn, target["schema"], target["index"]):
        raise ActionValidationError("refusing to recreate an index name that is already in use")
    definition = str(before_state["index_definition"])
    if not definition.upper().startswith("CREATE INDEX "):
        raise ActionValidationError("catalog returned an unexpected index definition")
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute(sql.SQL(definition))
    identity = _fresh_index_identity(db.dsn, target["schema"], target["index"])
    if not identity or not identity.get("indisvalid"):
        raise RuntimeError("PostgreSQL did not recreate the removed index")
    return {
        "recreated": True,
        "index_oid": identity.get("index_oid"),
        "index_definition": identity.get("index_definition"),
        "verification": {"index_exists": True, "index_valid": identity.get("indisvalid")},
    }


def _database_override(db: Database, database: str, parameter: str) -> str | None:
    row = db.fetch_one(
        """
        SELECT setconfig
        FROM pg_db_role_setting
        WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = %s)
          AND setrole = 0
        """,
        (database,),
    )
    if not row or not row.get("setconfig"):
        return None
    prefix = parameter + "="
    for item in row["setconfig"]:
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def _fresh_setting(dsn: str, parameter: str) -> dict[str, Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT name, setting, source, pending_restart FROM pg_settings WHERE name = %s",
            (parameter,),
        ).fetchone()
        return dict(row or {})


def _statistics_state(db: Database, schema_name: str, table_name: str) -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT last_analyze, last_autoanalyze, n_live_tup
        FROM pg_stat_user_tables
        WHERE schemaname = %s AND relname = %s
        """,
        (schema_name, table_name),
    ) or {}
    result: dict[str, Any] = {}
    for key, value in row.items():
        result[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return result


def _fresh_index_exists(dsn: str, schema_name: str, index_name: str) -> bool:
    return bool(_fresh_index_identity(dsn, schema_name, index_name))


def _fresh_index_identity(
    dsn: str, schema_name: str, index_name: str
) -> dict[str, Any] | None:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT c.oid::text AS index_oid,
                   COALESCE(i.indisvalid, FALSE) AS indisvalid,
                   pg_get_indexdef(c.oid) AS index_definition
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_index i ON i.indexrelid = c.oid
            WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'i'
            """,
            (schema_name, index_name),
        ).fetchone()
        return dict(row) if row else None


def _size_bytes(db: Database, value: str) -> int:
    try:
        row = db.fetch_one("SELECT pg_size_bytes(%s) AS bytes", (value,))
    except Exception:
        # PostgreSQL rejects malformed units with an error.  Treat that as an
        # invalid proposal so the HTTP layer can return a safe 400 response.
        return 0
    try:
        return int(row["bytes"])
    except (TypeError, ValueError, KeyError):
        return 0


def _optional_size_bytes(db: Database, value: str | None, unit: str | None = None) -> int | None:
    if not value:
        return None
    normalized = str(value)
    if unit and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", normalized):
        normalized += str(unit)
    return _size_bytes(db, normalized)


def _description(action_type: str, target: dict[str, Any]) -> str:
    if action_type == "set_database_parameter":
        return f"Set {target['parameter']} to {target['value']} for database {target['database']}"
    if action_type == "refresh_statistics":
        return f"Refresh PostgreSQL statistics for {target['schema']}.{target['table']}"
    if action_type == "drop_index":
        return f"Drop unused index {target['schema']}.{target['index']} concurrently"
    return (
        f"Create btree index {target['schema']}.{target['index']} on "
        f"{target['schema']}.{target['table']} ({', '.join(target['columns'])})"
    )


def _require_identifier(value: str) -> None:
    if not safe_identifier(value):
        raise ActionValidationError(f"unsafe PostgreSQL identifier: {value!r}")
