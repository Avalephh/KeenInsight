> **v2 scope note (2026-11-19):** SQL-level tuning (query optimizer agent,
> hints, rewrites, index actions, SQL evidence pages) has been removed from
> this codebase.  This document records the pre-v2 artifact; the active product
> is the SysInsight system-parameter tuning path only.

# Development measurement snapshot

This is a human-readable snapshot of the final local run of
`scripts/repeat_experiment.py` on 2026-09-06.  It is evidence for the PostgreSQL artifact,
not input data, a dashboard fixture, or a promise for every machine.  Delete
or regenerate it when the database, PostgreSQL version, hardware, or protocol
changes.

Environment: PostgreSQL 12.22, database `keeninsight`, demo relations with
20,000 customers, 800,000 orders, and 20 accounts.  The protocol used three
complete repetitions; each phase used seven timed executions after two
warm-ups.  The database returned `work_mem=4MB` before each memory action;
each baseline used session-local `work_mem=64kB`, with evidence-backed
proposals of 8MB for hash/aggregate spill and 66MB for window-sort spill.
Before each parallel-worker action, the database returned
`max_parallel_workers_per_gather=2`; the evidence-backed proposal raised it
to 3 and every after plan launched 3 workers.

This snapshot was regenerated after the guided walkthrough, plan-diff view,
direct plan diagnosis, evidence export, scoped cleanup controls, and RAG
indexer were added.  The unchanged workload protocol completed all 15 tunable
comparisons with zero failures; all 15 remained `effective` and all 15 actions
were rolled back.  The observed median improvement ranges were 98.97–99.16%
for the optional index, 30.86–33.49% for hash spill, 62.16–62.38% for grouped
aggregate spill, 45.16–46.08% for window sort, and 40.29–41.99% for parallel
workers.  The corresponding p95 ranges were 98.21–98.55%, 23.26–30.95%,
62.20–64.28%, 44.69–45.76%, and 40.32–41.26%.  The result verifies plan/effect
stability after the additions; absolute timings remain machine- and
PostgreSQL-version-dependent.

| Scenario | Repetition | Median before → after (ms) | P95 before → after (ms) | Median improvement | P95 improvement | Server evidence | Feedback | Rollback |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| missing index | 1 | 20.946 → 0.215 | 22.669 → 0.328 | 98.97% | 98.55% | `Seq Scan` → `Bitmap Index Scan` | effective | rolled back |
| missing index | 2 | 20.115 → 0.177 | 20.273 → 0.329 | 99.12% | 98.38% | `Seq Scan` → `Bitmap Index Scan` | effective | rolled back |
| missing index | 3 | 19.989 → 0.167 | 20.283 → 0.362 | 99.16% | 98.21% | `Seq Scan` → `Bitmap Index Scan` | effective | rolled back |
| hash spill | 1 | 74.700 → 51.085 | 75.739 → 53.609 | 31.61% | 29.22% | 32 hash batches + temp blocks → fewer batches, no temp blocks | effective | rolled back |
| hash spill | 2 | 74.704 → 51.649 | 75.306 → 57.790 | 30.86% | 23.26% | 32 hash batches + temp blocks → fewer batches, no temp blocks | effective | rolled back |
| hash spill | 3 | 75.426 → 50.165 | 75.694 → 52.264 | 33.49% | 30.95% | 32 hash batches + temp blocks → fewer batches, no temp blocks | effective | rolled back |
| grouped aggregate spill | 1 | 156.235 → 59.123 | 158.500 → 59.911 | 62.16% | 62.20% | sorted aggregate + external merge/Disk + temp blocks → hashed aggregate, no temp blocks | effective | rolled back |
| grouped aggregate spill | 2 | 162.821 → 61.346 | 169.677 → 62.043 | 62.32% | 63.43% | sorted aggregate + external merge/Disk + temp blocks → hashed aggregate, no temp blocks | effective | rolled back |
| grouped aggregate spill | 3 | 157.299 → 59.177 | 166.903 → 59.624 | 62.38% | 64.28% | sorted aggregate + external merge/Disk + temp blocks → hashed aggregate, no temp blocks | effective | rolled back |
| window sort spill | 1 | 736.626 → 400.772 | 743.055 → 403.235 | 45.59% | 45.73% | external merge/Disk + temp blocks → quicksort/Memory, no temp blocks | effective | rolled back |
| window sort spill | 2 | 739.682 → 405.676 | 745.637 → 412.425 | 45.16% | 44.69% | external merge/Disk + temp blocks → quicksort/Memory, no temp blocks | effective | rolled back |
| window sort spill | 3 | 738.221 → 398.042 | 740.584 → 401.656 | 46.08% | 45.76% | external merge/Disk + temp blocks → quicksort/Memory, no temp blocks | effective | rolled back |
| parallel workers | 1 | 31.965 → 19.088 | 32.398 → 19.337 | 40.29% | 40.32% | session workers 0 → database setting 3; actual workers 0 → 3 | effective | rolled back |
| parallel workers | 2 | 32.129 → 18.638 | 32.504 → 19.094 | 41.99% | 41.26% | session workers 0 → database setting 3; actual workers 0 → 3 | effective | rolled back |
| parallel workers | 3 | 32.053 → 18.852 | 32.405 → 19.088 | 41.19% | 41.09% | session workers 0 → database setting 3; actual workers 0 → 3 | effective | rolled back |

The source JSON contains the complete sample lists, exact
`pg_stat_statements` counter deltas (7 timed calls per phase), server
execution-time observations, query results, plan nodes, action verification,
lock incident rows, and failures.  After the later manifest, RAG, and
isolated-statistics additions, the post-change single-protocol check in
`artifacts/post-change-regression-pg12.json` also completed all five latency
cases as effective and rolled back every action.  The 7-sample/2-warm-up A/A
control in `artifacts/aa-control-results.json` passed with identical plan
signatures and results.  These are regression checks for the implementation
additions, not a replacement for the three-repetition snapshot above.
Regenerate the original repeated protocol with:

```bash
python scripts/repeat_experiment.py \
  --runs 3 --repetitions 7 --warmups 2 \
  --output experiment-repeated-results.json
```

The original paper's static demo values are not used as a baseline here.  The
paper source has no numerical table sufficient to reproduce those values, so
the ICDE version must report raw PostgreSQL observations and its environment.

## Strict architecture checks

The following checks are separate from the five-scenario latency table and
were executed against the same live PostgreSQL 12.22 service:

| Capability | Observed evidence | Result |
| --- | --- | --- |
| BCC eBPF + Kafka | session 26; 1,535,259 exact invocations, zero perf-buffer loss; lifecycle event counts sum exactly to the raw count | passed |
| Adaptive probe selection | high-frequency `BufFileWrite` arm: +4520.49% median overhead; measured-budget adaptive arm rejected it and measured +9.63% median / +10.88% p95 | passed |
| Plan-only ML | XGBoost latency run 21: 371/93 temporal split, RMSE 2.813 ms, MAE 1.100 ms, R² 0.99988; probe-overlapping runs excluded | passed |
| Root-cause ML | XGBoost run 22: six observed classes, 371/93 temporal split, accuracy 1.0 | passed on controlled fixture |
| Function SHAP | differential profile 1 and function model run 20 persisted TreeSHAP contributions from real event vectors | passed; small observed sample |
| LLVM evidence | 370 direct knob references, 15,184 call-edge rows, 2,314 propagation rows from PostgreSQL source compiled with clang | passed |
| Query agent | optimizer strict smoke: measured hints/rewrite, result equivalence, source/RAG evidence, reviewed/rejected index proposal | passed |
| Embedding RAG | BGE 384-dimensional vectors persisted; vector and hybrid reports each evaluated 20 fixed questions with 20/20 pass | passed |

The raw records are `artifacts/ebpf-smoke-strict.json`,
`artifacts/ebpf-overhead-strict.json`, the model artifacts under
`artifacts/models/`, `artifacts/optimizer-strict-smoke.json`, and the strict
RAG reports.  The high-frequency eBPF result is retained because it is a real
observer-overhead control; it is not folded into the clean database model.
