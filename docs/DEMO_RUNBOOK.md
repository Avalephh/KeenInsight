# ICDE live demonstration runbook

The live service is available at `http://127.0.0.1:8000` after
`docker compose up --build` or after starting the API against an existing
PostgreSQL instance.  The root opens the original English.zip Cluster/Host
List page shell and
`http://127.0.0.1:8000/console` opens the evidence-first control plane.  The
original page layout is retained, while its illustrative rows and charts are
cleared before load and replaced by API responses from PostgreSQL.

The original pages are also directly available under `/static/legacy/`.
Navigation, filters, detail dialogs, source-code views, knowledge views, and
real-time range buttons are live; operations that would require an unscoped
host-management action explicitly point to the typed control-plane workflow.

To trigger a database anomaly from the original page, click the blue **Anomaly
lab** entry in the top-right navigation (or **Console** and then **Workloads**).
It opens the guided workload view directly.  Choose a scenario and click
**Start real pressure experiment**.  The control moves to the dedicated
**Real-time measurement** page: `POST /api/workloads/{scenario}/runs/start`
creates the durable run first, while the page polls
`GET /api/workload-runs/{id}` for actual warm-up/timed samples and
`pg_stat_activity` sessions.  The next-step button returns to the guided flow
only after the run is completed.  The button is an explicit operator action;
opening the page never runs a workload.

Before a live session that should show business-shaped traffic, seed the
dedicated TPC-C-shaped schema once:

```bash
python scripts/bootstrap_tpcc.py
```

The generator sizes the data from the host resources and records exact row
counts in `keeninsight_tpcc.load_info`.  On the current host the selected five
warehouses produce 150,000 orders and about 1.5 million order lines.  The
normal-load selector then offers real order lookup, customer-history, and
stock-check queries against that schema.  This is a bounded demo seed, not a
TPC-C benchmark implementation or benchmark result.

The TPS cards show both the raw database counter rate and a five-point rolling
median trend.  Red vertical markers are based on the raw samples, so a short
burst is not smoothed away; its exact value and timestamp remain visible in
the chart legend.  The database TPS includes all database activity, including
the control plane.  Normal-load execution counts and latencies are shown in
the normal-load card and are the appropriate business-traffic evidence.

## Recommended five-minute walkthrough

1. Open **Workloads** through **Anomaly lab**, choose **aggregate_spill**, set timed samples to 5 (or
   7 for a reported result), warm-ups to 2, and leave baseline `work_mem` at
   `64kB`.  Click **Start real pressure experiment**, keep the real-time
   measurement page visible while the warm-up and timed samples advance, then
   use **Enter next step** to return to the guided flow.  Show the actual
   sorted aggregate, external sort/disk use, temporary blocks, timing samples,
   and the server execution time.
2. Use **Create validated action proposal**.  The action is a typed,
   database-scoped `work_mem` change derived from the observed plan; the
   service validates the parameter and current PostgreSQL metadata before it
   can be applied.  Click **Apply** only after showing the before state.
3. Run **after** with the same workload parameters.  Show the changed plan,
   no temporary blocks, timing improvement, and the plan-signal diff.  Record
   feedback, which requires matched SQL/parameters, an unchanged result, a
   verified plan change, and improvement in both median and p95.
4. Click **Rollback**.  The setting returns to its prior database override or
   default, and the action/audit records remain available.  **Clean demo
   state** can perform the same scoped recovery for a session and release any
   lock-contention workers.

There is also an **Automatic resolve and rollback** control for a completed
baseline run.  It is opt-in and sends only the run ID plus the rollback policy.
The server reuses the recorded SQL and parameters, derives the typed action
from the observed plan, retrieves RAG and compiler-backed code knowledge,
deploys the action, runs the same SQL, records strict feedback, and rolls back
on success when requested.  The response exposes the rule evidence, source
locators, targeted experiment, code-knowledge validation, and final action
state.  This is the complete automatic path; the four steps above remain the
inspectable manual path.

The same flow works for the following non-index cases, which are the primary
showcase of the system's operator-aware capability:

| Case | User-visible pressure | Evidence to point at | Recovery |
| --- | --- | --- | --- |
| hash join spill | session `work_mem=64kB` | multi-batch `Hash`, temp blocks | database `work_mem` proposal |
| grouped aggregate spill | session `work_mem=64kB` | sorted aggregate, external merge/disk, temp blocks | database `work_mem` proposal |
| window sort spill | session `work_mem=64kB` | external merge/disk sort | database `work_mem` proposal |
| parallel worker underprovisioning | session workers set to 0 | `Workers Planned/Launched` and live host limits | bounded database worker proposal |
| stale statistics | 2% → 92% distribution shift without `ANALYZE` (isolated probe; autovacuum disabled for the window) | estimated vs. actual rows and `pg_stat_user_tables` | typed `ANALYZE` completed; clean restores the probe |
| lock contention | two real row-locking sessions | waiter, blocker, `pg_blocking_pids`, lock graph | stop/release the demo sessions |
| filtered scan/index (secondary) | missing btree index | observed `Seq Scan` predicate | validated `CREATE INDEX CONCURRENTLY` |

## RAG portion

Open **RAG knowledge** after the walkthrough and use source filters to make
the provenance visible:

```text
review:        How should the demonstration become interactive?
paper:         How does the paper describe the multi-source knowledge base?
implementation: How are tuning actions validated and rolled back?
runtime-feedback: Which observed action outcomes were effective?
```

Each answer shows the extractive generator label (unless an optional external
generator is configured), the exact retrieved chunks, stable `R#` citations,
source type, and provenance URI.  Use **Sync evidence** after a new workload
or feedback record if the background interval has not elapsed.  A reviewer can
then inspect the same runtime evidence through **Export evidence**.

For a reproducible RAG check, run:

```bash
python scripts/evaluate_rag.py --output artifacts/rag-evaluation.json
```

The report uses 20 fixed questions outside the corpus and displays Recall@1,
Recall@5, MRR, precision, term coverage, and the negative no-hit result.

## Strict paper-capability walkthrough

For the portions that are not represented by the five-minute workload card,
use the **eBPF observability**, **Models & LLVM**, and **Tuning agent** views:

1. Start probes before the pressure query.  The API attaches real BCC
   uprobe/uretprobe pairs to PostgreSQL functions and a `vfs_write` kprobe;
   stop the session after the workload and open its SQL lifecycle rows.  The
   event count in a batched message is the exact invocation count, not the
   number of Kafka messages.
2. After at least four stopped probe/workload pairs, click **Adapt probes** or
   call `/api/ebpf/adapt` with `max_event_rate=1000`.  Inspect the Lasso
   coefficients, measured invocation rates, and rejected high-frequency
   probes.  The selected set is dynamically detached/attached without a
   PostgreSQL restart.
3. In **Models & LLVM**, train latency and root-cause models from persisted
   plan/configuration observations, run a plan-only prediction, and show its
   TreeSHAP rows.  Then compare two real eBPF sessions and use **Map to knobs**
   to display the function differential, source locator, LLVM CFG/SSA path, and
   typed system action.  Click **Retrieve code knowledge** to show the exact
   PostgreSQL source snippets, compiler support rows, experiment-required
   hypothesis, and validation accumulated from a real tuning experiment.
4. In **Tuning agent**, submit a read-only query.  Every session-local hint and
   rewrite is measured on PostgreSQL, checked for result equivalence, and
   ranked by measured latency/plan cost or the learned pairwise model.  Index
   candidates are proposal-only and require the same explicit reviewed action
   gate as the workload path.

The strict reproducibility controls retain both sides of the observability
trade-off:

```bash
python scripts/ebpf_strict_smoke.py
python scripts/ebpf_overhead_smoke.py
python scripts/optimizer_strict_smoke.py
python scripts/ebpf_auto_adapt_smoke.py
python scripts/ebpf_long_query_smoke.py
python scripts/code_knowledge_smoke.py
python scripts/auto_resolve_smoke.py
```

`ebpf-overhead-strict.json` measures the expensive direct `BufFileWrite` arm
and the budgeted adaptive arm.  Reviewers should report both; the adaptive
result is supported by its measured event-rate selection and equal plan/result
checks, not by a synthetic overhead constant.

`ebpf-auto-adapt-strict.json` demonstrates online detachment of an over-budget
high-frequency user-space probe while retaining the kernel signal;
`ebpf-long-query-strict.json` demonstrates immediate anomaly emission for a
real three-second query.  `auto-resolve-strict.json` demonstrates the
server-side automatic tuning loop and its rollback.

## Reset between demonstrations

Click **Clean demo state** or run:

```bash
curl -X POST http://127.0.0.1:8000/api/demo/clean
python scripts/preflight.py --require-clean
```

The cleanup releases in-memory lock sessions, rolls back only workload-originated
applied actions, verifies no demo index or waiting lock remains, and preserves
historical runs, incidents, feedback, audit records, and their RAG-derived
copies within the retention window.  A completed `ANALYZE` action is not
rolled back; the cleanup explicitly restores and re-analyzes the isolated
statistics probe instead.
