from __future__ import annotations

import json
import hashlib
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audit import record_audit
from .collector import Collector
from .config import Settings
from .db import Database
from .diagnose import (
    UnsafeQuery,
    diagnose_incident,
    diagnose_plan,
    grounded_explanation,
    parallel_workers_action,
)
from .ebpf_monitor import EBPFUnavailable, EbpfMonitor
from .eventing import KafkaEventBus, KafkaEventConsumer
from .experience import ExperienceMiner, ExperienceMiningError
from .feedback import FeedbackError, list_action_feedback, record_action_feedback
from .knowledge import ensure_knowledge, list_knowledge
from .legacy_tune import LegacyTuneCompatibility
from .llm import LLMClient
from .modeling import ModelManager, ModelingError
from .llvm_analysis import LLVMAnalysisError, SourceAnalyzer
from .rag import RagError, RagIndexer, ask as rag_ask, database_status as rag_database_status, search as rag_search
from .vector_rag import EmbeddingIndex, VectorRagError
from .pg import (
    activity,
    current_settings_map,
    database_metrics,
    indexes,
    jsonable,
    jsonable_rows,
    locks,
    server_meta,
    settings,
    tables,
    top_queries,
)
from .probes import ProbeSelectionError, train_lasso_probe_selector
from .schema import ensure_schema
from .tuning import (
    ActionValidationError,
    apply_action,
    create_action,
    get_action,
    list_actions,
    reject_action,
    rollback_action,
)
from .workload import WorkloadError, WorkloadRunner

LOG = logging.getLogger("keeninsight")
BASE_DIR = Path(__file__).resolve().parent
RAG_EVALUATION_PATH = BASE_DIR.parent / "artifacts" / "rag-evaluation.json"


def _rag_evaluation_report() -> dict[str, Any]:
    """Read the checked-in retrieval report without making it a live claim."""
    if not RAG_EVALUATION_PATH.is_file():
        return {"available": False, "reason": "run scripts/evaluate_rag.py first"}
    try:
        report = json.loads(RAG_EVALUATION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"could not read report: {exc}"}
    return {"available": True, "report": report}


def _static_source_manifest(source_dir: Path) -> dict[str, Any]:
    """Return a small integrity record for the shipped static RAG corpus."""
    manifest_path = source_dir / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return {
            "available": False,
            "path": str(manifest_path),
            "reason": "static source manifest is missing",
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "path": str(manifest_path),
            "reason": f"could not read static source manifest: {exc}",
        }
    counts: dict[str, int] = {}
    for record in payload.get("records") or []:
        source_type = record.get("source_type")
        if source_type:
            counts[str(source_type)] = counts.get(str(source_type), 0) + 1
    try:
        display_path = manifest_path.resolve().relative_to(BASE_DIR.parent.resolve()).as_posix()
    except ValueError:
        display_path = str(manifest_path)
    return {
        "available": True,
        "path": display_path,
        "format": payload.get("format"),
        "record_count": payload.get("count", len(payload.get("records") or [])),
        "source_type_counts": counts,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class ActionProposalRequest(BaseModel):
    action_type: str
    target: Dict[str, Any]
    description: Optional[str] = None
    incident_id: Optional[int] = None


class RulePatch(BaseModel):
    enabled: Optional[bool] = None
    severity: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class WorkloadRequest(BaseModel):
    phase: str = "baseline"
    repetitions: int = Field(default=5, ge=1, le=50)
    warmup_repetitions: int = Field(default=2, ge=0, le=10)
    parameters: Dict[str, Any] = Field(default_factory=dict)


class NormalLoadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # The deployed host still runs Python 3.8; Optional keeps Pydantic v2
    # from trying to evaluate the PEP 604 union at request-model startup.
    rate_per_second: Optional[float] = Field(default=None, ge=0.05, le=20.0)


class AutoResolveRequest(BaseModel):
    rollback_after_success: bool = Field(
        default=False,
        description="Run the complete automatic validation loop, then restore the previous setting after a successful result",
    )


class FeedbackRequest(BaseModel):
    baseline_run_id: int = Field(gt=0)
    after_run_id: int = Field(gt=0)


class RagAskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=5000)
    top_k: int = Field(default=5, ge=1, le=10)
    source_type: Optional[str] = Field(default=None, max_length=80)
    mode: str = Field(default="lexical", max_length=20)


class EbpfStartRequest(BaseModel):
    functions: List[str] = Field(default_factory=list, max_length=20)
    target_pids: List[int] = Field(default_factory=list, max_length=100)
    binary_path: Optional[str] = Field(default=None, max_length=4096)
    adaptive: bool = Field(
        default=False,
        description="Enable the online measured invocation-rate guard for optional user probes",
    )
    max_event_rate: float = Field(
        default=1000.0,
        gt=0,
        le=1_000_000_000.0,
        description="Measured invocations/second budget for optional user-space probes",
    )


class ModelTrainRequest(BaseModel):
    model_type: str = Field(default="latency_prediction", max_length=80)
    limit: int = Field(default=2000, ge=8, le=10000)


class ModelPredictRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    model_type: str = Field(default="latency_prediction", max_length=80)


class ModelScreenRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    threshold_ms: float = Field(default=1000.0, ge=0.0, le=86_400_000.0)


class DifferentialProfileRequest(BaseModel):
    baseline_session_id: int = Field(gt=0)
    degraded_session_id: int = Field(gt=0)


class SystemTuningRequest(BaseModel):
    baseline_session_id: int = Field(gt=0)
    degraded_session_id: int = Field(gt=0)
    max_hotspots: int = Field(default=10, ge=1, le=50)


class EbpfAdaptRequest(BaseModel):
    min_sessions: int = Field(default=4, ge=3, le=100)
    max_probes: int = Field(default=8, ge=1, le=20)
    alpha: float = Field(default=0.001, gt=0, le=1000)
    max_event_rate: float = Field(
        default=1000.0,
        gt=0,
        le=1_000_000_000.0,
        description="Measured invocations/second budget for optional user-space probes",
    )


class SourceAnalysisRequest(BaseModel):
    knobs: List[str] = Field(default_factory=list, max_length=20)
    functions: List[str] = Field(default_factory=list, max_length=50)
    max_files: int = Field(default=80, ge=1, le=500)


class SourceKnowledgeRequest(BaseModel):
    knobs: List[str] = Field(default_factory=list, max_length=20)
    functions: List[str] = Field(default_factory=list, max_length=50)
    limit: int = Field(default=50, ge=1, le=200)


def _build_app() -> FastAPI:
    settings_obj = Settings.from_env()
    db = Database(settings_obj.database_url)
    llm_client = LLMClient(
        url=settings_obj.rag_llm_url,
        model=settings_obj.rag_llm_model,
        token=settings_obj.rag_llm_token,
    )
    collector = Collector(
        db,
        settings_obj.collect_interval_seconds,
        retention_hours=settings_obj.snapshot_retention_hours,
        cleanup_interval_seconds=settings_obj.collect_interval_seconds,
    )
    workloads = WorkloadRunner(db)
    legacy_tune = LegacyTuneCompatibility(db, workloads)
    rag_indexer = RagIndexer(
        db,
        source_dir=settings_obj.rag_sources_dir,
        interval_seconds=settings_obj.rag_sync_interval_seconds,
        llm_url=settings_obj.rag_llm_url,
        llm_model=settings_obj.rag_llm_model,
        llm_token=settings_obj.rag_llm_token,
    )
    vector_index = EmbeddingIndex(
        db,
        model_name=settings_obj.embedding_model,
        cache_dir=settings_obj.embedding_cache_dir,
    )
    event_bus = KafkaEventBus(
        db,
        bootstrap_servers=settings_obj.kafka_bootstrap_servers,
        topic=settings_obj.kafka_topic,
    )
    event_consumer = KafkaEventConsumer(
        db,
        bootstrap_servers=settings_obj.kafka_bootstrap_servers,
        topic=settings_obj.kafka_topic,
        group_id=settings_obj.kafka_group_id,
    )
    ebpf_monitor = EbpfMonitor(
        db,
        event_bus,
        reporting_interval_seconds=settings_obj.ebpf_probe_interval_seconds,
        auto_adapt=settings_obj.ebpf_auto_adapt,
        max_event_rate=settings_obj.ebpf_max_event_rate,
        long_query_threshold_seconds=settings_obj.ebpf_long_query_threshold_seconds,
    )
    model_manager = ModelManager(db, settings_obj.model_dir)
    source_analyzer = SourceAnalyzer(
        db,
        settings_obj.llvm_source_root,
        llm_client=llm_client,
    )
    def source_evidence() -> dict[str, Any]:
        return {
            "items": db.fetch_all(
                """
                SELECT knob, function_name, file_path, line_number,
                       relation_type, ir_symbol, evidence, source_digest
                FROM keeninsight.source_analyses
                WHERE relation_type IN (
                    'knob_source_reference', 'llvm_knob_propagation',
                    'llvm_data_flow', 'llvm_control_edge'
                )
                ORDER BY analyzed_at DESC LIMIT 50
                """
            )
        }

    experience_miner = ExperienceMiner(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        last_error: Optional[Exception] = None
        for attempt in range(30):
            try:
                db.open()
                ensure_schema(db)
                ensure_knowledge(db)
                workloads.recover_stale_runs()
                legacy_tune.recover_stale_tasks()
                # Initial indexing is source ingestion, not workload
                # execution.  The periodic sync remains in its own daemon
                # thread so the original collector/workload path is unchanged.
                rag_indexer.sync_once()
                event_bus.start()
                break
            except Exception as exc:
                last_error = exc
                LOG.warning("waiting for PostgreSQL (%s/30): %s", attempt + 1, exc)
                try:
                    db.close()
                except Exception:
                    pass
                if attempt == 29:
                    raise RuntimeError(f"could not connect to PostgreSQL: {last_error}") from exc
                time.sleep(1)
        app.state.db = db
        app.state.collector = collector
        app.state.workloads = workloads
        app.state.legacy_tune = legacy_tune
        app.state.event_bus = event_bus
        app.state.event_consumer = event_consumer
        app.state.ebpf_monitor = ebpf_monitor
        app.state.model_manager = model_manager
        app.state.source_analyzer = source_analyzer
        app.state.llm = llm_client
        app.state.experience_miner = experience_miner
        collector.start()
        # Do not let the first browser request race the first database
        # observation.  The collector remains a daemon and continues polling
        # in the background; this short readiness wait only seeds the overview
        # with a real snapshot before the HTTP service is declared ready.
        initial_sample_timeout = max(
            5.0,
            min(30.0, settings_obj.collect_interval_seconds * 2.0),
        )
        if not collector.wait_for_first_sample(timeout=initial_sample_timeout):
            LOG.warning(
                "collector has not persisted an initial snapshot after %.1fs; "
                "the background worker will continue retrying",
                initial_sample_timeout,
            )
        rag_indexer.start()
        event_consumer.start()
        if settings_obj.ebpf_enabled:
            try:
                ebpf_monitor.start()
            except Exception as exc:
                LOG.warning("configured eBPF monitor did not start: %s", exc)
        try:
            yield
        finally:
            workloads.stop_all()
            ebpf_monitor.stop()
            event_consumer.stop()
            event_bus.stop()
            rag_indexer.stop()
            collector.stop()
            db.close()

    app = FastAPI(
        title="KeenInsight for PostgreSQL",
        version="0.1.0",
        description="Live evidence-driven PostgreSQL monitoring, diagnosis, and safe tuning",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    legacy_tune.register(app)

    def demo_manifest_payload() -> dict[str, Any]:
        """Describe the live environment and the reproducibility contract."""
        evidence_counts = db.fetch_one(
            """
            SELECT
                (SELECT count(*)::int FROM keeninsight.ebpf_events) AS ebpf_events,
                (SELECT count(*)::int FROM keeninsight.event_outbox) AS outbox_events,
                (SELECT count(*)::int FROM keeninsight.probe_sessions) AS probe_sessions,
                (SELECT count(*)::int FROM keeninsight.sql_lifecycle_events) AS sql_lifecycle_events,
                (SELECT count(*)::int FROM keeninsight.model_runs) AS model_runs,
                (SELECT count(*)::int FROM keeninsight.model_predictions) AS model_predictions,
                (SELECT count(*)::int FROM keeninsight.model_attributions) AS model_attributions,
                (SELECT count(*)::int FROM keeninsight.differential_profiles) AS differential_profiles,
                (SELECT count(*)::int FROM keeninsight.tuning_actions) AS tuning_actions,
                (SELECT count(*)::int FROM keeninsight.tuning_feedback) AS tuning_feedback,
                (SELECT count(*)::int FROM keeninsight.legacy_tune_tasks) AS legacy_tune_tasks,
                (SELECT count(*)::int FROM keeninsight.legacy_tune_tasks
                    WHERE first_evaluate_status = 'success') AS legacy_tune_baselines,
                (SELECT count(*)::int FROM keeninsight.legacy_tune_tasks
                    WHERE one_step_status = 'success') AS legacy_tune_one_step_successes,
                (SELECT count(*)::int FROM keeninsight.legacy_tune_tasks
                    WHERE multi_status = 'success') AS legacy_tune_multi_successes,
                (SELECT count(*)::int FROM keeninsight.source_analyses) AS source_analysis_records,
                (SELECT count(*)::int FROM keeninsight.source_analyses WHERE relation_type = 'llvm_call_edge') AS llvm_call_edges,
                (SELECT count(*)::int FROM keeninsight.source_analyses WHERE relation_type = 'llvm_knob_propagation') AS llvm_knob_propagations,
                (SELECT count(*)::int FROM keeninsight.source_analyses WHERE relation_type = 'llvm_control_edge') AS llvm_control_edges,
                (SELECT count(*)::int FROM keeninsight.source_analyses WHERE relation_type = 'llvm_data_flow') AS llvm_data_flows,
                (SELECT count(*)::int FROM keeninsight.code_knowledge) AS code_knowledge_records,
                (SELECT count(*)::int FROM keeninsight.tuning_experiences) AS tuning_experiences,
                (SELECT count(*)::int FROM keeninsight.rag_embeddings) AS rag_embeddings
            """
        ) or {}
        return {
            "format": "keeninsight-demo-manifest-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "claim_boundary": {
                "implementation": "PostgreSQL + eBPF/BCC + Kafka + XGBoost/SHAP + LLVM + embedding RAG",
                "paper_alignment": "The paper-described capabilities are implemented in this artifact; this manifest records runtime limits and evidence instead of narrowing the paper claims.",
                "capabilities": {
                    "adaptive_ebpf_monitoring": "live BCC kprobe/uprobe/uretprobe attachment with Lasso-selected probe sets and an online measured invocation-rate guard",
                    "kafka_event_stream": "Kafka producer/consumer with auditable outbox and UUID deduplication",
                    "sql_lifecycle_reconstruction": "query-id/time-window reconstruction from persisted eBPF events, including exact batched invocation counts",
                    "latency_and_root_cause_models": "XGBoost temporal validation with persisted plan-only features and TreeSHAP",
                    "differential_function_profiling": "real eBPF event-vector deltas plus function-level XGBoost/TreeSHAP",
                    "llvm_code_propagation": "clang-generated LLVM IR CFG/SSA data-flow analysis, call graph, and knob-to-function propagation",
                    "tuning_agent": "retrieval-grounded system parameter actions, measured ranking, semantic verification, reviewed actions, compiler-backed code retrieval/summary, and an automatic closed-loop deploy/feedback/rollback endpoint",
                    "upstream_workload_tune_compatibility": "the main/WorkloadTune_new configuration → baseline → exception → one-step → apply → multi-round API sequence backed by real PostgreSQL runs and typed feedback",
                    "embedding_rag": "384-dimensional BGE embeddings persisted in PostgreSQL with vector/hybrid evaluation",
                    "experience_closure": "feedback clustering, association rules, confidence, and provenance-backed runtime RAG",
                },
                "runtime_evidence_counts": jsonable(evidence_counts),
                "operational_limits": [
                    "eBPF requires a kernel with BCC support and sufficient tracing privileges",
                    "the adaptive selector uses a configurable measured invocation-rate budget; high-frequency probes are explicitly rejected when the budget would be exceeded",
                    "the external LLM validator is optional; deterministic typed validation remains mandatory",
                    "PostgreSQL 12 uses the pg_stat_activity query fingerprint fallback when compute_query_id is unavailable",
                    "legacy WorkloadTune benchmark labels are retained as metadata and mapped to named PostgreSQL scenarios; they are not relabeled as TPC-C/TPC-H/Sysbench benchmark results",
                    "the db-replay branch is an independent Go replay service and is not exposed by this FastAPI process",
                ],
                "verification_artifacts": [
                    "artifacts/final-regression-pg12.json",
                    "artifacts/aa-control-results.json",
                    "artifacts/rag-vector-evaluation-strict.json",
                    "artifacts/rag-hybrid-evaluation-strict.json",
                    "artifacts/ebpf-smoke-strict.json",
                    "artifacts/ebpf-overhead-strict.json",
                    "artifacts/ebpf-auto-adapt-strict.json",
                    "artifacts/ebpf-long-query-strict.json",
                    "artifacts/auto-resolve-strict.json",
                    "artifacts/legacy-workload-tune-compatibility.json",
                    "artifacts/rag-evaluation.json",
                    "docs/UPSTREAM_COMPATIBILITY_AUDIT.md",
                ],
            },
            "server": jsonable(server_meta(db)),
            "settings": jsonable(current_settings_map(db)),
            "collector": collector.status(),
            "observability": {
                "ebpf": jsonable(ebpf_monitor.status()),
                "kafka": jsonable(event_bus.status()),
                "kafka_consumer": jsonable(event_consumer.status()),
            },
            "analysis": jsonable(model_manager.status()),
            "source_analysis": jsonable(source_analyzer.status()),
            "llm": llm_client.status(),
            "experiences": jsonable(experience_miner.status()),
            "demo_state": jsonable(workloads.demo_state()),
            "scenarios": [
                {
                    "name": item.get("name"),
                    "title": item.get("title"),
                    "query": item.get("query"),
                    "parameters": item.get("parameters") or {},
                }
                for item in workloads.scenarios()
            ],
            "measurement_contract": {
                "timing": "client-side monotonic timing around the same SQL, with PostgreSQL EXPLAIN ANALYZE evidence",
                "warmups_and_repetitions": "per workload request; raw samples are persisted",
                "feedback_minimum_timed_samples": 3,
                "feedback_requires_same_result": True,
                "feedback_requires_verified_plan_signal": True,
                "latency_effective_threshold_pct": 5.0,
                "statistics_maintenance_criterion": "estimate/actual agreement plus result preservation; latency improvement is not required",
                "p95_definition": "linear interpolation percentile over the timed sample list",
            },
            "rag": {
                "database": jsonable(rag_database_status(db)),
                "indexer": rag_indexer.status(),
                "static_manifest": _static_source_manifest(rag_indexer.source_dir),
                "fixed_evaluation": _rag_evaluation_report(),
                "vector": vector_index.status(),
            },
        }

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        """Open the original English demo shell at the service root.

        The evidence console remains available at ``/console``.  Keeping the
        original page set as the default matters for the ICDE demo because the
        supplied static prototype is itself part of the intended user flow.
        """
        return RedirectResponse(url="/static/legacy/host-list.html")

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        """Open the connection gate before entering the live control plane."""
        return FileResponse(
            BASE_DIR / "static" / "console-wait.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/console/home", include_in_schema=False)
    def console_home() -> FileResponse:
        """Open the evidence-first PostgreSQL control plane after readiness."""
        return FileResponse(
            BASE_DIR / "static" / "console.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        try:
            meta = server_meta(db)
            db_ok = bool(meta)
            error = None
        except Exception as exc:
            db_ok = False
            error = str(exc)
            meta = {}
        return {
            "ok": db_ok,
            "database": db_ok,
            "error": error,
            "server": jsonable(meta),
            "collector": collector.status(),
            "rag": rag_indexer.status(),
            "llm": llm_client.status(),
        }

    @app.get("/api/meta")
    def meta() -> Dict[str, Any]:
        return {
            "server": jsonable(server_meta(db)),
            "settings": jsonable(current_settings_map(db)),
            "collector": collector.status(),
            "rag": rag_indexer.status(),
            "llm": llm_client.status(),
            "scopes": {
                "database": "current_database()",
                "pg_stat_statements": "current database entries only",
            },
        }

    @app.get("/api/demo/manifest")
    def demo_manifest() -> Dict[str, Any]:
        return jsonable(demo_manifest_payload())

    @app.get("/api/dashboard")
    def dashboard() -> Dict[str, Any]:
        snapshot = db.fetch_one(
            """
            SELECT id, collected_at, server_version, database_name, payload,
                   collection_error
            FROM keeninsight.snapshots
            WHERE collected_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
            ORDER BY collected_at DESC LIMIT 1
            """,
            (settings_obj.snapshot_retention_hours,),
        )
        incidents = db.fetch_all(
            """
            SELECT id, signature, kind, severity, status, summary, first_seen,
                   last_seen, resolved_at, evidence, diagnosis, metadata
            FROM keeninsight.incidents
            WHERE status = 'active'
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     last_seen DESC
            LIMIT 20
            """
        )
        return {
            "snapshot": jsonable(snapshot) if snapshot else None,
            "active_incidents": jsonable_rows(incidents),
            "collector": collector.status(),
        }

    @app.get("/api/metrics")
    def metrics(limit: int = Query(default=120, ge=1, le=1000)) -> Dict[str, Any]:
        rows = db.fetch_all(
            """
            SELECT id, collected_at, server_version, database_name, payload,
                   collection_error
            FROM keeninsight.snapshots
            WHERE collected_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
            ORDER BY collected_at DESC LIMIT %s
            """,
            (settings_obj.snapshot_retention_hours, limit),
        )
        rows.reverse()
        return {"items": jsonable_rows(rows), "collector": collector.status()}

    @app.get("/api/dashboard/metrics")
    def dashboard_metrics(limit: int = Query(default=120, ge=1, le=1000)) -> Dict[str, Any]:
        """Return the small, chart-oriented projection used by the legacy UI.

        ``/api/metrics`` deliberately exposes complete raw snapshots for
        audits.  The original dashboard only needs host/database counters and
        a few current counts, so this projection avoids shipping every
        activity/query row on each browser refresh while retaining the same
        persisted source values.
        """
        rows = db.fetch_all(
            """
            SELECT id, collected_at, payload, collection_error
            FROM keeninsight.snapshots
            WHERE collected_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
            ORDER BY collected_at DESC LIMIT %s
            """,
            (settings_obj.snapshot_retention_hours, limit),
        )
        items: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            items.append(
                {
                    "id": row.get("id"),
                    "collected_at": row.get("collected_at"),
                    "collection_error": row.get("collection_error"),
                    "host": payload.get("host") or {},
                    "database": payload.get("database") or {},
                    "rates": payload.get("rates") or {},
                    "latency": payload.get("latency") or {},
                    "activity_count": len(payload.get("activity") or []),
                    "lock_count": len(payload.get("locks") or []),
                    "query_count": len(payload.get("top_queries") or []),
                    "features": payload.get("features") or {},
                }
            )
        return {"items": jsonable_rows(items), "collector": collector.status()}

    @app.get("/api/activity")
    def get_activity(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        return {"items": jsonable_rows(activity(db, limit))}

    @app.get("/api/locks")
    def get_locks() -> Dict[str, Any]:
        return {"items": jsonable_rows(locks(db))}

    @app.get("/api/queries")
    def get_queries(limit: int = Query(default=50, ge=1, le=200)) -> Dict[str, Any]:
        rows, error = top_queries(db, limit)
        return {"items": jsonable_rows(rows), "unavailable_reason": error}

    @app.get("/api/settings")
    def get_settings(search: Optional[str] = None) -> Dict[str, Any]:
        if search:
            term = f"%{search}%"
            rows = db.fetch_all(
                """
                SELECT name, setting, COALESCE(unit, '') AS unit, vartype,
                       context, min_val, max_val, enumvals, source, boot_val,
                       reset_val, pending_restart, short_desc, extra_desc
                FROM pg_settings
                WHERE name ILIKE %s OR short_desc ILIKE %s
                ORDER BY name LIMIT 200
                """,
                (term, term),
            )
        else:
            rows = settings(
                db,
                [
                    "max_connections",
                    "work_mem",
                    "shared_buffers",
                    "effective_cache_size",
                    "random_page_cost",
                    "track_io_timing",
                    "compute_query_id",
                ],
            )
        return {"items": jsonable_rows(rows)}

    @app.get("/api/tables")
    def get_tables(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        return {"items": jsonable_rows(tables(db, limit))}

    @app.get("/api/indexes")
    def get_indexes(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        return {"items": jsonable_rows(indexes(db, limit))}

    @app.get("/api/rules")
    def get_rules() -> Dict[str, Any]:
        return {
            "items": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, name, kind, severity, enabled, config,
                           created_at, updated_at
                    FROM keeninsight.alert_rules ORDER BY id
                    """
                )
            )
        }

    @app.patch("/api/rules/{rule_id}")
    def patch_rule(rule_id: int, patch: RulePatch) -> Dict[str, Any]:
        if patch.severity is not None and patch.severity not in {"info", "warning", "critical"}:
            raise HTTPException(status_code=400, detail="severity must be info, warning, or critical")
        current = db.fetch_one("SELECT * FROM keeninsight.alert_rules WHERE id = %s", (rule_id,))
        if not current:
            raise HTTPException(status_code=404, detail="rule not found")
        enabled = current["enabled"] if patch.enabled is None else patch.enabled
        severity = current["severity"] if patch.severity is None else patch.severity
        config = current["config"] if patch.config is None else patch.config
        try:
            _validate_rule_config(current["kind"], config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = db.fetch_one(
            """
            UPDATE keeninsight.alert_rules
            SET enabled = %s, severity = %s, config = %s::jsonb, updated_at = now()
            WHERE id = %s
            RETURNING id, name, kind, severity, enabled, config, created_at, updated_at
            """,
            (enabled, severity, json.dumps(config), rule_id),
        )
        record_audit(
            db,
            "rule_updated",
            "alert_rule",
            rule_id,
            {
                "before": {
                    "enabled": current["enabled"],
                    "severity": current["severity"],
                    "config": current["config"],
                },
                "after": {"enabled": enabled, "severity": severity, "config": config},
            },
        )
        return jsonable(row or {})

    @app.get("/api/incidents")
    def get_incidents(
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Dict[str, Any]:
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status = %s")
            params.append(status)
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = db.fetch_all(
            f"""
            SELECT id, signature, kind, severity, status, summary, first_seen,
                   last_seen, resolved_at, evidence, diagnosis, metadata
            FROM keeninsight.incidents {where}
            ORDER BY last_seen DESC LIMIT %s
            """,
            tuple(params),
        )
        return {"items": jsonable_rows(rows)}

    @app.get("/api/incidents/{incident_id}")
    def get_incident(incident_id: int) -> Dict[str, Any]:
        row = db.fetch_one(
            """
            SELECT id, signature, kind, severity, status, summary, first_seen,
                   last_seen, resolved_at, evidence, diagnosis, metadata
            FROM keeninsight.incidents WHERE id = %s
            """,
            (incident_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="incident not found")
        row["actions"] = list_actions_for_incident(db, incident_id)
        return jsonable(row)

    @app.post("/api/incidents/{incident_id}/diagnose")
    def diagnose(incident_id: int, analyze: bool = False) -> Dict[str, Any]:
        try:
            result = diagnose_incident(
                db,
                incident_id,
                analyze=analyze,
                llm_client=llm_client,
            )
            persisted_actions = []
            for proposal in (result.get("diagnosis") or {}).get("actions", []):
                try:
                    persisted_actions.append(
                        create_action(db, proposal, incident_id=incident_id)
                    )
                except ActionValidationError as exc:
                    persisted_actions.append({"not_persisted": str(exc), "proposal": proposal})
            result["actions"] = persisted_actions
            record_audit(
                db,
                "diagnosis_completed",
                "incident",
                incident_id,
                {
                    "root_causes": result.get("diagnosis", {}).get("root_causes", []),
                    "action_count": len(persisted_actions),
                    "analyze": analyze,
                },
            )
            return jsonable(result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/incidents/{incident_id}/resolve")
    def resolve_incident(incident_id: int) -> Dict[str, Any]:
        row = db.fetch_one(
            """
            UPDATE keeninsight.incidents
            SET status = 'resolved', resolved_at = now()
            WHERE id = %s
            RETURNING id, status, resolved_at
            """,
            (incident_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="incident not found")
        record_audit(
            db,
            "incident_resolved",
            "incident",
            incident_id,
            {"status": row["status"]},
        )
        return jsonable(row)

    @app.get("/api/knowledge")
    def get_knowledge(root_cause: Optional[str] = None) -> Dict[str, Any]:
        return {"items": jsonable_rows(list_knowledge(db, root_cause=root_cause))}

    @app.get("/api/llm/status")
    def llm_status() -> Dict[str, Any]:
        """Expose redacted model connectivity state for the demo UI."""
        return jsonable(llm_client.status())

    @app.get("/api/rag/status")
    def rag_status() -> Dict[str, Any]:
        return {
            "indexer": rag_indexer.status(),
            "database": jsonable(rag_database_status(db)),
            "vector": jsonable(vector_index.status()),
            "llm": llm_client.status(),
        }

    @app.get("/api/rag/search")
    def rag_search_endpoint(
        q: str = Query(min_length=1, max_length=5000),
        top_k: int = Query(default=5, ge=1, le=10),
        source_type: Optional[str] = Query(default=None, max_length=80),
        mode: str = Query(default="lexical", pattern="^(lexical|vector|hybrid)$"),
    ) -> Dict[str, Any]:
        try:
            if mode == "lexical":
                items = rag_search(db, q, top_k=top_k, source_type=source_type)
            elif mode == "vector":
                items = vector_index.search(q, top_k=top_k, source_type=source_type)
            else:
                lexical = rag_search(db, q, top_k=max(top_k, 5), source_type=source_type)
                vectors = vector_index.search(q, top_k=max(top_k, 5), source_type=source_type)
                by_id = {}
                for index, item in enumerate(lexical, 1):
                    by_id[item["chunk_id"]] = {**item, "hybrid_score": 1.0 / (index + 10.0)}
                for index, item in enumerate(vectors, 1):
                    current = by_id.get(item["chunk_id"])
                    if current:
                        current["hybrid_score"] += 1.0 / (index + 10.0)
                        current["vector_similarity"] = item.get("vector_similarity")
                    else:
                        by_id[item["chunk_id"]] = {**item, "hybrid_score": 1.0 / (index + 10.0)}
                items = sorted(by_id.values(), key=lambda item: item["hybrid_score"], reverse=True)[:top_k]
                for index, item in enumerate(items, 1):
                    item["citation_id"] = "R%d" % index
                    item["retrieval_mode"] = "hybrid-rrf"
            return {"query": q, "mode": mode, "items": jsonable_rows(items)}
        except (RagError, VectorRagError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/rag/ask")
    def rag_ask_endpoint(request: RagAskRequest) -> Dict[str, Any]:
        try:
            return jsonable(
                rag_ask(
                    db,
                    request.query,
                    top_k=request.top_k,
                    source_type=request.source_type,
                    mode=request.mode,
                    vector_search=vector_index.search,
                    llm_url=settings_obj.rag_llm_url,
                    llm_model=settings_obj.rag_llm_model,
                    llm_token=settings_obj.rag_llm_token,
                    llm_client=llm_client,
                )
            )
        except (RagError, VectorRagError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/rag/vector/status")
    def rag_vector_status() -> Dict[str, Any]:
        return jsonable(vector_index.status())

    @app.post("/api/rag/vector/sync")
    def rag_vector_sync(
        limit: int = Query(default=10000, ge=1, le=50000),
    ) -> Dict[str, Any]:
        try:
            result = vector_index.sync(limit=limit)
            record_audit(
                db,
                "rag_vector_sync_completed",
                "rag_embedding_index",
                None,
                {"processed": result.get("processed"), "created": result.get("created"), "updated": result.get("updated")},
            )
            return jsonable(result)
        except VectorRagError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/rag/sync")
    def rag_sync() -> Dict[str, Any]:
        try:
            result = rag_indexer.sync_once()
            record_audit(
                db,
                "rag_sync_completed",
                "rag",
                None,
                {
                    "static": result.get("static"),
                    "runtime": result.get("runtime"),
                    "documents": (result.get("database") or {}).get("documents"),
                    "chunks": (result.get("database") or {}).get("chunks"),
                },
            )
            return jsonable(
                {
                    "sync": result,
                    "indexer": rag_indexer.status(),
                    "database": result.get("database"),
                }
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"RAG sync failed: {exc}") from exc

    @app.get("/api/rag/evaluation")
    def rag_evaluation() -> Dict[str, Any]:
        return jsonable(_rag_evaluation_report())

    @app.get("/api/kafka/status")
    def kafka_status() -> Dict[str, Any]:
        return {
            "producer": event_bus.status(),
            "consumer": event_consumer.status(),
        }

    @app.get("/api/ebpf/status")
    def ebpf_status() -> Dict[str, Any]:
        return jsonable(ebpf_monitor.status())

    @app.get("/api/ebpf/probes")
    def ebpf_probes(
        refresh: bool = Query(default=False),
    ) -> Dict[str, Any]:
        try:
            rows = ebpf_monitor.catalog() if refresh else db.fetch_all(
                """
                SELECT id, probe_kind, function_name, binary_path, symbol_offset,
                       source_file, source_line, source_digest, selected, attached,
                       lasso_coefficient, importance, metadata, created_at, updated_at
                FROM keeninsight.probe_catalog
                ORDER BY selected DESC, importance DESC NULLS LAST, function_name
                """
            )
            return {"items": jsonable_rows(rows)}
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"probe catalog failed: {exc}") from exc

    @app.get("/api/ebpf/events")
    def ebpf_events(
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> Dict[str, Any]:
        return {"items": jsonable_rows(ebpf_monitor.recent_events(limit))}

    @app.get("/api/ebpf/timelines")
    def ebpf_timelines(
        session_id: int = Query(gt=0),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> Dict[str, Any]:
        try:
            items = ebpf_monitor.reconstruct_sql_lifecycle(session_id, limit=limit)
            return {
                "session_id": session_id,
                "method": "query-id/time-interval SQL lifecycle reconstruction",
                "items": jsonable_rows(items),
            }
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"SQL lifecycle reconstruction failed: {exc}") from exc

    @app.post("/api/system-tuning/recommend")
    def system_tuning_recommend(request: SystemTuningRequest) -> Dict[str, Any]:
        """Map measured eBPF hotspots through LLVM evidence to typed actions."""
        try:
            profile = model_manager.differential_profile(
                request.baseline_session_id, request.degraded_session_id
            )
            functions = [
                item.get("function_name")
                for item in (profile.get("functions") or [])[: request.max_hotspots]
                if item.get("function_name")
            ]

            # A call-graph path alone is not enough to justify changing a
            # server parameter: inclusive functions such as PortalRun can be
            # adjacent to many unrelated GUCs in a large PostgreSQL graph.
            # Bind the source evidence to the completed workload that overlaps
            # the degraded probe session and to the observed plan/temp signal.
            session = db.fetch_one(
                """
                SELECT id, started_at, stopped_at
                FROM keeninsight.probe_sessions WHERE id = %s
                """,
                (request.degraded_session_id,),
            )
            workload = None
            if session and session.get("started_at") and session.get("stopped_at"):
                workload = db.fetch_one(
                    """
                    SELECT id, scenario, observations, started_at, finished_at
                    FROM keeninsight.workload_runs
                    WHERE status = 'completed'
                      AND started_at <= %s AND finished_at >= %s
                    ORDER BY abs(extract(epoch from (started_at - %s))) ASC LIMIT 1
                    """,
                    (session["stopped_at"], session["started_at"], session["started_at"]),
                )
            observations = (workload or {}).get("observations") or {}
            plan_summary = observations.get("plan_summary") or {}
            scenario = str((workload or {}).get("scenario") or "")
            plan_signal = json.dumps(
                {"plan_summary": plan_summary, "plan": observations.get("plan") or {}},
                default=str,
            ).lower()
            plan_nodes = (observations.get("plan") or {}).get("nodes") or []
            node_types = [str(node.get("Node Type") or "").lower() for node in plan_nodes]
            has_hash_operator = "hash" in scenario.lower() or any(
                "hash" in node_type for node_type in node_types
            )
            has_sort_operator = "sort" in scenario.lower() or any(
                "sort" in node_type for node_type in node_types
            ) or bool(plan_summary.get("sort_methods"))
            has_aggregate_operator = (
                "aggregate" in scenario.lower() or any("aggregate" in node_type for node_type in node_types)
            ) and not has_hash_operator and not has_sort_operator
            has_temp_signal = bool(plan_summary.get("has_temp_blocks")) or any(
                token in plan_signal for token in ("temp read", "temp written", "disk", "spill")
            )
            def _positive(value: Any) -> bool:
                try:
                    return float(value or 0) > 0
                except (TypeError, ValueError):
                    return False

            has_parallel_signal = "parallel" in scenario.lower() or any(
                node_type in {"gather", "gather merge"}
                or _positive(node.get("Workers Planned"))
                or _positive(node.get("Workers Launched"))
                for node, node_type in zip(plan_nodes, node_types)
            )
            causal_knobs: list[str] = []
            if has_temp_signal or "_spill" in scenario.lower() or any(
                name in {"BufFileWrite", "vfs_write", "tuplesort_begin_heap"}
                for name in functions
            ):
                causal_knobs.append("work_mem")
            if has_parallel_signal:
                causal_knobs.append("max_parallel_workers_per_gather")

            runtime_source_rows = source_analyzer.knob_evidence_for_functions(functions)

            # Add the direct operator-level references from the exact
            # PostgreSQL source files selected by the measured plan.  These
            # rows are important when the runtime hotspot is a storage/kernel
            # boundary and therefore has no PostgreSQL definition of its own.
            operator_specs: list[tuple[str, str, list[str]]] = []
            if "work_mem" in causal_knobs:
                if has_hash_operator:
                    operator_specs.append(
                        (
                            "work_mem",
                            "src/backend/executor/nodeHash.c",
                            ["ExecChooseHashTableSize", "ExecParallelHashIncreaseNumBatches"],
                        )
                    )
                if has_sort_operator:
                    operator_specs.append(
                        (
                            "work_mem",
                            "src/backend/executor/nodeSort.c",
                            ["ExecSort"],
                        )
                    )
                    operator_specs.append(
                        (
                            "work_mem",
                            "src/backend/utils/sort/tuplesort.c",
                            ["tuplesort_begin_common", "tuplesort_begin_heap"],
                        )
                    )
                if has_aggregate_operator:
                    operator_specs.append(
                        (
                            "work_mem",
                            "src/backend/executor/nodeAgg.c",
                            ["initialize_aggregate", "initialize_phase", "ExecAgg"],
                        )
                    )
            if "max_parallel_workers_per_gather" in causal_knobs:
                operator_specs.append(
                    (
                        "max_parallel_workers_per_gather",
                        "src/backend/optimizer/path/allpaths.c",
                        ["create_plain_partial_paths", "create_partial_bitmap_paths"],
                    )
                )
                operator_specs.append(
                    (
                        "max_parallel_workers_per_gather",
                        "src/backend/optimizer/plan/planner.c",
                        ["standard_planner"],
                    )
                )
            direct_operator_rows: list[dict[str, Any]] = []
            for knob, file_path, function_names in operator_specs:
                direct_operator_rows.extend(
                    db.fetch_all(
                        """
                        SELECT id, source_root, knob, function_name, file_path,
                               line_number, relation_type, ir_symbol, evidence,
                               source_digest, analyzed_at
                        FROM keeninsight.source_analyses
                        WHERE relation_type IN ('knob_source_reference', 'llvm_data_flow')
                          AND knob = %s AND file_path = %s
                          AND function_name = ANY(%s)
                        ORDER BY CASE WHEN relation_type = 'knob_source_reference' THEN 0 ELSE 1 END,
                                 line_number LIMIT 40
                        """,
                        (knob, file_path, function_names),
                    )
                )

            source_rows: list[dict[str, Any]] = []
            seen_source_ids: set[int] = set()
            domain_terms = {
                "work_mem": ("hash", "sort", "tuple", "spill", "buf_file"),
                "max_parallel_workers_per_gather": ("parallel", "partial", "planner"),
            }
            runtime_anchor_names = {
                "BufFileWrite",
                "vfs_write",
                "LogicalTapeWrite",
                "tuplesort_begin_heap",
                "ExecHashJoinSaveTuple",
            }
            for row in [*direct_operator_rows, *runtime_source_rows]:
                knob = str(row.get("knob") or "")
                if knob not in causal_knobs:
                    continue
                evidence_text = json.dumps(row.get("evidence") or {}, default=str).lower()
                function_name = str(row.get("function_name") or "")
                source_line = str((row.get("evidence") or {}).get("line") or "").strip()
                if source_line.startswith(("*", "/*", "//")):
                    # The source scan records comments as provenance too, but
                    # comments are not executable parameter use sites and
                    # should not justify an action.
                    continue
                is_direct_operator = any(
                    row.get("file_path") == file_path and function_name in function_names
                    for _, file_path, function_names in operator_specs
                )
                is_runtime_anchor = (
                    knob == "work_mem"
                    and function_name in runtime_anchor_names
                    and function_name in functions
                    and any(
                    term in evidence_text for term in domain_terms.get(knob, ())
                    )
                )
                if not is_direct_operator and not is_runtime_anchor:
                    continue
                row_id = int(row["id"]) if row.get("id") is not None else hash(
                    (knob, function_name, row.get("file_path"), row.get("line_number"))
                )
                if row_id in seen_source_ids:
                    continue
                seen_source_ids.add(row_id)
                source_rows.append(row)
            knobs = []
            for row in source_rows:
                knob = row.get("knob")
                if knob and knob not in knobs:
                    knobs.append(knob)
            code_knowledge = source_analyzer.retrieve_code_knowledge(
                knobs=knobs or causal_knobs,
                functions=functions,
                limit=50,
                persist=True,
            )
            code_knowledge_by_knob = {
                str(row.get("knob")): row
                for row in (code_knowledge.get("records") or [])
            }
            settings_map = current_settings_map(db)
            actions = []
            unsupported = []
            for knob in knobs:
                setting = settings_map.get(knob) or {}
                if knob == "work_mem":
                    try:
                        current_kb = float(setting.get("setting") or 0)
                        unit = str(setting.get("unit") or "kB").lower()
                        if unit == "mb":
                            current_kb *= 1024
                        elif unit == "gb":
                            current_kb *= 1024 * 1024
                        target_mb = max(8, int(math.ceil(max(1.0, current_kb) * 2 / 1024.0)))
                        target_mb = min(256, target_mb)
                        target_value = "%dMB" % target_mb
                    except (TypeError, ValueError):
                        unsupported.append({"knob": knob, "reason": "could not parse live PostgreSQL setting"})
                        continue
                elif knob == "max_parallel_workers_per_gather":
                    try:
                        target_value = str(min(8, max(1, int(setting.get("setting") or 0) + 1)))
                    except (TypeError, ValueError):
                        unsupported.append({"knob": knob, "reason": "could not parse live PostgreSQL setting"})
                        continue
                else:
                    unsupported.append({"knob": knob, "reason": "source evidence found the knob but this deployment's persistent action allowlist does not expose it"})
                    continue
                relevant_source = [row for row in source_rows if row.get("knob") == knob]
                proposal = {
                    "action_type": "set_database_parameter",
                    "target": {"parameter": knob, "value": target_value},
                    "description": "Increase %s based on a measured eBPF hotspot and LLVM knob-propagation evidence; validate with the same workload." % knob,
                    "source": {
                        "type": "function_differential_plus_llvm",
                        "baseline_session_id": request.baseline_session_id,
                        "degraded_session_id": request.degraded_session_id,
                        "hotspot_functions": functions,
                        "workload_run_id": (workload or {}).get("id"),
                        "workload_scenario": scenario,
                    },
                    "proposal_evidence": {
                        "hotspot_functions": functions,
                        "source_records": relevant_source[:20],
                        "code_knowledge": code_knowledge_by_knob.get(knob),
                        "current_setting": setting,
                        "target_value": target_value,
                        "workload_context": {
                            "run_id": (workload or {}).get("id"),
                            "scenario": scenario,
                            "plan_summary": plan_summary,
                            "has_temp_signal": has_temp_signal,
                            "has_parallel_signal": has_parallel_signal,
                        },
                    },
                }
                actions.append(create_action(db, proposal))
            record_audit(
                db,
                "system_tuning_recommendation_created",
                "differential_profile",
                (profile.get("profile") or {}).get("id"),
                {"hotspot_functions": functions, "knobs": knobs, "action_count": len(actions)},
            )
            return jsonable(
                {
                    "profile": profile,
                    "hotspot_functions": functions,
                    "source_evidence": source_rows,
                    "code_knowledge": code_knowledge,
                    "knobs": knobs,
                    "workload_context": {
                        "run_id": (workload or {}).get("id"),
                        "scenario": scenario,
                        "plan_summary": plan_summary,
                        "has_temp_signal": has_temp_signal,
                        "has_parallel_signal": has_parallel_signal,
                    },
                    "actions": actions,
                    "unsupported_knobs": unsupported,
                    "requires_explicit_review": True,
                }
            )
        except (ModelingError, ActionValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/ebpf/start")
    def ebpf_start(request: EbpfStartRequest) -> Dict[str, Any]:
        try:
            result = ebpf_monitor.start(
                binary_path=request.binary_path,
                functions=request.functions or None,
                target_pids=request.target_pids or None,
                adaptive=request.adaptive,
                max_event_rate=request.max_event_rate,
            )
            record_audit(
                db,
                "ebpf_session_started",
                "probe_session",
                result.get("session_id"),
                {"selected_probes": result.get("selected_probes"), "transport": result.get("transport")},
            )
            return jsonable(result)
        except (EBPFUnavailable, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/ebpf/stop")
    def ebpf_stop() -> Dict[str, Any]:
        result = ebpf_monitor.stop()
        record_audit(
            db,
            "ebpf_session_stopped",
            "probe_session",
            result.get("session_id"),
            {"event_count": result.get("event_count"), "lost_count": result.get("lost_count")},
        )
        return jsonable(result)

    @app.post("/api/ebpf/adapt")
    def ebpf_adapt(request: EbpfAdaptRequest) -> Dict[str, Any]:
        try:
            selection = train_lasso_probe_selector(
                db,
                min_sessions=request.min_sessions,
                max_probes=request.max_probes,
                alpha=request.alpha,
                max_event_rate=request.max_event_rate,
            )
            monitor = ebpf_monitor.adapt(
                selection["selected_probes"],
                max_event_rate=request.max_event_rate,
            )
            selection["monitor"] = monitor
            record_audit(
                db,
                "ebpf_probe_set_adapted",
                "probe_session",
                monitor.get("session_id"),
                {
                    "method": selection.get("method"),
                    "sample_count": selection.get("sample_count"),
                    "selected_probes": selection.get("selected_probes"),
                    "selection_budget": selection.get("selection_budget"),
                },
            )
            return jsonable(selection)
        except (ProbeSelectionError, EBPFUnavailable, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/models/status")
    def models_status() -> Dict[str, Any]:
        return jsonable(model_manager.status())

    @app.get("/api/source-analysis/status")
    def source_analysis_status() -> Dict[str, Any]:
        return jsonable(source_analyzer.status())

    @app.post("/api/source-analysis/run")
    def run_source_analysis(request: SourceAnalysisRequest) -> Dict[str, Any]:
        try:
            result = source_analyzer.run(request.knobs or None, max_files=request.max_files)
            result["code_knowledge"] = source_analyzer.retrieve_code_knowledge(
                knobs=request.knobs or result.get("knobs") or None,
                functions=request.functions or None,
                limit=50,
                persist=True,
            )
            record_audit(
                db,
                "source_analysis_completed",
                "source_analysis",
                None,
                {
                    "source_root": result.get("source_root"),
                    "compiled_count": result.get("compiled_count"),
                    "persisted_reference_count": result.get("persisted_reference_count"),
                    "code_knowledge_records": len(
                        (result.get("code_knowledge") or {}).get("records") or []
                    ),
                },
            )
            return jsonable(result)
        except LLVMAnalysisError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/source-analysis/knowledge")
    def source_analysis_knowledge(request: SourceKnowledgeRequest) -> Dict[str, Any]:
        """Expose the code-retrieval/summary stage independently for review."""
        try:
            result = source_analyzer.retrieve_code_knowledge(
                knobs=request.knobs or None,
                functions=request.functions or None,
                limit=request.limit,
                persist=True,
            )
            record_audit(
                db,
                "code_knowledge_retrieved",
                "code_knowledge",
                None,
                {
                    "knobs": result.get("requested_knobs"),
                    "functions": result.get("requested_functions"),
                    "hypothesis_count": len(result.get("hypotheses") or []),
                    "record_count": len(result.get("records") or []),
                },
            )
            return jsonable(result)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"code knowledge retrieval failed: {exc}") from exc

    @app.get("/api/experiences/status")
    def experiences_status() -> Dict[str, Any]:
        return jsonable(experience_miner.status())

    @app.post("/api/experiences/mine")
    def experiences_mine(max_clusters: int = Query(default=8, ge=1, le=20)) -> Dict[str, Any]:
        try:
            result = experience_miner.mine(max_clusters=max_clusters)
            record_audit(db, "tuning_experiences_mined", "tuning_experience", None, {"cluster_count": result.get("cluster_count"), "feedback_count": result.get("feedback_count")})
            return jsonable(result)
        except ExperienceMiningError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/experiences")
    def experiences(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        return {"items": jsonable_rows(experience_miner.list(limit))}

    @app.get("/api/source-analysis")
    def source_analysis(
        knob: Optional[str] = Query(default=None, max_length=120),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> Dict[str, Any]:
        if knob:
            rows = db.fetch_all(
                """
                SELECT id, source_root, knob, function_name, file_path,
                       line_number, relation_type, ir_symbol, evidence,
                       source_digest, analyzed_at
                FROM keeninsight.source_analyses WHERE knob = %s
                ORDER BY analyzed_at DESC LIMIT %s
                """,
                (knob, limit),
            )
        else:
            rows = db.fetch_all(
                """
                SELECT id, source_root, knob, function_name, file_path,
                       line_number, relation_type, ir_symbol, evidence,
                       source_digest, analyzed_at
                FROM keeninsight.source_analyses
                ORDER BY analyzed_at DESC LIMIT %s
                """,
                (limit,),
            )
        return {"items": jsonable_rows(rows)}

    @app.post("/api/models/train")
    def train_model(request: ModelTrainRequest) -> Dict[str, Any]:
        try:
            result = model_manager.train(request.model_type, request.limit)
            record_audit(
                db,
                "model_trained",
                "model_run",
                (result.get("run") or {}).get("id"),
                {"model_type": request.model_type, "metrics": result.get("metrics")},
            )
            return jsonable(result)
        except ModelingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/models/predict")
    def predict_model(request: ModelPredictRequest) -> Dict[str, Any]:
        try:
            result = model_manager.predict(request.query, model_type=request.model_type)
            record_audit(
                db,
                "model_prediction_created",
                "model_prediction",
                (result.get("prediction_record") or {}).get("id"),
                {"model_type": request.model_type, "query": request.query},
            )
            return jsonable(result)
        except (ModelingError, UnsafeQuery) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/models/screen")
    def screen_model(request: ModelScreenRequest) -> Dict[str, Any]:
        try:
            result = model_manager.screen(request.query, threshold_ms=request.threshold_ms)
            record_audit(
                db,
                "model_screen_created",
                "model_prediction",
                result.get("prediction_record_id"),
                {"query": request.query, "threshold_ms": request.threshold_ms},
            )
            return jsonable(result)
        except (ModelingError, UnsafeQuery) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/models/attributions")
    def model_attributions(limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
        return {"items": jsonable_rows(model_manager.attributions(limit))}

    @app.post("/api/profiling/differential")
    def differential_profile(request: DifferentialProfileRequest) -> Dict[str, Any]:
        try:
            result = model_manager.differential_profile(
                request.baseline_session_id, request.degraded_session_id
            )
            record_audit(
                db,
                "differential_profile_created",
                "differential_profile",
                (result.get("profile") or {}).get("id"),
                {
                    "baseline_session_id": request.baseline_session_id,
                    "degraded_session_id": request.degraded_session_id,
                },
            )
            return jsonable(result)
        except ModelingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/profiling/differential")
    def differential_profiles(limit: int = Query(default=50, ge=1, le=200)) -> Dict[str, Any]:
        return {
            "items": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, baseline_session_id, degraded_session_id, method,
                           features, created_at
                    FROM keeninsight.differential_profiles
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
            )
        }

    @app.get("/api/actions")
    def get_actions(
        status: Optional[str] = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Dict[str, Any]:
        return {"items": jsonable_rows(list_actions(db, status=status, limit=limit))}

    @app.post("/api/actions/propose")
    def propose_action(request: ActionProposalRequest) -> Dict[str, Any]:
        try:
            row = create_action(
                db,
                {
                    "action_type": request.action_type,
                    "target": request.target,
                    "description": request.description,
                },
                incident_id=request.incident_id,
            )
            return jsonable(row)
        except ActionValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/actions/{action_id}/feedback")
    def action_feedback(action_id: int, request: FeedbackRequest) -> Dict[str, Any]:
        try:
            result = record_action_feedback(
                db,
                action_id,
                request.baseline_run_id,
                request.after_run_id,
            )
            current_action = get_action(db, action_id) or result.get("action") or {}
            proposal_evidence = (current_action.get("validation") or {}).get(
                "proposal_evidence"
            ) or {}
            code_record = proposal_evidence.get("code_knowledge") or {}
            code_record_ids = []
            if code_record.get("id") is not None:
                code_record_ids.append(int(code_record["id"]))
            if code_record_ids:
                outcome = ((result.get("feedback") or {}).get("outcome") or {})
                code_validation = {
                    "method": "same recorded SQL and parameters before/after typed deployment",
                    "baseline_run_id": request.baseline_run_id,
                    "after_run_id": request.after_run_id,
                    "effective": bool(outcome.get("effective")),
                    "observed_effect": outcome.get("observed_effect"),
                    "median_improvement_pct": outcome.get("median_improvement_pct"),
                    "p95_improvement_pct": outcome.get("p95_improvement_pct"),
                    "plan_change": outcome.get("plan_change"),
                    "result_preserved": bool(
                        (outcome.get("measurement") or {}).get("result_comparable")
                    ),
                }
                validated = source_analyzer.validate_code_knowledge(
                    code_record_ids, code_validation
                )
                if validated:
                    validation = dict(current_action.get("validation") or {})
                    validation["code_knowledge_validation"] = validated
                    db.execute(
                        "UPDATE keeninsight.tuning_actions SET validation = %s::jsonb WHERE id = %s",
                        (json.dumps(validation, default=str), action_id),
                    )
                    result["code_knowledge_validation"] = validated
            return jsonable(result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FeedbackError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"feedback failed: {exc}") from exc

    @app.get("/api/actions/{action_id}/feedback")
    def get_action_feedback(
        action_id: int, limit: int = Query(default=50, ge=1, le=200)
    ) -> Dict[str, Any]:
        if not get_action(db, action_id):
            raise HTTPException(status_code=404, detail="action not found")
        return {"items": jsonable_rows(list_action_feedback(db, action_id, limit=limit))}

    @app.post("/api/actions/{action_id}/apply")
    def apply(action_id: int) -> Dict[str, Any]:
        try:
            return jsonable(apply_action(db, action_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"action failed: {exc}") from exc

    @app.post("/api/actions/{action_id}/rollback")
    def rollback(action_id: int) -> Dict[str, Any]:
        try:
            return jsonable(rollback_action(db, action_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"rollback failed: {exc}") from exc

    @app.post("/api/actions/{action_id}/reject")
    def reject(action_id: int) -> Dict[str, Any]:
        try:
            return jsonable(reject_action(db, action_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/workloads")
    def get_workloads() -> Dict[str, Any]:
        return {"items": jsonable_rows(workloads.scenarios()), "state": jsonable(workloads.demo_state())}

    @app.get("/api/normal-loads")
    def get_normal_loads() -> Dict[str, Any]:
        return {
            "items": jsonable_rows(workloads.normal_loads()),
            "active": jsonable(workloads.normal_load_status()),
        }

    @app.post("/api/normal-loads/start")
    def start_normal_load(request: NormalLoadRequest) -> Dict[str, Any]:
        try:
            return jsonable(
                workloads.start_normal_load(
                    request.name,
                    request.rate_per_second,
                )
            )
        except WorkloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"normal load failed to start: {exc}") from exc

    @app.post("/api/normal-loads/stop")
    def stop_normal_load() -> Dict[str, Any]:
        try:
            return jsonable(workloads.stop_normal_load())
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"normal load failed to stop: {exc}") from exc

    @app.post("/api/demo/bootstrap")
    def bootstrap_demo() -> Dict[str, Any]:
        try:
            return jsonable(workloads.bootstrap())
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"demo bootstrap failed: {exc}") from exc

    @app.post("/api/demo/clean")
    def clean_demo_state() -> Dict[str, Any]:
        """Restore only mutable state created by the local workload demo."""
        before = workloads.demo_state()
        lock_run_ids = workloads.active_lock_run_ids()
        workloads.stop_all()
        stats_reset = workloads.reset_demo_mutations()
        rollback_results: List[Dict[str, Any]] = []
        rollback_errors: List[Dict[str, Any]] = []
        for action in list_actions(db, status="applied", limit=1000):
            source = (action.get("validation") or {}).get("proposal_source") or {}
            if source.get("type") != "workload_run":
                continue
            try:
                rollback_results.append(rollback_action(db, int(action["id"])))
            except Exception as exc:
                rollback_errors.append(
                    {"action_id": action.get("id"), "error": str(exc)}
                )
        recovered_runs = workloads.recover_stale_runs()
        after = workloads.demo_state()
        lock_rows = locks(db)
        payload = {
            "stopped_lock_runs": lock_run_ids,
            "stats_probe_reset": stats_reset,
            "rolled_back_actions": [row.get("id") for row in rollback_results],
            "rollback_errors": rollback_errors,
            "recovered_stale_runs": recovered_runs,
            "before": before,
            "after": after,
            "active_locks_after": lock_rows,
            "note": "Historical workload runs, feedback, incidents, and audit events were preserved.",
        }
        record_audit(db, "demo_state_cleaned", "demo", None, payload)
        return jsonable(payload)

    @app.post("/api/workloads/{scenario}/runs/start")
    def start_workload(scenario: str, request: WorkloadRequest) -> Dict[str, Any]:
        """Start a real PostgreSQL workload and return before its samples finish."""
        try:
            return jsonable(
                workloads.start_async(
                    scenario,
                    request.phase,
                    request.repetitions,
                    request.parameters,
                    request.warmup_repetitions,
                )
            )
        except (KeyError, WorkloadError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"workload failed to start: {exc}") from exc

    @app.post("/api/workloads/{scenario}/runs")
    def run_workload(scenario: str, request: WorkloadRequest) -> Dict[str, Any]:
        try:
            return jsonable(
                workloads.run(
                    scenario,
                    request.phase,
                    request.repetitions,
                    request.parameters,
                    request.warmup_repetitions,
                )
            )
        except (KeyError, WorkloadError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"workload failed: {exc}") from exc

    @app.get("/api/workload-runs/{run_id}")
    def get_workload_run(run_id: int) -> Dict[str, Any]:
        row = workloads.get_run(run_id)
        if not row:
            raise HTTPException(status_code=404, detail="workload run not found")
        payload = dict(row)
        payload["run_id"] = payload["id"]
        return jsonable(payload)

    @app.post("/api/workload-runs/{run_id}/propose")
    def propose_workload_action(run_id: int) -> Dict[str, Any]:
        try:
            return jsonable(workloads.propose_action(run_id))
        except (KeyError, WorkloadError, ActionValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/workload-runs/{run_id}/auto-resolve")
    def auto_resolve_workload(
        run_id: int, request: AutoResolveRequest
    ) -> Dict[str, Any]:
        """Execute the evidence-backed workload resolution loop.

        The source run determines the scenario, SQL, and measurement settings;
        callers cannot replace them with a hand-crafted after query.  The
        orchestration therefore exercises the same typed proposal, PostgreSQL
        apply/verification, identical after-phase workload, semantic feedback,
        and rollback primitives exposed by the manual UI.
        """
        source_run = db.fetch_one(
            """
            SELECT id, scenario, phase, status, parameters, observations,
                   started_at, finished_at
            FROM keeninsight.workload_runs WHERE id = %s
            """,
            (run_id,),
        )
        if not source_run:
            raise HTTPException(status_code=404, detail="workload run not found")
        if source_run["status"] != "completed" or source_run["phase"] != "baseline":
            raise HTTPException(
                status_code=400,
                detail="automatic resolution requires a completed baseline workload run",
            )
        source_parameters = dict(source_run.get("parameters") or {})
        try:
            repetitions = int(source_parameters.pop("repetitions"))
            warmup_repetitions = int(source_parameters.pop("warmup_repetitions"))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="baseline run does not retain valid measurement parameters",
            ) from exc
        if repetitions < 3:
            raise HTTPException(
                status_code=400,
                detail="automatic feedback requires at least three baseline repetitions",
            )

        action: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        feedback: dict[str, Any] | None = None
        rollback_result: dict[str, Any] | None = None
        try:
            # This reads only the proposal produced by the real workload plan
            # and validates it again against pg_settings/pg_catalog.
            action = workloads.propose_action(run_id)
            baseline_observations = source_run.get("observations") or {}
            rule_causes, rule_evidence, _ = diagnose_plan(
                db, baseline_observations.get("plan") or {}
            )
            retrieved_knowledge = rag_search(
                db, str(baseline_observations.get("query") or source_run["scenario"]), top_k=5
            )
            action_target = action.get("target") or {}
            action_parameter = action_target.get("parameter")
            source_evidence = []
            if action_parameter:
                source_evidence = db.fetch_all(
                    """
                    SELECT id, knob, function_name, file_path, line_number,
                           relation_type, ir_symbol, evidence, source_digest
                    FROM (
                        SELECT sa.*,
                               row_number() OVER (
                                   PARTITION BY relation_type
                                   ORDER BY analyzed_at DESC, id DESC
                               ) AS relation_rank
                        FROM keeninsight.source_analyses AS sa
                        WHERE knob = %s
                          AND relation_type IN (
                              'knob_source_reference', 'llvm_knob_propagation',
                              'llvm_data_flow'
                          )
                    ) AS ranked
                    WHERE relation_rank <= 10
                    ORDER BY CASE WHEN relation_type = 'knob_source_reference' THEN 0
                                  WHEN relation_type = 'llvm_data_flow' THEN 1
                                  ELSE 2 END,
                             relation_rank
                    """,
                    (action_parameter,),
                )
            code_knowledge = source_analyzer.retrieve_code_knowledge(
                knobs=[str(action_parameter)] if action_parameter else None,
                limit=50,
                persist=True,
            )
            analysis_evidence = {
                "rule_causes": rule_causes,
                "rule_evidence": rule_evidence,
                "retrieved_knowledge": retrieved_knowledge,
                "source_evidence": source_evidence,
                "code_knowledge": code_knowledge,
                "method": "EXPLAIN plan rules + grounded RAG + compiler-backed LLVM source evidence",
            }
            action_validation = dict(action.get("validation") or {})
            action_validation["execution_mode"] = "automatic_closed_loop"
            action_validation["automatic_resolution"] = {
                "baseline_run_id": run_id,
                "same_sql_after_phase": True,
                "rollback_after_success": request.rollback_after_success,
            }
            action_validation["analysis_evidence"] = analysis_evidence
            db.execute(
                "UPDATE keeninsight.tuning_actions SET validation = %s::jsonb WHERE id = %s",
                (json.dumps(action_validation, default=str), action["id"]),
            )
            action = get_action(db, int(action["id"])) or action
            action = apply_action(db, int(action["id"]))

            try:
                # The endpoint deliberately reuses the recorded scenario and
                # parameters. WorkloadRunner then opens a fresh PostgreSQL
                # session, so a database-level deployment is actually tested.
                after = workloads.run(
                    source_run["scenario"],
                    "after",
                    repetitions=repetitions,
                    parameters=source_parameters,
                    warmup_repetitions=warmup_repetitions,
                )
            except Exception:
                # A failed verification run must not leave a persistent tuning
                # action applied. ANALYZE is completed maintenance and has no
                # inverse; all reversible action types are restored here.
                current = get_action(db, int(action["id"])) or action
                if current.get("status") == "applied":
                    try:
                        rollback_result = rollback_action(db, int(action["id"]))
                    except Exception as rollback_exc:
                        raise RuntimeError(
                            "after-phase failed and automatic rollback failed: %s"
                            % rollback_exc
                        ) from rollback_exc
                raise

            feedback = record_action_feedback(db, int(action["id"]), run_id, int(after["run_id"]))
            outcome = ((feedback.get("feedback") or {}).get("outcome") or {})
            targeted_experiment = {
                "method": "same recorded SQL and parameters before/after typed deployment",
                "baseline_run_id": run_id,
                "after_run_id": int(after["run_id"]),
                "effective": bool(outcome.get("effective")),
                "observed_effect": outcome.get("observed_effect"),
                "median_improvement_pct": outcome.get("median_improvement_pct"),
                "p95_improvement_pct": outcome.get("p95_improvement_pct"),
                "plan_change": outcome.get("plan_change"),
                "result_preserved": bool(
                    (outcome.get("measurement") or {}).get("result_comparable")
                ),
            }
            validated_code_records = source_analyzer.validate_code_knowledge(
                [int(row["id"]) for row in (code_knowledge.get("records") or []) if row.get("id") is not None],
                targeted_experiment,
            )
            analysis_evidence["targeted_experiment"] = targeted_experiment
            analysis_evidence["validated_code_knowledge"] = validated_code_records
            effective = bool(outcome.get("effective"))
            current = get_action(db, int(action["id"])) or action
            should_restore = (not effective) or request.rollback_after_success
            if should_restore and current.get("status") == "applied":
                rollback_result = rollback_action(db, int(action["id"]))
            action = get_action(db, int(action["id"])) or current
            final_validation = dict(action.get("validation") or {})
            final_validation["analysis_evidence"] = analysis_evidence
            db.execute(
                "UPDATE keeninsight.tuning_actions SET validation = %s::jsonb WHERE id = %s",
                (json.dumps(final_validation, default=str), int(action["id"])),
            )
            action = get_action(db, int(action["id"])) or action

            resolution = {
                "mode": "automatic_closed_loop",
                "action_id": int(action["id"]),
                "baseline_run_id": run_id,
                "after_run_id": int(after["run_id"]),
                "effective": effective,
                "deployed": action.get("status") in {"applied", "completed"},
                "rollback_performed": rollback_result is not None,
                "rollback_after_success": request.rollback_after_success,
            }
            for target_run_id in (run_id, int(after["run_id"])):
                db.execute(
                    """
                    UPDATE keeninsight.workload_runs
                    SET observations = observations || %s::jsonb
                    WHERE id = %s
                    """,
                    (json.dumps({"automatic_resolution": resolution}, default=str), target_run_id),
                )
            record_audit(
                db,
                "automatic_resolution_completed",
                "workload_run",
                run_id,
                {
                    **resolution,
                    "observed_effect": outcome.get("observed_effect"),
                    "median_improvement_pct": outcome.get("median_improvement_pct"),
                    "p95_improvement_pct": outcome.get("p95_improvement_pct"),
                },
            )
            return jsonable(
                {
                    "mode": "automatic_closed_loop",
                    "baseline": source_run,
                    "action": action,
                    "after": after,
                    "feedback": feedback,
                    "rollback": rollback_result,
                    "analysis": analysis_evidence,
                    "resolution": resolution,
                }
            )
        except (KeyError, WorkloadError, ActionValidationError, FeedbackError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"automatic resolution failed: {exc}") from exc

    @app.post("/api/workload-runs/{run_id}/diagnose")
    def diagnose_workload_run(run_id: int) -> Dict[str, Any]:
        row = db.fetch_one(
            """
            SELECT id, scenario, phase, status, observations
            FROM keeninsight.workload_runs WHERE id = %s
            """,
            (run_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="workload run not found")
        observations = row.get("observations") or {}
        plan = observations.get("plan")
        if not isinstance(plan, dict) or not plan.get("nodes"):
            raise HTTPException(
                status_code=400,
                detail="this workload run has no explainable PostgreSQL plan",
            )
        causes, evidence, actions = diagnose_plan(db, plan)
        scenario = row["scenario"]
        scenario_causes = list(causes)
        scenario_evidence = list(evidence)
        scenario_actions = list(actions)
        if scenario in {
            "hash_join_spill",
            "aggregate_spill",
            "sort_spill",
        }:
            scenario_causes = [scenario]
            from .workload import _work_mem_action_from_plan

            preferred = _work_mem_action_from_plan(
                db, plan, prefer_sort=scenario == "sort_spill"
            )
            if preferred:
                scenario_actions = [preferred]
        elif scenario == "parallel_query_underprovisioned":
            if scenario not in scenario_causes:
                scenario_causes.insert(0, scenario)
            worker_nodes = [
                {
                    "node_type": node.get("Node Type"),
                    "workers_planned": node.get("Workers Planned"),
                    "workers_launched": node.get("Workers Launched"),
                }
                for node in plan.get("nodes") or []
                if node.get("Workers Planned") is not None
                or node.get("Workers Launched") is not None
            ]
            scenario_evidence.insert(
                0,
                {
                    "source": "EXPLAIN ANALYZE + pg_settings",
                    "observed": {
                        "workers": worker_nodes,
                        "max_parallel_workers_per_gather": (
                            observations.get("phase_parallel_workers")
                        ),
                    },
                    "interpretation": (
                        "The baseline plan did not launch parallel workers; the "
                        "proposal must be checked against live PostgreSQL limits."
                    ),
                },
            )
            proposal = observations.get("action_proposal")
            if proposal:
                scenario_actions = [proposal]
            else:
                worker_action = parallel_workers_action(db, plan=plan)
                if worker_action:
                    scenario_actions = [worker_action]
        elif scenario == "stale_statistics":
            scenario_causes = ["stale_statistics"]
            statistics_measurement = observations.get("statistics_measurement") or {}
            scenario_evidence.insert(
                0,
                {
                    "source": "EXPLAIN ANALYZE + pg_stat_user_tables",
                    "observed": statistics_measurement,
                    "interpretation": (
                        "The predicate's estimated row count diverged materially from "
                        "the actual count after a data distribution shift; refresh "
                        "statistics before attributing the plan to an index or query rewrite."
                    ),
                },
            )
            proposal = observations.get("action_proposal")
            if proposal:
                scenario_actions = [proposal]
        proposal = observations.get("action_proposal")
        if proposal and not scenario_actions:
            scenario_actions = [proposal]
        rule_root_causes = list(dict.fromkeys(scenario_causes))
        model_result = None
        model_error = None
        try:
            model_result = model_manager.predict(
                str(observations.get("query") or ""), model_type="root_cause"
            )
        except (ModelingError, UnsafeQuery) as exc:
            model_error = str(exc)
        model_prediction = (model_result or {}).get("prediction") or {}
        model_root_cause = model_prediction.get("root_cause")
        root_causes = list(rule_root_causes)
        if model_root_cause and model_root_cause not in root_causes:
            # Keep the model hypothesis visible to the caller and to RAG even
            # when the deterministic plan rule chose a different primary
            # cause.  Actions remain tied to verified PostgreSQL evidence.
            root_causes.append(str(model_root_cause))
        if model_result:
            scenario_evidence.insert(
                0,
                {
                    "source": "XGBoost root-cause classifier + TreeSHAP",
                    "prediction": model_prediction,
                    "top_attributions": (model_result.get("attribution") or [])[:8],
                    "interpretation": "Model output is fused as a ranked hypothesis with the deterministic plan evidence; it does not authorize an unverified action.",
                },
            )
        fusion = {
            "rule_causes": rule_root_causes,
            "model_top_cause": model_root_cause,
            "agreement": bool(model_root_cause and model_root_cause in rule_root_causes),
            "decision": (
                "model_corroborates_rule"
                if model_root_cause and model_root_cause in rule_root_causes
                else "model_adds_ranked_hypothesis"
                if model_root_cause and rule_root_causes
                else "model_only"
                if model_root_cause
                else "rules_only"
            ),
        }
        if model_error:
            fusion["model_unavailable_reason"] = model_error
        knowledge = [
            item
            for cause in root_causes
            for item in list_knowledge(db, root_cause=cause)
        ]
        llm_analysis = grounded_explanation(
            llm_client,
            title="工作负载运行 #%s · %s" % (run_id, scenario),
            summary="根据同一条 PostgreSQL 工作负载的已持久化执行计划、计时和诊断规则生成解释。",
            root_causes=root_causes,
            evidence=scenario_evidence,
            actions=scenario_actions,
            model_hypothesis={
                "top_cause": model_root_cause,
                "fusion": fusion,
            },
        )
        diagnosis = {
            "method": "PostgreSQL evidence rules + XGBoost/TreeSHAP rule-model fusion",
            "run_id": run_id,
            "scenario": scenario,
            "root_causes": root_causes,
            "evidence": scenario_evidence,
            "actions": scenario_actions,
            "knowledge": knowledge,
            "fusion": fusion,
            "model_prediction": model_result,
        }
        if llm_analysis:
            diagnosis["llm_analysis"] = llm_analysis
        updated_observations = dict(observations)
        updated_observations["diagnosis"] = diagnosis
        db.execute(
            """
            UPDATE keeninsight.workload_runs
            SET observations = %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(updated_observations, default=str), run_id),
        )
        record_audit(
            db,
            "workload_diagnosis_completed",
            "workload_run",
            run_id,
            {
                "scenario": scenario,
                "root_causes": root_causes,
                "action_count": len(scenario_actions),
            },
        )
        return jsonable(
            {
                "run_id": run_id,
                "scenario": scenario,
                "diagnosis": diagnosis,
            }
        )

    @app.post("/api/workloads/lock_contention/start")
    def start_lock_workload() -> Dict[str, Any]:
        try:
            return jsonable(workloads.start_lock_contention())
        except (WorkloadError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/workloads/lock_contention/{run_id}/stop")
    def stop_lock_workload(run_id: int) -> Dict[str, Any]:
        try:
            return jsonable(workloads.stop_lock_contention(run_id))
        except WorkloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/workload-runs")
    def get_workload_runs(limit: int = Query(default=50, ge=1, le=200)) -> Dict[str, Any]:
        return {"items": jsonable_rows(workloads.list_runs(limit))}

    @app.get("/api/export")
    def export_evidence(limit: int = Query(default=200, ge=1, le=1000)) -> Dict[str, Any]:
        """Export the current evidence trail for a reproducible demo handoff."""
        runs = db.fetch_all(
            """
            SELECT id, scenario, phase, status, parameters, observations,
                   started_at, finished_at, error
            FROM keeninsight.workload_runs
            ORDER BY started_at ASC LIMIT %s
            """,
            (limit,),
        )
        actions = db.fetch_all(
            """
            SELECT id, incident_id, action_type, target, description, status,
                   before_state, after_state, rollback_state, validation,
                   error, proposed_at, applied_at, rolled_back_at
            FROM keeninsight.tuning_actions
            ORDER BY proposed_at ASC LIMIT %s
            """,
            (limit,),
        )
        feedback = db.fetch_all(
            """
            SELECT id, action_id, baseline_run_id, after_run_id, outcome, created_at
            FROM keeninsight.tuning_feedback
            ORDER BY created_at ASC LIMIT %s
            """,
            (limit,),
        )
        incidents = db.fetch_all(
            """
            SELECT id, signature, kind, severity, status, summary, first_seen,
                   last_seen, resolved_at, evidence, diagnosis, metadata
            FROM keeninsight.incidents
            ORDER BY first_seen ASC LIMIT %s
            """,
            (limit,),
        )
        audit_events = db.fetch_all(
            """
            SELECT id, event_type, entity_type, entity_id, payload, created_at
            FROM keeninsight.audit_events
            ORDER BY created_at ASC LIMIT %s
            """,
            (limit,),
        )
        rag_documents = db.fetch_all(
            """
            SELECT id, source_key, source_type, title, uri, content_hash,
                   metadata, created_at, updated_at
            FROM keeninsight.rag_documents
            ORDER BY updated_at ASC LIMIT %s
            """,
            (limit,),
        )
        return {
            "format": "keeninsight-postgresql-evidence-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "scope": {
                "database": server_meta(db).get("database_name"),
                "limit_per_collection": limit,
                "note": "This export contains observations returned by the connected PostgreSQL instance; it is not a dashboard fixture.",
            },
            "server": jsonable(server_meta(db)),
            "settings": jsonable(current_settings_map(db)),
            "collector": collector.status(),
            "observability": {
                "ebpf": jsonable(ebpf_monitor.status()),
                "kafka": jsonable(event_bus.status()),
                "kafka_consumer": jsonable(event_consumer.status()),
            },
            "analysis": jsonable(model_manager.status()),
            "source_analysis": jsonable(source_analyzer.status()),
            "llm": llm_client.status(),
            "experiences": jsonable(experience_miner.status()),
            "rag": jsonable(rag_database_status(db)),
            "rag_indexer": rag_indexer.status(),
            "rag_evaluation": jsonable(_rag_evaluation_report()),
            "demo_manifest": jsonable(demo_manifest_payload()),
            "demo_state": jsonable(workloads.demo_state()),
            "workload_runs": jsonable_rows(runs),
            "tuning_actions": jsonable_rows(actions),
            "tuning_feedback": jsonable_rows(feedback),
            "incidents": jsonable_rows(incidents),
            "audit_events": jsonable_rows(audit_events),
            "rag_documents": jsonable_rows(rag_documents),
            "knowledge": jsonable_rows(list_knowledge(db)),
            "locks_at_export": jsonable_rows(locks(db)),
            "probe_catalog": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, probe_kind, function_name, binary_path, symbol_offset,
                           source_file, source_line, source_digest, selected, attached,
                           lasso_coefficient, importance, metadata, created_at, updated_at
                    FROM keeninsight.probe_catalog ORDER BY id LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "ebpf_events": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, session_id, observed_at, event_type, probe_kind,
                           function_name, binary_path, pid, tid, cpu, cgroup_id,
                           cid, query_id, duration_ns, bytes, is_anomaly,
                           transport, payload
                    FROM keeninsight.ebpf_events ORDER BY observed_at ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "model_runs": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, model_type, algorithm, status, feature_names,
                           target_name, train_count, metrics, artifact_path,
                           model_digest, created_at, finished_at, error
                    FROM keeninsight.model_runs ORDER BY created_at ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "model_predictions": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, model_run_id, model_type, query, features,
                           prediction, created_at
                    FROM keeninsight.model_predictions ORDER BY created_at ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "model_attributions": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, model_run_id, prediction_id, method, feature_name,
                           feature_value, contribution, base_value, metadata, created_at
                    FROM keeninsight.model_attributions ORDER BY created_at ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "differential_profiles": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, baseline_session_id, degraded_session_id, method,
                           features, created_at
                    FROM keeninsight.differential_profiles ORDER BY created_at ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "source_analyses": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, source_root, knob, function_name, file_path,
                           line_number, relation_type, ir_symbol, evidence,
                           source_digest, analyzed_at
                    FROM keeninsight.source_analyses ORDER BY analyzed_at ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "code_knowledge": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, fingerprint, source_root, knob, target_functions,
                           retrieval_context, hypothesis, validation, confidence,
                           provenance, created_at, updated_at
                    FROM keeninsight.code_knowledge
                    ORDER BY updated_at ASC, id ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
            "tuning_experiences": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, fingerprint, cluster_id, context, root_cause, action,
                           support, success_count, confidence, provenance,
                           created_at, updated_at
                    FROM keeninsight.tuning_experiences ORDER BY updated_at ASC LIMIT %s
                    """,
                    (limit,),
                )
            ),
        }

    @app.get("/api/audit")
    def audit(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        return {
            "items": jsonable_rows(
                db.fetch_all(
                    """
                    SELECT id, event_type, entity_type, entity_id, payload, created_at
                    FROM keeninsight.audit_events ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
            )
        }

    return app


def list_actions_for_incident(db: Database, incident_id: int) -> List[Dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT id, incident_id, action_type, target, description, status,
               before_state, after_state, rollback_state, validation,
               error, proposed_at, applied_at, rolled_back_at
        FROM keeninsight.tuning_actions
        WHERE incident_id = %s ORDER BY proposed_at DESC
        """,
        (incident_id,),
    )


def _validate_rule_config(kind: str, config: Any) -> None:
    if not isinstance(config, dict):
        raise ValueError("rule config must be a JSON object")

    def number(name: str, *, minimum: float, maximum: float | None = None) -> None:
        if name not in config:
            return
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"rule config {name!r} must be a finite number")
        if value < minimum or (maximum is not None and value > maximum):
            bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
            raise ValueError(f"rule config {name!r} must be {bound}")

    if kind == "connection_saturation":
        number("ratio", minimum=0.0, maximum=1.0)
    elif kind == "lock_wait":
        number("minimum_waiting_sessions", minimum=1.0)
    elif kind == "slow_query":
        number("mean_exec_time_ms", minimum=0.0)
        number("minimum_calls", minimum=1.0)
    elif kind == "temp_spill":
        number("minimum_temp_blocks_written", minimum=1.0)
    elif kind == "cache_hit_ratio":
        number("ratio", minimum=0.0, maximum=1.0)
        number("minimum_block_reads", minimum=0.0)


app = _build_app()
