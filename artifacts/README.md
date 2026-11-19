# Reproducibility artifacts

This directory contains raw validation outputs generated against the local
PostgreSQL instance.  JSON files are evidence records, not dashboard fixtures.

- `aa-control-results.json`: identical A/A workload arms; no tuning action is
  applied.  The report requires matching plan signatures and result values.
- `ui-aa-control.json`: a post-UI-change A/A control on the grouped-aggregate
  spill workload; it verifies that restoring the original page shell and
  adding the compact dashboard projection does not apply a tuning action or
  change the observed plan/result.
- `preflight-pg12.json`: fail-fast environment, schema, extension, settings,
  lock, API, export, and RAG status checks.
- `final-regression-pg12.json`: repeated before/after tuning protocol with raw
  timings, plans, proposal derivation, feedback, verification, rollback state,
  and post-rollback reversal checks.
- `post-change-regression-pg12.json`: one complete post-change protocol run;
  used as a regression check that the added manifest/RAG/statistics code does
  not alter the original workload contract.
- `pg16-repeat-results.json`: created only when `scripts/validate_pg16.sh`
  completes against the PostgreSQL 16 Compose image.
- `pg16-validation-status.json`: records the current environment limitation;
  the script was checked and returned exit code 2 because Docker is absent.
- `stats-case.json`: standalone stale-statistics recovery evidence; it records
  the estimate/actual mismatch, completed `ANALYZE` action, feedback criterion,
  and clean-probe state.
- `rag-evaluation.json`: fixed 20-question lexical retrieval/source-attribution
  evaluation, including Recall@1/Recall@5, MRR, term coverage, and a negative
  no-hit query.
- `rag-vector-evaluation-strict.json`: the same fixed questions evaluated with
  persisted BGE embeddings and exact cosine retrieval.
- `rag-hybrid-evaluation-strict.json`: the same questions evaluated with
  lexical/vector reciprocal-rank fusion.
- `ebpf-smoke-strict.json`: real BCC probe session, exact batched invocation
  accounting, SQL lifecycle reconstruction, perf-buffer loss, and Kafka
  delivery checks.
- `ebpf-overhead-strict.json`: clean, direct high-frequency probe, and
  measured-budget adaptive arms, including overhead, loss, result, and plan
  comparisons.
- `ebpf-auto-adapt-strict.json`: online in-session event-rate guard that
  dynamically detaches an over-budget user-space probe under real spill load.
- `optimizer-strict-smoke.json`: live hint/rewrite/index-candidate optimizer
  runs with PostgreSQL result equivalence and ranking evidence.
- `ICDE_CLAIM_AUDIT.md` (in `docs/`): paper-to-code capability audit and the
  exact boundary of claims supported by the executable artifact.

Each output should be interpreted together with the PostgreSQL version,
dataset cardinality, host/container resources, configuration, and protocol
parameters stored in the JSON itself.

The live API also exposes `GET /api/demo/manifest` and includes the same
manifest in `GET /api/export`; it records source-corpus integrity, runtime
evidence counts, active model/LLVM/optimizer/RAG status, and the explicit
PostgreSQL symbol/deployment boundary.  Raw artifacts are generated from the
connected service; they are not frontend fixture data.
