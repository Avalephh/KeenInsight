# KeenInsight for PostgreSQL

KeenInsight is an evidence-driven PostgreSQL implementation of the paper's
closed-loop monitoring, diagnosis, and tuning architecture.  It addresses the
problems identified in the previous demonstration review:

* the dashboard reads live PostgreSQL views instead of rendering mock arrays;
* workload scenarios execute real SQL and record the actual plan and timing;
* diagnoses expose the evidence that produced them;
* tuning actions are typed, validated, audited, and reversible where an inverse
  exists (maintenance refreshes are explicitly recorded as non-invertible);
* live BCC eBPF probes feed a Kafka topic and an auditable PostgreSQL outbox;
* Lasso-based probe adaptation, event-vector differential profiling, XGBoost /
  TreeSHAP diagnosis, and PostgreSQL-source LLVM CFG/SSA propagation are
  available;
* the query optimizer agent measures session-local hints and safe rewrites,
  while index changes remain explicit reviewed proposals;
* the RAG path combines collected paper/review/project sources with runtime
  evidence, compiler-backed code retrieval/summary, TF-IDF/KMeans experience
  mining, and real BGE vector retrieval;
* the demo can run locally with Docker Compose and can also connect to an
  existing PostgreSQL 12+ instance (with compatibility fallbacks where
  server-version fields differ).

Deterministic PostgreSQL rules remain visible and are fused with model
hypotheses; neither is mislabeled as the other.  A configured OpenAI-compatible
LLM can generate grounded RAG, incident/plan explanations, compiler-backed
code summaries, and additional query candidates, but the safe deterministic
path remains authoritative. Every generated candidate passes the same
allowlist, semantic, and measured verification gates.

## Quick start

Requirements: Docker Engine with Compose v2.

```bash
docker compose up --build
```

Open <http://localhost:8000>.  The root opens the supplied English demo shell
at its Cluster/Host List landing page with the original navigation and page
layout; the live adapter replaces the
illustrative rows and charts with data returned by PostgreSQL.  The
evidence-first control plane is available at <http://localhost:8000/console>.
The Compose database is PostgreSQL 16 with
`pg_stat_statements`, `compute_query_id`, and `track_io_timing` enabled.  The
demo schema is created by the database initialization script.  Use the
**Workloads** panel to run actual hash-join spill, grouped-aggregate spill,
window-sort spill, parallel-worker, lock-contention, or optional missing-index
workloads.  The three `work_mem` cases use the same safe setting action but
exercise different PostgreSQL operators and expose their actual plan changes.
The panel lets you change the measured repetition count, the selected
`customer_id`, the baseline session `work_mem`, and the baseline
parallel-worker setting; the server validates these knobs against PostgreSQL
before execution.  A separate stale-statistics case shifts a namespaced table
distribution and recovers planner estimates with a typed `ANALYZE` action.
The guided walkthrough takes one scenario through baseline,
evidence-backed diagnosis, proposal review, explicit apply, after measurement,
feedback, and rollback.  A completed baseline can alternatively be sent to the
opt-in `/api/workload-runs/{id}/auto-resolve` endpoint, which performs the
server-side typed generate → deploy → same-SQL measurement → feedback →
rollback loop and records code-knowledge validation.  The result view can show
a plan-signal diff and rerun diagnosis directly from the persisted PostgreSQL
plan.  **Export evidence**
downloads the raw run/action/feedback/audit record, while **Clean demo state**
releases demo lock sessions and rolls back only workload-originated applied
actions without deleting historical evidence.

The development measurement snapshot in `docs/MEASURED_RESULTS.md` was taken
against PostgreSQL 12.22 on an existing local instance; rerun the protocol on
the PostgreSQL 16 Compose image before using those numbers as a new result.
The strict implementation audit, including real eBPF loss/overhead checks,
model metrics, LLVM evidence, optimizer verification, and RAG evaluation, is
in `docs/ICDE_CLAIM_AUDIT.md`.

### Continuous telemetry retention

The PostgreSQL collector starts in the background and records one real
observation every `KI_COLLECT_INTERVAL_SECONDS` (5 seconds by default).  The
overview waits for the first successful observation during service startup,
then reads only persisted snapshots from the configured rolling window.  The
default window is 24 hours (`KI_SNAPSHOT_RETENTION_HOURS=24`); the collector
removes older rows at startup and periodically while it is running.  The
dashboard APIs also apply the same time boundary, so a stale row cannot appear
if cleanup is temporarily delayed.  This retention policy covers both normal
telemetry and persisted collection-error observations.

The TPS chart intentionally has two visual layers.  The thin line and points
are the unchanged interval values calculated from PostgreSQL's cumulative
`pg_stat_database.xact_commit` and `xact_rollback` counters; the thick line is
a five-point rolling median used only to make a stable operating level easy to
see.  A robust change detector marks sudden rises or drops in red and prints
the exact raw value.  If one burst is much larger than the normal range, the
axis stays readable for the baseline and the burst is clipped visually but
retained as an exact labeled point.  No raw observation is overwritten or used
to change incident detection.

### Real TPC-C-shaped demo data

The normal-load selector can use a repeatable TPC-C-shaped seed dataset in the
separate `keeninsight_tpcc` schema.  It is real PostgreSQL data, not a browser
fixture: the normal-load worker executes read-only queries against its orders,
customers, order-line, stock, and item tables.  Load it once with:

```bash
python scripts/bootstrap_tpcc.py
```

The generator chooses a conservative scale from the host's CPU, memory, and
free disk.  On a 4-vCPU/30-GiB host it selects five warehouses: 50 districts,
100,000 items, 500,000 stock rows, 150,000 customers, 150,000 orders, and
about 1.5 million order lines (roughly 542 MB including table indexes).  This
keeps interactive queries responsive while retaining the usual TPC-C row
ratios.  Use `--warehouses N` to select another bounded scale, or
`--replace` to replace only the dedicated `keeninsight_tpcc` schema.  The
dataset is explicitly a TPC-C-shaped demo seed; it is not a compliant TPC-C
benchmark implementation and its timings must not be reported as TPC-C
benchmark results.

After loading, `/api/workloads` reports the measured row counts and size, and
the Workloads page exposes three additional real loads: latest-order lookup,
customer order history, and stock availability check.  The original
`keeninsight_demo` tables and anomaly scenarios remain separate.

## Original English demo pages

The original page set is also directly available under `/static/legacy/`:
`template.html`, `host-list.html`, `realtime-monitor.html`, `alert-list.html`,
`sql-log.html`, `alert-rules.html`, `dba-rules.html`, `manual.html`,
`code-source.html`, and the detail pages.  Its HTML/CSS visual structure is
retained from `English.zip`; `legacy-runtime.js` and `legacy-pages.js` provide
the live API bindings.

The earlier Vue/Python/C++ implementation is kept as an explicit design
reference in [the upstream KeenInsight repository](https://github.com/Avalephh/KeenInsight)
and [the local upstream reference note](docs/UPSTREAM_REFERENCE.md).  The old
WorkloadTune API sequence is live through the PostgreSQL-backed compatibility
adapter at `/api/tune/*`; its branch-by-branch comparison, including the
independent `dev`, `dream`, and `db-replay` boundaries, is in
[UPSTREAM_COMPATIBILITY_AUDIT.md](docs/UPSTREAM_COMPATIBILITY_AUDIT.md).  The
adapter retains the old page fields but reports measured PostgreSQL values and
typed-action evidence, while the original English static shell remains the
browser-facing landing page.

## Grounded RAG knowledge

The **RAG knowledge** panel searches a portable, source-attributed collection
of the supplied paper, review PDF, legacy demo pages, extracted PDF design
assets, current design notes, implementation, UI, SQL, tests, deployment files,
and compact evaluation summaries.  The collection is
stored in `rag_sources/` and described by `rag_sources/manifest.json`; it is
safe to regenerate with:

```bash
python scripts/collect_sources.py \
  --output rag_sources \
  --paper-zip /root/demo/KeenInsight_VLDB_Demo.zip \
  --legacy-demo-zip /root/demo/English.zip \
  --review-pdf "/root/demo/Conference Management Toolkit - View review.pdf" \
  --project-root .
```

At runtime a low-frequency indexer also mirrors recent workload runs, tuning
actions, measured feedback, incidents, the latest telemetry snapshot, model
and optimizer records, SQL lifecycle events, source-analysis/code-knowledge
evidence, and empirical knowledge entries.  Static documents use content hashes; runtime
records use stable source keys, so repeated syncs are idempotent.  The default
interval is 60 seconds and can be changed with
`KI_RAG_SYNC_INTERVAL_SECONDS` (minimum 15 seconds).  `GET /api/rag/search`,
`POST /api/rag/ask`, and `POST /api/rag/sync` expose the lexical pipeline;
`/api/rag/search` and `/api/rag/ask` additionally support `vector` and `hybrid`
embedding modes.  The retriever uses PostgreSQL full-text search, exact-term
gating, and 384-dimensional BGE embeddings persisted in PostgreSQL.  A fixed
20-question retrieval set is kept outside `rag_sources/`;
`scripts/evaluate_rag.py` reports source Recall@1/Recall@5, MRR, precision,
term coverage, and a no-hit negative test for lexical, vector, and hybrid
modes.
Each hit also carries normalized line coordinates (and extracted PDF page
markers where available).  Without an optional
`KI_RAG_LLM_URL`/`KI_RAG_LLM_MODEL`, the answer is an extractive evidence brief;
no unsupported model-generated claim is presented.

The RAG worker does not read the demo business tables and is outside the
timed workload path.  Use `python scripts/preflight.py --require-clean` for a
fail-fast environment check, `python scripts/aa_control.py` for an identical
workload control, `scripts/ebpf_strict_smoke.py` for real event/lifecycle/Kafka
delivery, `scripts/ebpf_overhead_smoke.py` for adaptive overhead measurement,
`scripts/ebpf_auto_adapt_smoke.py` and `scripts/ebpf_long_query_smoke.py` for
online anomaly-aware monitoring, `scripts/code_knowledge_smoke.py` for
compiler-backed retrieval, `scripts/auto_resolve_smoke.py` for the automatic
closed loop, and `scripts/validate_pg16.sh` for the isolated PostgreSQL 16 Compose
validation when Docker is available.  See `docs/RAG_OPERATIONS.md`,
`docs/EBPF_OPERATIONS.md`, and `docs/PG16_VALIDATION.md` for the exact
contracts.

To stop and remove the local demo database:

```bash
docker compose down -v
```

The `-v` option removes only the named demo volume.

## Existing PostgreSQL instance

Copy `.env.example` to `.env` and set `KI_DATABASE_URL`.  The service account
needs permission to read `pg_stat_activity`, `pg_locks`, `pg_settings`, and
`pg_stat_statements`.  A monitoring-only deployment can use the PostgreSQL
`pg_read_all_stats` role.  Applying database-level settings or creating an
index requires the corresponding PostgreSQL privileges and is intentionally
rejected if the server refuses it.

`pg_stat_statements` must be loaded through `shared_preload_libraries` and the
extension must be created in the monitored database.  A restart is required
when adding it to an already-running server.  See the official PostgreSQL
documentation for the version-specific deployment details.

The bundled service is intended for an isolated local/artifact environment.
It has no authentication or multi-tenant authorization layer and should not be
published directly to an untrusted network.

## Project layout

```text
app/                 API, collector, eBPF/Kafka, models, LLVM, tuning, RAG, UI
db/init/             PostgreSQL initialization and demo data
demo/                Workload definitions and provisioning logic
scripts/             local verification helpers
rag_sources/         source-attributed static material for the RAG index
artifacts/            reproducibility reports and raw experiment outputs
tests/               unit tests for evidence parsing and guardrails
```

## Important safety properties

1. The browser submits an action ID, never executable SQL.  eBPF start/stop
   and adaptive selection are explicit operator controls.
2. Only action types implemented by `app/tuning.py` can change the database;
   the non-index actions are limited to validated database-scoped
   `work_mem` and `max_parallel_workers_per_gather` settings plus a validated
   `ANALYZE` refresh.  Query hints are session-local; rewrites are restricted
   to a provably equivalent form; index add/remove candidates are proposal-only
   until a reviewed typed action is requested.  The statistics refresh is
   recorded as completed maintenance because it has no meaningful inverse.
3. Query plans from arbitrary input are explained only after conservative
   read-only validation; `EXPLAIN ANALYZE` is only exposed through an explicit
   user action and accepts `SELECT`/read-only statements.
4. The default persistent setting action is scoped to the current database,
   not the whole cluster.  The service records the previous value before
   applying it.
5. An index rollback drops an index only when KeenInsight created it during the
   recorded action.
6. Observations are persisted in `keeninsight.snapshots`, incidents in
   `keeninsight.incidents`, eBPF events and SQL lifecycle rows in their
   dedicated tables, and all action changes in `keeninsight.audit_events`.
7. A proposed workload action can be compared with its measured before/after
   runs through `POST /api/actions/{id}/feedback`; the idempotent comparison is
   persisted in `keeninsight.tuning_feedback` and contributes to empirical
   knowledge with its run provenance.
8. `POST /api/demo/clean` is a scoped recovery operation: it stops in-memory
   demo lock sessions, rolls back applied actions explicitly sourced from a
   workload run, verifies that demo locks are gone, and preserves runs,
   feedback, incidents, and audit events.

## Research artifact direction

The service exposes the raw measurements needed for an ICDE evaluation:
collection interval and failures, database counters, query statistics, lock
waits, execution plans, workload runs, action validation, and rollback state.
The experimental scripts also export measured before/after feedback and the
resulting empirical knowledge update.  Run
`python scripts/repeat_experiment.py --runs 3 --repetitions 7 --warmups 2`
to obtain an aggregate over complete protocol repetitions; every aggregate
is calculated from the returned PostgreSQL observations.  Export these
records rather than copying numbers into the paper.  The UI export button calls
`GET /api/export?limit=1000` and returns the same evidence collections as one
JSON handoff.  `GET /api/demo/manifest` and the `demo_manifest` member of
`/api/export` capture the live version, data cardinality, scenario contract,
measurement rules, static-source digest, runtime capability counts, and
operational limits.  See
`docs/ICDE_ARTIFACT.md` for the exact evaluation contract and
`docs/PAPER_POSTGRESQL_ALIGNMENT.md` for the claim-by-claim boundary of the
PostgreSQL-native implementation.
