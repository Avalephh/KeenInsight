# Upstream branch compatibility audit

Audit date: 2026-09-07.  This note records what was compared against the
public KeenInsight branches and what is actually executable in this
PostgreSQL artifact.  A branch is treated as an independent implementation
when it has a different runtime, database contract, or safety model; its file
names are not used as evidence that the capability is present.

Reference repository: [Avalephh/KeenInsight](https://github.com/Avalephh/KeenInsight).
The inspected branch tips were `main` (`47eef64`), `WorkloadTune_new`
(`a5f10ec`), `WorkloadTune` (`ff35ee5`), `dev` (`cb31542`), `dream`
(`eda702e`), and `db-replay` (`d8a6cf2`).

## Capability mapping

| Upstream branch and source | What the old code actually provides | Current artifact decision |
| --- | --- | --- |
| [`main/sysinsight_front/src/api/tune.ts`](https://github.com/Avalephh/KeenInsight/blob/main/sysinsight_front/src/api/tune.ts), [`WorkloadTune_new`](https://github.com/Avalephh/KeenInsight/tree/WorkloadTune_new) | Vue workflow and `/api/tune/*` contracts: configuration, first evaluation, exception functions, one-step recommendation, apply parameters, and multi-round polling/results. The old backend starts container/file-based jobs. | **Implemented as a compatibility boundary.** `app/legacy_tune.py` preserves the request names, response fields, polling states, and page sequence. Its workers execute the current real PostgreSQL scenarios, timed samples, `EXPLAIN ANALYZE`, typed actions, feedback, and rollback. `legacy_tune_tasks` makes state restart-aware instead of keeping it only in memory. |
| [`WorkloadTune`](https://github.com/Avalephh/KeenInsight/tree/WorkloadTune) | Earlier MySQL optimizer/control-path assets, extracted code, and tuning-lake data. Its paths and assumptions are not the same as the later Vue API branch. | **Reference data only.** The PostgreSQL port does not relabel MySQL knobs or reuse its precomputed numbers. The source/RAG layer may retrieve these materials, but mutation candidates must pass the PostgreSQL allowlist and live measurement gates. |
| [`dev/one/run_pipeline.py`](https://github.com/Avalephh/KeenInsight/blob/dev/one/run_pipeline.py), [`dev/one/ApplyKnob.py`](https://github.com/Avalephh/KeenInsight/blob/dev/one/ApplyKnob.py) | Monitor → threshold → differential profile/SHAP → root cause → function/knob mapping → knowledge → apply pipeline. The branch also contains a pgbench walkthrough. | **Core stages are implemented with the current evidence model:** `app/collector.py`, `app/ebpf_monitor.py`, `app/probes.py`, `app/modeling.py`, `app/diagnose.py`, `app/knowledge.py`, `app/rag.py`, and `app/tuning.py`. The current workload suite uses namespaced PostgreSQL fixtures and records all measurements. The old pgbench script is not imported as evidence because its `create_function_profiles()` path explicitly creates simulated profiles and its settings workflow uses broad `ALTER SYSTEM` mutations. Therefore the artifact exposes real PostgreSQL scenarios, not a false pgbench result. |
| [`dev/one/README_PGBENCH_DEMO.md`](https://github.com/Avalephh/KeenInsight/blob/dev/one/README_PGBENCH_DEMO.md) | A separate pgbench/TPC-B-style demo with `work_mem`, `shared_buffers`, and `maintenance_work_mem` examples. | **Explicitly not claimed as the current benchmark.** `work_mem` pressure is covered by real hash, aggregate, and sort cases; shared buffers and maintenance memory are not silently reported as tuned by the current service. This prevents a reviewer from confusing a script-level scenario with a measured artifact result. |
| [`dream/agent/action/action_manager.py`](https://github.com/Avalephh/KeenInsight/blob/dream/dream/agent/action/action_manager.py), [`dream/agent/action/action_space.py`](https://github.com/Avalephh/KeenInsight/blob/dream/dream/agent/action/action_space.py), [`dream/database/pg_env.py`](https://github.com/Avalephh/KeenInsight/blob/dream/dream/database/pg_env.py) | SQL-agent loop: retrieve experience, generate hints/rewrite/actions, evaluate, roll back, and save experience. The branch requires `pg_hint_plan`, QED, downloaded RCRank/retriever models, and a separate environment. | **Semantics aligned, dependencies intentionally replaced.** `app/optimizer.py` implements session-local allowlisted hints, a conservative equivalent rewrite, result/plan verification, measured candidate ranking, and proposal-only index candidates. `app/experience.py` stores measured feedback. The service does not claim pg_hint_plan or QED execution; its API reports the local deterministic verifier and optional LLM boundary. |
| [`db-replay/README.md`](https://github.com/Avalephh/KeenInsight/blob/db-replay/README.md), [`internal/replay/replayer.go`](https://github.com/Avalephh/KeenInsight/blob/db-replay/internal/replay/replayer.go) | Independent Go service for pgAudit-log parsing, transaction replay, speed/concurrency controls, and replay reports. It has its own HTTP server, storage/configuration, and lifecycle. | **Kept as an optional companion, not mixed into the tuning control plane.** The current service consumes controlled live PostgreSQL workloads and persists their evidence. It does not expose the Go service's `/api/v1/replay/*` contract and must not be presented as if replay logs were supported. Adding the Go service later is an integration/deployment choice, not a reason to weaken the live measurement path. |

## Compatibility contract now tested

The old page sequence is supported by these routes:

```text
POST /api/tune/setConfig
POST /api/tune/firstEvaluate
GET  /api/tune/firstEvaluateResults
GET  /api/tune/oneStepStatus
GET  /api/tune/exception-functions
POST /api/tune/oneStep
GET  /api/tune/oneStepResult
POST /api/tune/applyParams
POST /api/tune/startTune
GET  /api/tune/multiTuneStatus
GET  /api/tune/getMultiTuneResult
```

The compatibility fields `tps`, `qps`, `latency`, `predicted_score`, and
`improvement` are display fields required by the old page. Their semantics are
recorded alongside each response: rate/score values are derived from timed
PostgreSQL samples, and the field called `predicted_score` is the measured
after-phase score rather than an unvalidated prediction. Multi-round candidates
use the same SQL, repetitions, result check, plan signal, feedback record, and
rollback path as the canonical API.

The contract tests in `tests/test_legacy_compatibility.py` cover route names,
payload normalization, and evidence identity. The smoke protocol additionally
executes the full sequence against PostgreSQL; the latest verified run used
baseline run `576`, candidate after run `577`, action `191`, feedback `173`,
and observed an effective `45.4008%` median latency improvement before
rollback. The machine-readable record is
[`artifacts/legacy-workload-tune-compatibility.json`](../artifacts/legacy-workload-tune-compatibility.json).
These IDs are audit evidence from one local run, not a universal performance
claim.

## Review boundary

The old branches remain valuable source material for RAG and design review,
but the executable source of truth is the current API and its persisted
records. A branch feature is considered implemented only when the current
service can show the input, the analysis evidence, the typed mutation (if
any), the same-workload measurement, and the resulting audit/rollback state.
Static JSON, old profiler snapshots, and a benchmark label alone do not satisfy
that criterion.
