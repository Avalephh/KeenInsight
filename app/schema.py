from __future__ import annotations

from psycopg.errors import (
    DuplicateObject,
    InsufficientPrivilege,
    ObjectNotInPrerequisiteState,
    UndefinedFile,
)

from .db import Database


SCHEMA_STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS keeninsight",
    """
    CREATE TABLE IF NOT EXISTS keeninsight.alert_rules (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        config JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.snapshots (
        id BIGSERIAL PRIMARY KEY,
        collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        server_version TEXT,
        database_name TEXT NOT NULL,
        payload JSONB NOT NULL,
        collection_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.incidents (
        id BIGSERIAL PRIMARY KEY,
        signature TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'resolved', 'ignored')),
        summary TEXT NOT NULL,
        first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolved_at TIMESTAMPTZ,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        diagnosis JSONB NOT NULL DEFAULT '{}'::jsonb,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.knowledge_entries (
        id BIGSERIAL PRIMARY KEY,
        source_type TEXT NOT NULL,
        title TEXT NOT NULL,
        root_cause TEXT NOT NULL,
        context JSONB NOT NULL DEFAULT '{}'::jsonb,
        action_type TEXT,
        action_template JSONB NOT NULL DEFAULT '{}'::jsonb,
        provenance TEXT NOT NULL,
        verified BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source_type, title)
    )
    """,
    # These ALTER statements make the fresh schema and an already-running
    # local instance converge without requiring a destructive reset.
    "ALTER TABLE keeninsight.knowledge_entries ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0",
    "ALTER TABLE keeninsight.knowledge_entries ADD COLUMN IF NOT EXISTS feedback_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE keeninsight.knowledge_entries ADD COLUMN IF NOT EXISTS success_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE keeninsight.knowledge_entries ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE keeninsight.knowledge_entries ADD COLUMN IF NOT EXISTS last_feedback_at TIMESTAMPTZ",
    "ALTER TABLE keeninsight.knowledge_entries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    """
    CREATE TABLE IF NOT EXISTS keeninsight.tuning_actions (
        id BIGSERIAL PRIMARY KEY,
        incident_id BIGINT REFERENCES keeninsight.incidents(id) ON DELETE SET NULL,
        action_type TEXT NOT NULL,
        target JSONB NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'proposed'
            CHECK (status IN ('proposed', 'applying', 'applied', 'completed', 'rolled_back', 'failed', 'rejected')),
        before_state JSONB NOT NULL DEFAULT '{}'::jsonb,
        after_state JSONB NOT NULL DEFAULT '{}'::jsonb,
        rollback_state JSONB NOT NULL DEFAULT '{}'::jsonb,
        validation JSONB NOT NULL DEFAULT '{}'::jsonb,
        error TEXT,
        proposed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        applied_at TIMESTAMPTZ,
        rolled_back_at TIMESTAMPTZ
    )
    """,
    "ALTER TABLE keeninsight.tuning_actions DROP CONSTRAINT IF EXISTS tuning_actions_status_check",
    """
    ALTER TABLE keeninsight.tuning_actions
    ADD CONSTRAINT tuning_actions_status_check
    CHECK (status IN ('proposed', 'applying', 'applied', 'completed', 'rolled_back', 'failed', 'rejected'))
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.workload_runs (
        id BIGSERIAL PRIMARY KEY,
        scenario TEXT NOT NULL,
        phase TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
        parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
        observations JSONB NOT NULL DEFAULT '{}'::jsonb,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.tuning_feedback (
        id BIGSERIAL PRIMARY KEY,
        action_id BIGINT NOT NULL REFERENCES keeninsight.tuning_actions(id) ON DELETE CASCADE,
        baseline_run_id BIGINT NOT NULL REFERENCES keeninsight.workload_runs(id),
        after_run_id BIGINT NOT NULL REFERENCES keeninsight.workload_runs(id),
        outcome JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (action_id, baseline_run_id, after_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.legacy_tune_tasks (
        task_name TEXT PRIMARY KEY,
        load_type TEXT NOT NULL,
        scenario TEXT NOT NULL,
        load_config JSONB NOT NULL DEFAULT '{}'::jsonb,
        tune_config JSONB NOT NULL DEFAULT '{}'::jsonb,
        configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
        first_evaluate_status TEXT NOT NULL DEFAULT 'idle',
        first_evaluate_run_id BIGINT REFERENCES keeninsight.workload_runs(id),
        first_evaluate_result JSONB NOT NULL DEFAULT '{}'::jsonb,
        one_step_status TEXT NOT NULL DEFAULT 'idle',
        one_step_action_id BIGINT REFERENCES keeninsight.tuning_actions(id),
        one_step_baseline_run_id BIGINT REFERENCES keeninsight.workload_runs(id),
        one_step_after_run_id BIGINT REFERENCES keeninsight.workload_runs(id),
        one_step_action_type TEXT,
        one_step_target JSONB NOT NULL DEFAULT '{}'::jsonb,
        one_step_result JSONB NOT NULL DEFAULT '{}'::jsonb,
        multi_status TEXT NOT NULL DEFAULT 'idle',
        multi_result JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.audit_events (
        id BIGSERIAL PRIMARY KEY,
        event_type TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id BIGINT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.rag_documents (
        id BIGSERIAL PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        title TEXT NOT NULL,
        uri TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.rag_chunks (
        id BIGSERIAL PRIMARY KEY,
        document_id BIGINT NOT NULL REFERENCES keeninsight.rag_documents(id)
            ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        token_count INTEGER NOT NULL DEFAULT 0,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        search_vector TSVECTOR NOT NULL,
        UNIQUE (document_id, chunk_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS incidents_status_last_seen_idx ON keeninsight.incidents (status, last_seen DESC)",
    "CREATE INDEX IF NOT EXISTS snapshots_collected_at_idx ON keeninsight.snapshots (collected_at DESC)",
    "CREATE INDEX IF NOT EXISTS actions_status_idx ON keeninsight.tuning_actions (status, proposed_at DESC)",
    "CREATE INDEX IF NOT EXISTS tuning_feedback_action_idx ON keeninsight.tuning_feedback (action_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS legacy_tune_tasks_status_idx ON keeninsight.legacy_tune_tasks (first_evaluate_status, one_step_status, multi_status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS rag_documents_source_type_idx ON keeninsight.rag_documents (source_type, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS rag_chunks_search_vector_idx ON keeninsight.rag_chunks USING GIN (search_vector)",
    """
    CREATE TABLE IF NOT EXISTS keeninsight.probe_catalog (
        id BIGSERIAL PRIMARY KEY,
        probe_kind TEXT NOT NULL CHECK (probe_kind IN ('kprobe', 'uprobe', 'uretprobe')),
        function_name TEXT NOT NULL,
        binary_path TEXT NOT NULL DEFAULT '',
        symbol_offset BIGINT,
        source_file TEXT,
        source_line INTEGER,
        source_digest TEXT,
        selected BOOLEAN NOT NULL DEFAULT FALSE,
        attached BOOLEAN NOT NULL DEFAULT FALSE,
        lasso_coefficient DOUBLE PRECISION,
        importance DOUBLE PRECISION,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (probe_kind, function_name, binary_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.probe_sessions (
        id BIGSERIAL PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'created'
            CHECK (status IN ('created', 'starting', 'running', 'stopping', 'stopped', 'failed')),
        transport TEXT NOT NULL DEFAULT 'unavailable',
        target_pids JSONB NOT NULL DEFAULT '[]'::jsonb,
        selected_probes JSONB NOT NULL DEFAULT '[]'::jsonb,
        config JSONB NOT NULL DEFAULT '{}'::jsonb,
        started_at TIMESTAMPTZ,
        stopped_at TIMESTAMPTZ,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.ebpf_events (
        id BIGSERIAL PRIMARY KEY,
        session_id BIGINT REFERENCES keeninsight.probe_sessions(id) ON DELETE SET NULL,
        observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        event_type TEXT NOT NULL,
        probe_kind TEXT NOT NULL,
        function_name TEXT NOT NULL,
        binary_path TEXT,
        pid INTEGER,
        tid INTEGER,
        cpu INTEGER,
        cgroup_id BIGINT,
        cid BIGINT,
        query_id TEXT,
        duration_ns BIGINT,
        bytes BIGINT,
        is_anomaly BOOLEAN NOT NULL DEFAULT FALSE,
        transport TEXT NOT NULL DEFAULT 'database',
        payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.event_outbox (
        id BIGSERIAL PRIMARY KEY,
        topic TEXT NOT NULL,
        event_key TEXT,
        payload JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'published', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.sql_lifecycle_events (
        id BIGSERIAL PRIMARY KEY,
        session_id BIGINT NOT NULL REFERENCES keeninsight.probe_sessions(id)
            ON DELETE CASCADE,
        query_id TEXT NOT NULL,
        query_text TEXT,
        query_id_source TEXT,
        function_name TEXT NOT NULL,
        first_observed_at TIMESTAMPTZ NOT NULL,
        last_observed_at TIMESTAMPTZ NOT NULL,
        event_count BIGINT NOT NULL DEFAULT 0,
        duration_ns BIGINT NOT NULL DEFAULT 0,
        bytes BIGINT NOT NULL DEFAULT 0,
        anomaly_count BIGINT NOT NULL DEFAULT 0,
        source_event_ids BIGINT[] NOT NULL DEFAULT '{}',
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (session_id, query_id, function_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.model_runs (
        id BIGSERIAL PRIMARY KEY,
        model_type TEXT NOT NULL,
        algorithm TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'training'
            CHECK (status IN ('training', 'ready', 'failed')),
        feature_names JSONB NOT NULL DEFAULT '[]'::jsonb,
        target_name TEXT,
        train_count INTEGER NOT NULL DEFAULT 0,
        metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
        artifact_path TEXT,
        model_digest TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.model_predictions (
        id BIGSERIAL PRIMARY KEY,
        model_run_id BIGINT REFERENCES keeninsight.model_runs(id) ON DELETE SET NULL,
        model_type TEXT NOT NULL,
        query TEXT,
        features JSONB NOT NULL DEFAULT '{}'::jsonb,
        prediction JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.model_attributions (
        id BIGSERIAL PRIMARY KEY,
        model_run_id BIGINT REFERENCES keeninsight.model_runs(id) ON DELETE SET NULL,
        prediction_id BIGINT REFERENCES keeninsight.model_predictions(id) ON DELETE SET NULL,
        method TEXT NOT NULL,
        feature_name TEXT NOT NULL,
        feature_value DOUBLE PRECISION,
        contribution DOUBLE PRECISION NOT NULL,
        base_value DOUBLE PRECISION,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.differential_profiles (
        id BIGSERIAL PRIMARY KEY,
        baseline_session_id BIGINT NOT NULL REFERENCES keeninsight.probe_sessions(id)
            ON DELETE CASCADE,
        degraded_session_id BIGINT NOT NULL REFERENCES keeninsight.probe_sessions(id)
            ON DELETE CASCADE,
        method TEXT NOT NULL,
        features JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (baseline_session_id, degraded_session_id, method)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.source_analyses (
        id BIGSERIAL PRIMARY KEY,
        source_root TEXT NOT NULL,
        knob TEXT,
        function_name TEXT,
        file_path TEXT,
        line_number INTEGER,
        relation_type TEXT NOT NULL,
        ir_symbol TEXT,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        source_digest TEXT,
        analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source_root, knob, function_name, file_path, line_number, relation_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.code_knowledge (
        id BIGSERIAL PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        source_root TEXT NOT NULL,
        knob TEXT NOT NULL,
        target_functions JSONB NOT NULL DEFAULT '[]'::jsonb,
        retrieval_context JSONB NOT NULL DEFAULT '{}'::jsonb,
        hypothesis JSONB NOT NULL DEFAULT '{}'::jsonb,
        validation JSONB NOT NULL DEFAULT '{}'::jsonb,
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.tuning_experiences (
        id BIGSERIAL PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        cluster_id INTEGER,
        context JSONB NOT NULL DEFAULT '{}'::jsonb,
        root_cause TEXT NOT NULL,
        action JSONB NOT NULL DEFAULT '{}'::jsonb,
        support INTEGER NOT NULL DEFAULT 0,
        success_count INTEGER NOT NULL DEFAULT 0,
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.rag_embeddings (
        chunk_id BIGINT PRIMARY KEY REFERENCES keeninsight.rag_chunks(id) ON DELETE CASCADE,
        model_name TEXT NOT NULL,
        dimensions INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        embedding DOUBLE PRECISION[] NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.optimizer_runs (
        id BIGSERIAL PRIMARY KEY,
        query TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'completed', 'failed')),
        baseline JSONB NOT NULL DEFAULT '{}'::jsonb,
        knowledge JSONB NOT NULL DEFAULT '[]'::jsonb,
        source_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        selected_candidate TEXT,
        ranking JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keeninsight.optimizer_candidates (
        id BIGSERIAL PRIMARY KEY,
        run_id BIGINT NOT NULL REFERENCES keeninsight.optimizer_runs(id) ON DELETE CASCADE,
        candidate_key TEXT NOT NULL,
        action_type TEXT NOT NULL,
        query TEXT NOT NULL,
        hints JSONB NOT NULL DEFAULT '{}'::jsonb,
        proposal JSONB NOT NULL DEFAULT '{}'::jsonb,
        plan JSONB NOT NULL DEFAULT '{}'::jsonb,
        measurement JSONB NOT NULL DEFAULT '{}'::jsonb,
        semantic_equivalence JSONB NOT NULL DEFAULT '{}'::jsonb,
        score DOUBLE PRECISION,
        rank INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id, candidate_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ebpf_events_observed_at_idx ON keeninsight.ebpf_events (observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS ebpf_events_function_idx ON keeninsight.ebpf_events (function_name, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS event_outbox_status_idx ON keeninsight.event_outbox (status, created_at)",
    "CREATE INDEX IF NOT EXISTS sql_lifecycle_session_query_idx ON keeninsight.sql_lifecycle_events (session_id, query_id, first_observed_at)",
    "CREATE INDEX IF NOT EXISTS model_predictions_type_idx ON keeninsight.model_predictions (model_type, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS model_attributions_run_idx ON keeninsight.model_attributions (model_run_id, contribution DESC)",
    "CREATE INDEX IF NOT EXISTS differential_profiles_created_idx ON keeninsight.differential_profiles (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS source_analyses_knob_idx ON keeninsight.source_analyses (knob, function_name)",
    "CREATE INDEX IF NOT EXISTS code_knowledge_knob_idx ON keeninsight.code_knowledge (knob, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS tuning_experiences_cluster_idx ON keeninsight.tuning_experiences (cluster_id, confidence DESC)",
    "CREATE INDEX IF NOT EXISTS rag_embeddings_model_idx ON keeninsight.rag_embeddings (model_name, dimensions)",
    "CREATE INDEX IF NOT EXISTS optimizer_candidates_run_idx ON keeninsight.optimizer_candidates (run_id, rank)",
]

DEFAULT_RULES = [
    (
        "Connection saturation",
        "connection_saturation",
        "warning",
        {"ratio": 0.80},
    ),
    (
        "Blocked sessions",
        "lock_wait",
        "critical",
        {"minimum_waiting_sessions": 1},
    ),
    (
        "Slow statement",
        "slow_query",
        "warning",
        {"mean_exec_time_ms": 1000.0, "minimum_calls": 1},
    ),
    (
        "Temporary-file spill",
        "temp_spill",
        "warning",
        {"minimum_temp_blocks_written": 1},
    ),
    (
        "Low database cache hit ratio",
        "cache_hit_ratio",
        "warning",
        {"ratio": 0.90, "minimum_block_reads": 100},
    ),
]


def ensure_schema(db: Database) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)
            for name, kind, severity, config in DEFAULT_RULES:
                cur.execute(
                    """
                    INSERT INTO keeninsight.alert_rules (name, kind, severity, config)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    (name, kind, severity, _json(config)),
                )

    # This is intentionally best-effort for an existing server.  The API will
    # report that pg_stat_statements is unavailable instead of inventing data.
    try:
        db.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    except (
        InsufficientPrivilege,
        UndefinedFile,
        DuplicateObject,
        ObjectNotInPrerequisiteState,
    ):
        pass


def _json(value: object) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))
