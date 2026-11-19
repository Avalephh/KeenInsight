> **v2 scope note (2026-11-19):** SQL-level tuning (query optimizer agent,
> hints, rewrites, index actions, SQL evidence pages) has been removed from
> this codebase.  This document records the pre-v2 artifact; the active product
> is the SysInsight system-parameter tuning path only.

# Paper-to-artifact alignment

This file describes how the paper's architecture is executed by the current
PostgreSQL artifact.  It is an implementation map, not a request to remove
the paper's eBPF, model, LLVM, agent, or RAG claims.  The original paper and
the supplied legacy demo are preserved in `rag_sources/`.

## Capability mapping

| Paper idea | PostgreSQL implementation | Evidence/API |
| --- | --- | --- |
| Event-driven fine-grained monitoring | BCC uprobes/uretprobes on PostgreSQL symbols and a `vfs_write` kprobe; kernel normal-call aggregation and anomaly-immediate events | `/api/ebpf/start`, `/api/ebpf/stop`, `artifacts/ebpf-smoke-strict.json` |
| Adaptive probe attachment | Lasso ranks measured function vectors; an empirical event-rate budget prunes high-frequency user probes before dynamic reattachment | `/api/ebpf/adapt`, `artifacts/ebpf-overhead-strict.json` |
| SQL-level lifecycle | Local PostgreSQL query fingerprint or `pg_stat_activity` query ID is joined with event timestamps and materialized by function | `/api/ebpf/timelines`, `keeninsight.sql_lifecycle_events` |
| Hierarchical diagnosis | Snapshot/plan rules screen system and SQL signals; XGBoost latency/root-cause models and TreeSHAP provide model hypotheses; `/diagnose` records rule/model agreement | `/api/models/screen`, `/api/models/predict`, `/api/workload-runs/{id}/diagnose` |
| Differential profiling | Persisted eBPF event vectors are differenced between normal/degraded sessions; function-level XGBoost/TreeSHAP is trained on measured probe/workload pairs | `/api/profiling/differential`, `model_runs`, `model_attributions` |
| System-level resolution | clang-generated LLVM CFG/SSA/data-flow plus bounded cross-translation-unit paths map observed hotspots to contextual `work_mem`/parallel actions; actions are typed and reviewed | `/api/system-tuning/recommend`, `/api/source-analysis`, `tuning_actions` |
| Code retrieval and semantic summary | Exact PostgreSQL source snippets are retrieved next to compiler rows, grouped into experiment-required knob hypotheses, and validated from a measured experiment | `/api/source-analysis/knowledge`, `code_knowledge`, `artifacts/code-knowledge-strict.json` |
| Automatic closed-loop tuning | An opt-in server endpoint reuses a completed baseline, generates a typed action, deploys it, measures the same SQL, records feedback, and rolls back | `/api/workload-runs/{id}/auto-resolve`, `artifacts/auto-resolve-strict.json` |
| SQL-level resolution | Read-only session-local hint candidates, a conservative equivalent rewrite, and reviewed index add/remove proposals are benchmarked and semantically checked | `/api/optimizer/optimize`, `artifacts/optimizer-strict-smoke.json` |
| Pairwise validation/ranking | Candidate median/p95, optimizer cost, semantic result bag, and historical pairwise LogisticRegression ranking are persisted | `optimizer_runs`, `optimizer_candidates` |
| Multi-source KB | Paper/review/legacy demo/project/official/community sources, runtime evidence, LLVM rows and measured experience are normalized into cited chunks | `/api/rag/status`, `/api/rag/ask`, `rag_sources/manifest.json` |
| Evolving tuning experience | TF-IDF/KMeans clusters and association rules with support, confidence, lift and provenance | `/api/experiences/mine`, `tuning_experiences` |
| Long-query anomaly handling | Actual PostgreSQL function duration and `pg_stat_activity.query_start` age trigger immediate event emission and lifecycle anomaly reasons | `/api/ebpf/status`, `artifacts/ebpf-long-query-strict.json` |

## PostgreSQL port of the demonstration scenarios

The original supplied screenshots contain MySQL-specific names such as
`log_buffer_flush_to_disk` and `HashJoin(off)`.  The executable keeps the
paper's monitoring → diagnosis → resolution structure but runs real
PostgreSQL mechanisms:

- hash join, grouped aggregate, and window sort spill under session-local
  `work_mem` pressure;
- underprovisioned parallel aggregate with actual worker counts;
- row-lock contention and blocker identification;
- stale planner statistics and typed `ANALYZE` maintenance;
- filtered scan/index selection as a secondary SQL action;
- session-local hints and a semantics-preserving `OR` → `IN` rewrite.

This is a genuine DBMS port, not a relabeling of the MySQL functions.  The
exact MySQL scenarios require a MySQL server/build and are not used as
PostgreSQL measurements.

## Measurement and causal controls

The baseline/after protocol executes the same SQL with the same parameters,
stores raw timed samples and `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` plans,
checks complete result equality, and requires a verified plan signal plus at
least a five-percent median and p95 improvement before calling a latency
action effective.  Database setting/index actions are reversible when an
inverse exists; statistics refresh is recorded as one-way maintenance.

`artifacts/final-regression-pg12.json` contains three repetitions for the five
tunable latency cases, all effective and rolled back.  `artifacts/aa-control-
results.json` runs identical arms without an action and checks plan/result
equality.  The statistics case and raw source/model/RAG artifacts are kept
separate so a controlled fixture result cannot be mistaken for a production
trace.

For model training, workload runs overlapping an eBPF session are excluded
from the pre-execution latency/root-cause data set.  They remain valid data for
the function-level model.  This separation is necessary because direct
high-frequency instrumentation can add measurable latency; the overhead
control reports that effect instead of hiding it.

## Runtime and deployment boundary

The PostgreSQL collector and rule diagnosis remain available without optional
services.  eBPF needs BCC, a compatible kernel, tracing privileges, and a
process namespace exposing the PostgreSQL executable.  Kafka is used when a
broker is configured; otherwise the event bus explicitly reports a database
fallback.  The vector path requires fastembed/model cache.  The optional
OpenAI-compatible LLM receives retrieved context only; deterministic typed
validation remains mandatory.  Manual persistent actions require explicit
operator review, while the opt-in automatic endpoint invokes only the same
server-side typed handlers and performs measured feedback/rollback.

PostgreSQL 12.22 is the current measured environment.  Compose targets
PostgreSQL 16, but `docs/PG16_VALIDATION.md` records `not_run` when Docker is
unavailable.  No PG12 result is relabeled as PG16.
