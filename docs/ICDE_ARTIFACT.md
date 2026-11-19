# ICDE artifact and evaluation contract

This artifact is the executable PostgreSQL port of the KeenInsight architecture.
It includes the paper's fine-grained monitoring, adaptive probe selection,
stream transport, hierarchical diagnosis, source/LLVM evidence, tuning agents,
and multi-source knowledge loop.  PostgreSQL statistics views and JSON plans
remain the database-native evidence path; BCC/Kafka, compiler tooling, and the
embedding model are explicit runtime dependencies rather than dashboard
placeholders.  The original paper source is preserved and is not narrowed by
this artifact.

## Reproduce a clean run

Use an isolated Docker volume for a clean database.  The initialization SQL
creates the namespaced demo relations and clears setup statements from
`pg_stat_statements`; it does not insert timing or tuning results.

```bash
docker compose down -v
docker compose up --build -d
./scripts/smoke.sh
python scripts/preflight.py --require-clean --output artifacts/preflight-pg12.json
python scripts/aa_control.py --repetitions 7 --warmups 2 \
  --output artifacts/aa-control-results.json
python scripts/run_experiment.py --repetitions 7 --warmups 2 --output results.json
python scripts/run_stats_case.py --repetitions 7 --warmups 2 \
  --output artifacts/stats-case.json
python scripts/evaluate_rag.py --output artifacts/rag-evaluation.json
python scripts/evaluate_rag.py --mode vector \
  --output artifacts/rag-vector-evaluation-strict.json
python scripts/evaluate_rag.py --mode hybrid \
  --output artifacts/rag-hybrid-evaluation-strict.json
python scripts/ebpf_strict_smoke.py
python scripts/ebpf_auto_adapt_smoke.py
python scripts/ebpf_long_query_smoke.py
python scripts/code_knowledge_smoke.py
python scripts/auto_resolve_smoke.py
```

The A/A report is a control, not a tuning result: it executes identical
baseline arms, applies no proposal, and stores plan/result equality and timing
differences for all five tunable scenarios.  `scripts/validate_pg16.sh` runs
the same preflight and repeated experiment in an isolated PostgreSQL 16
Compose project when Docker Engine is available; the local development
snapshot is explicitly labeled by PostgreSQL version.

The stale-statistics maintenance case is a separate non-latency comparison. It
changes only the isolated `stats_probe` distribution, exposes the estimate vs.
actual row mismatch, applies a typed `ANALYZE`, and verifies estimate/actual
agreement with the same result. The isolated probe disables autovacuum only
for this reproducibility window; the regular demo tables retain their default
maintenance behavior. Its action is intentionally `completed`, not
`applied`, because PostgreSQL planner statistics do not have a meaningful
rollback operation; `POST /api/demo/clean` restores the probe distribution and
re-analyzes it.

The script performs the following observable sequence:

1. provisions the demo relations with PostgreSQL DDL and `ANALYZE`;
2. measures the missing-index baseline with client timing and
   `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`;
3. derives a candidate from the observed sequential-scan predicate, validates
   the table/column in `pg_catalog`, applies `CREATE INDEX CONCURRENTLY`, and
   verifies the index identity;
4. measures the same query after the change, records before/after feedback,
   rolls the index back only if its recorded PostgreSQL OID and definition
   still match, and re-runs the baseline pressure arm to verify that the
   original plan signal returns;
5. repeats the process for a hash join whose baseline uses a session-local
   `work_mem` value, with the recommendation derived from the observed hash
   node and current `pg_settings` value;
6. repeats the process for a grouped aggregate under the same session-local
   memory pressure, and verifies the observed sorted/temp-spilling aggregate
   path against the after-phase in-memory path;
7. repeats the process for a window sort under low session-local `work_mem`,
   and verifies `external merge`/disk sorting versus the after-phase in-memory
   sort method;
8. repeats the process for a large aggregate whose baseline sets the session
   `max_parallel_workers_per_gather` to zero.  The proposal raises the
   database default by one only when live PostgreSQL and host limits allow it;
   the after phase verifies actual `Workers Launched` in `EXPLAIN ANALYZE`;
9. starts two real sessions for row-lock contention and waits until
   `pg_stat_activity`/`pg_blocking_pids` expose the waiter before collecting
   the lock incident.
10. runs the isolated stale-statistics case, shifting 2% to 92% rare values
    without `ANALYZE`, then verifies the typed maintenance proposal against
    `EXPLAIN ANALYZE` estimates and actual rows.

For reversible latency actions, the experiment exporter also records a
post-rollback baseline check.  It compares the original and restored plan
signals and result, so a reported improvement is accompanied by a reversal
test.  This check is diagnostic evidence only; it is not used to select or
discard a timing sample.

The output contains the raw timings, warm-up/timed sample counts, p95
calculation inputs, complete plan summaries, exact `pg_stat_statements` deltas,
PostgreSQL version, action state, proposal derivation evidence, verification
state, feedback, rollback state, and post-rollback reversal check.  Preserve
the output together with the container image digest, host CPU/memory,
PostgreSQL configuration, dataset cardinality, and the exact repetition count.
Report repeated runs rather than selecting one favorable sample.  The feedback
evaluator requires at least three timed samples, identical workload/measurement
parameters, complete equal results, the after-phase setting to match the action
target, a verified plan signal, and at least a five-percent improvement in both
median and p95 before labeling an action `effective`.

## Interactive controls

The Workloads panel accepts the number of timed repetitions and warm-ups and
exposes real `customer_id`, baseline `work_mem`, and baseline parallel-worker
knobs.  The server validates them against the connected PostgreSQL
data/settings before executing them.  The browser
submits a phase and typed parameters; it never submits an arbitrary tuning
statement.  The guided walkthrough presents the observable baseline →
diagnosis → proposal review → apply → after → feedback → rollback sequence;
the apply step remains an explicit operator action.  After applying a proposed
action, the after-phase result can be recorded as measured feedback.  Feedback
is idempotent for the same action and before/after run IDs.  A result card can
request a read-only diagnosis from its stored PostgreSQL plan, including the
evidence fields and knowledge entries used by the rule.  The result card also
renders a before/after plan-signal diff so a timing change is not shown without
its observed execution change.

The same UI also exposes an opt-in automatic-resolution control for a
completed baseline workload.  It submits only the baseline run ID and a
rollback policy; the server reuses the recorded SQL/parameters, generates a
typed proposal, retrieves RAG and compiler-backed code knowledge, applies the
typed database action, measures the same SQL, records feedback, and rolls
back when requested.  The automatic path is therefore executable end to end,
while the manual path remains available for step-by-step review.  Neither
path executes retrieved or generated text as SQL.

For a live walkthrough, `POST /api/demo/clean` stops any in-memory lock
contention runs and rolls back only applied actions whose proposal provenance
is `workload_run`.  It verifies that no demo lock waits remain and preserves
workload runs, feedback, incidents, and audit events.  `GET /api/export` emits
those collections, current settings, server metadata, collector status, the
lock snapshot, the fixed RAG report, and a `demo_manifest` as a versioned JSON
evidence bundle.  `GET /api/demo/manifest` provides the compact manifest
without the historical collections.  These controls are scoped to the local
artifact and do not delete the database or its history.

## Evidence and persistence

| Evidence | PostgreSQL source | Persistent record |
| --- | --- | --- |
| counters and interval rates | `pg_stat_database` | `keeninsight.snapshots` |
| host/container resource snapshot | Linux `/proc` and filesystem APIs | `keeninsight.snapshots` |
| active sessions and waits | `pg_stat_activity` | snapshot payload |
| blocking graph | `pg_locks`, `pg_blocking_pids` | snapshot payload / incidents |
| cumulative query counters | `pg_stat_statements` | snapshot payload and query deltas |
| actual access path, spill, and parallel workers | JSON `EXPLAIN` | workload run / diagnosis |
| stale planner estimate | `EXPLAIN ANALYZE` estimated/actual rows and `pg_stat_user_tables` | standalone stats run / completed `ANALYZE` action |
| setting/catalog validation | `pg_settings`, `pg_catalog` | tuning action state |
| change history | application events | `keeninsight.audit_events` |
| measured before/after effect | two completed workload runs | `keeninsight.tuning_feedback` and empirical KB |
| fine-grained function/kernel events | BCC uprobe/uretprobe and `vfs_write` kprobe | `probe_sessions`, `ebpf_events`, and Kafka `event_outbox` |
| exact batched invocation count | kernel aggregate payload (`count`, duration/bytes totals) | `ebpf_events.payload` and `sql_lifecycle_events` |
| SQL/function lifecycle | query identity plus event time interval | `keeninsight.sql_lifecycle_events` |
| learned diagnosis | XGBoost temporal model and TreeSHAP | `model_runs`, `model_predictions`, `model_attributions` |
| differential hotspot | persisted baseline/degraded eBPF vectors | `differential_profiles` and function model rows |
| code-level causal context | clang-generated LLVM CFG/SSA/data-flow, call edges, source locators, and bounded cross-TU paths | `source_analyses` |
| code retrieval and semantic hypothesis | exact source snippets grouped with compiler support; experiment-required hypothesis and validation | `code_knowledge`, `/api/source-analysis/knowledge` |
| query-agent candidates | measured hint/rewrite candidates and pairwise ranking | `optimizer_runs`, `optimizer_candidates` |
| source-attributed research context | collected paper/review/demo/project text | `rag_documents` / `rag_chunks`, locators, and `rag_sources/manifest.json` |
| continuously collected runtime context | recent runs/actions/feedback/incidents/snapshot/models/lifecycle/source/code-knowledge/optimizer/experience | RAG background sync and `/api/rag/status` |
| semantic retrieval | BGE small 384-dimensional passage/query embeddings | `rag_embeddings`, `/api/rag/vector/status` |
| automatic closed-loop tuning | typed server orchestration: proposal → apply → same-SQL measurement → feedback → rollback | `/api/workload-runs/{id}/auto-resolve`, `artifacts/auto-resolve-strict.json` |
| long-query anomaly path | duration/query-age aware immediate eBPF emission and lifecycle anomaly reasons | `artifacts/ebpf-long-query-strict.json` |

The empirical knowledge entry uses a smoothed proportion of successful measured
outcomes.  Its confidence is an aggregate of recorded observations, not a
claim of predictive model accuracy.  The normalized feedback table remains the
complete history; the knowledge row only retains a bounded recent context for
display.

`docs/MEASURED_RESULTS.md` is a development-environment summary generated from
the same protocol.  It is included for orientation only; the raw JSON from
`scripts/repeat_experiment.py` is the reproducibility record.

## RAG source and evidence contract

The checked-in `rag_sources/manifest.json` is the static source inventory.  It
is currently built from 87 portable text records: five paper source files and
nine text-bearing PDF design assets from `KeenInsight_VLDB_Demo.zip`, the
extracted five-page review, all 12 HTML pages from `English.zip`, current
project design/implementation/UI/SQL/test/deployment files, two complete asset
inventories, and six compact summaries of the raw evaluation/preflight/
statistics-case/post-change-regression JSON.
Generated records retain a source key, source type, title, URI, character
count, and SHA-256 digest.  Binary diagrams themselves are inventoried but are
not treated as hidden answer text; only searchable PDF text extractions and
source LaTeX/HTML are placed in the retrieval corpus.

The runtime indexer adds stable records for recent workload runs, tuning
actions, feedback, incidents, the latest telemetry snapshot, compiler-backed
source/code-knowledge records, and empirical knowledge entries.  Its default window is 100 rows per dynamic category and
the latest snapshot is retained; stale derived runtime copies are pruned after
each sync while their source-table history remains intact.  It can be
refreshed with `POST /api/rag/sync`.  It stores normalized documents and
bounded overlapping chunks in `keeninsight.rag_documents` and
`keeninsight.rag_chunks`; a GIN index supports PostgreSQL full-text retrieval.
`GET /api/rag/search` returns ranked exact chunks, while `POST /api/rag/ask`
returns the same passages, stable `R#` citations, provenance, and either an
extractive answer or an explicitly configured external generator result.  The
same optional generator is used only for display-only incident/plan/source
explanations and for typed optimizer hypotheses; the no-generator path is the
default artifact behavior.

The static corpus has a fixed retrieval check in
`evaluation/rag_questions.json`.  It reports Recall@1/Recall@5, MRR,
precision@5, term coverage, and negative-query accuracy for 20 questions; the
question file is not indexed.  The default lexical report is supplemented by
the same questions evaluated through persisted BGE embeddings and hybrid
reciprocal-rank fusion in `artifacts/rag-vector-evaluation-strict.json` and
`artifacts/rag-hybrid-evaluation-strict.json`.  These are retrieval/source
attribution checks, not a claim that a local LLM generated the answer.
`GET /api/demo/manifest` records the corpus hash, live PostgreSQL version,
fixture cardinalities, scenario contract, and measurement thresholds used by
the artifact.

RAG collection is deliberately read-only with respect to the demo fixture:
the worker reads only persisted evidence and source files, never the
`keeninsight_demo` relations.  RAG cannot apply a tuning action from generated
text.  The opt-in automatic workload endpoint is a separate server-side
orchestrator that invokes typed action handlers, measures the recorded SQL,
and records feedback/rollback; the manual path still requires explicit user
review and apply.
The static path is content-hash idempotent, and the background interval is
kept outside the timed workload path.  The API/UI exposes sync status and
retrieved chunks so a reviewer can inspect what supported an answer.

The supplied materials can be recollected with:

```bash
python scripts/collect_sources.py \
  --output rag_sources \
  --paper-zip /root/demo/KeenInsight_VLDB_Demo.zip \
  --legacy-demo-zip /root/demo/English.zip \
  --review-pdf "/root/demo/Conference Management Toolkit - View review.pdf" \
  --project-root .
```

## Scope and deployment cautions

The bundled Compose service is a local research artifact, not an internet-facing
control plane: it has no authentication or multi-tenant authorization layer.
For a deployment, place it behind authentication and restrict the service
role.  Monitoring and mutation privileges should be separated; applying a
database setting or creating an index must be granted explicitly.  Existing
PostgreSQL instances need `pg_stat_statements` in `shared_preload_libraries`
and the extension installed in the monitored database.

The lexical retriever remains available as a dependency-light fallback, while
the UI exposes vector and hybrid retrieval when `fastembed` and its model cache
are available.  The embedding index is synchronized explicitly and reports its
model, dimensions, processed rows, and errors.  An optional OpenAI-compatible
generator can summarize retrieved context and other bounded evidence, but
citations remain visible and generator failures fall back to deterministic
explanations or the extractive brief.

The eBPF overhead control is part of the artifact contract.  Directly tracing
the high-frequency `BufFileWrite` symbol is intentionally measurable and can be
expensive; the adaptive selector uses the real persisted invocation rate to
reject it under the configured budget.  See `docs/EBPF_OPERATIONS.md` and
`artifacts/ebpf-overhead-strict.json` for both arms, including zero-loss and
plan/result checks.
