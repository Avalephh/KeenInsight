# Implementation notes and experimental contract

## What is real

The collector reads PostgreSQL's own statistics views.  The workload runner
executes SQL against the configured instance and obtains plans using
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`.  The UI displays values returned by
the API; it does not contain a second copy of incident or performance data.

The demo schema is only a reproducible workload fixture.  It is not used to
pre-compute the answer.  The actual access path, number of hash batches, sort
method, aggregate strategy, temporary blocks, parallel workers, execution
time, and effect of a change are read from each run.  Each workload separates
warm-up executions from timed executions, reports a timing summary, and
records an exact `pg_stat_statements` counter delta for the measured SQL.
Client latency and server-reported execution time are kept as different
measurements.

The dashboard's host metrics are read from Linux `/proc` and filesystem
interfaces in the service/container namespace.  They are real host/container
snapshots, not generated values, but they are not a substitute for the
paper's function-level eBPF signal.

## What is configurable

Alert rules are persisted in `keeninsight.alert_rules`, including thresholds,
severity, and enabled state.  The API exposes `PATCH /api/rules/{id}` so a
deployment can use workload-appropriate thresholds.  The default values are
generic starting points, not claims about all PostgreSQL installations.
The interactive workload runner also validates a selected demo `customer_id`,
session-local baseline `work_mem`, and session-local baseline
`max_parallel_workers_per_gather` against the connected database.  The
hash-join, grouped-aggregate, and window-sort scenarios share the `work_mem`
guardrail while their operator-specific plan evidence remains visible.  These
are experiment knobs, not precomputed result selectors.

After a typed action has been applied, two completed runs can be compared via
`POST /api/actions/{id}/feedback`.  The comparison uses the observed median
and p95 timings plus plan signals (for example an actual index name, hash
batches, sort method, aggregate strategy, temporary blocks, and `Workers
Launched`).  It is stored in
`keeninsight.tuning_feedback`, audited, and folded into a measured knowledge
entry with an empirical confidence aggregate.  The evaluator accepts only
the same scenario, SQL, workload knobs, and measurement parameters.  It needs
at least three timed samples on both sides, an unchanged query result, a
verified plan signal, and at least a five-percent improvement in both median
and p95 before it labels an action `effective`.

The workload result card also exposes `POST /api/workload-runs/{id}/diagnose`.
This is a read-only, deterministic re-analysis of the persisted JSON plan; it
returns the root cause, observed fields, typed proposal, and matching knowledge
provenance without executing the workload again.  `GET /api/export?limit=1000`
returns the raw evidence trail for a handoff.  `POST /api/demo/clean` is the
scoped recovery control for an interactive session: it stops demo lock runs,
rolls back only actions carrying a `workload_run` proposal source, verifies
the remaining lock rows, and keeps historical records.

Query detection uses interval deltas rather than cumulative totals.  Query
incidents are auto-resolved only when the filtered `pg_stat_statements` result
fits inside the collector's top-N window; absence from a truncated top-N view
is treated as unknown, not as recovery.

## Source collection and RAG

The repository ships a grounded research-assistant path for the paper and
demo handoff.  `scripts/collect_sources.py` turns the supplied LaTeX paper,
review PDF, legacy HTML, and current project files into UTF-8 text with a
stable `SOURCE_KEY`, source type, title, and provenance URI.  The checked-in
manifest is a portable inventory; it does not depend on the original archive
paths at service runtime.  The current collection includes the paper source,
all 12 legacy pages, the five-page review extraction, and current design,
implementation, UI, SQL, test, and deployment references.

On startup and every `KI_RAG_SYNC_INTERVAL_SECONDS` (default 60 seconds), the
RAG indexer upserts those static files—including extracted auxiliary PDF text,
archive asset inventories, and compact evaluation-data summaries—and mirrors
recent PostgreSQL evidence: workload runs, typed tuning actions, measured
feedback, incidents, the latest snapshot, and knowledge entries.  These records
are content-hashed and keyed
by their source IDs, so new experiments become searchable without re-reading
the demo tables.  `POST /api/rag/sync` is the explicit refresh control.
The runtime index keeps only the current 100 records per dynamic category (and
the latest snapshot); pruning removes derived RAG copies only, while the full
historical workload/action/feedback/incident evidence remains in its source
tables and export bundle.

The storage is two PostgreSQL tables, `keeninsight.rag_documents` and
`keeninsight.rag_chunks`, with a GIN full-text index.  Retrieval uses the
PostgreSQL `simple` configuration and `websearch_to_tsquery`, then a
parameterized `ILIKE` term fallback for unusual queries.  Results are ranked,
deduplicated to one chunk per document, and returned with `R1`, `R2`, … source
citations plus the exact retrieved chunk.  `POST /api/rag/ask` uses an
extractive cited answer by default.  A generic OpenAI-compatible chat
completion endpoint can be enabled through environment variables, but it is
optional and is constrained by a prompt requiring evidence citations.

This is a portable lexical RAG implementation, not an embedding or pgvector
claim.  It is intentionally stable on the PostgreSQL 12/16 artifact images;
adding a vector backend would be a separately measured extension.  RAG
indexing uses its own low-frequency worker and only the evidence tables, so it
does not alter the workload SQL or the timed measurement protocol.  The
preflight, A/A control, and repeated experiment reports are the required
regression evidence for that boundary.

## What the current system intentionally does not do

It does not pretend to provide eBPF function-level visibility inside
PostgreSQL.  PostgreSQL-native evidence is used instead: `pg_stat_activity`,
`pg_locks`, `pg_stat_database`, `pg_stat_statements`, `pg_settings`, and JSON
execution plans.  A future source-insight adapter can be mounted when a
matching PostgreSQL source tree is available; no source locations are invented
when it is absent.

It also does not automatically apply arbitrary SQL suggested by a language
model.  Only typed index, `work_mem`, and
`max_parallel_workers_per_gather` actions pass the server-side guardrails.

The bundled HTTP service has no authentication and is intended for a local,
isolated artifact environment.  A production deployment must add an
authenticated gateway and separate monitoring and mutation privileges.

## Experiment protocol

1. Start PostgreSQL and the API.
2. Run `scripts/run_experiment.py` with at least seven timed samples and two
   warm-ups per phase, or use `scripts/repeat_experiment.py` for three
   repetitions of the complete protocol.  Use the clean Compose volume when
   the experiment must start without historical observations.
3. Preserve the generated JSON with the PostgreSQL version, hardware, Docker
   image digest, and workload configuration.
4. Report median/p95 timing from the returned `timings_ms`, and report the
   complete plan summaries.  Do not copy a single favorable run into a paper.
5. Repeat each phase after resetting the demo database or explicitly record
   the existing indexes/settings.  The API records action and rollback state.

The UI-only walkthrough, plan diff, export, and cleanup endpoints are outside
the workload execution path.  After those additions, the same final local
regression protocol completed 3 repetitions × 5 tunable scenarios with 7 timed
samples and 2 warm-ups per phase: 15/15 comparisons were effective, 15/15
actions rolled back, and there were no protocol failures.  This is a guard
against implementation regressions, not a claim that the measured latency is
portable across machines.

The clean Compose workflow and the fields that must accompany an ICDE result
are documented in `docs/ICDE_ARTIFACT.md`.
