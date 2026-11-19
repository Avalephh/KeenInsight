# PostgreSQL 12 local foreground-TPS model

This is a newly trained **experimental PostgreSQL model**, not a recovered
MySQL pretrained model and not RCRank or an LLM explanation.

- Input: 678 function sample-rate features fixed by the pinned PostgreSQL
  function/parameter/source association library. An absent function in a
  complete measured counts file has zero sampled hits, not zero runtime cost.
- Target: measured foreground control TPS under pressure / normal control TPS.
  Each label must agree with the successful control worker's measured result,
  not the injected external TPS or an LLM estimate.
- Data: 53 distinct actual perf/case pairs, 37 scenario IDs, from the existing
  local `results/tpcc_api_validation` benchmark history. No synthetic examples.
- Predictor: 96-tree RandomForestRegressor, max_depth 5, min_samples_leaf 2,
  max_features .5, n_jobs 1, random_state 20261023.
- Validation: five scenario-grouped folds, MAE 0.111456 retention ratio
  (11.1456 percentage points), R² 0.358289. This is limited generalization,
  **not** a production TPS prediction guarantee. AP-only/TPC-DS input is not calibrated.
- Latest independent later batch: **28.4455 pp MAE, R² -4.9642**, six measured
  scenarios. Adding 12 earlier aligned measurements produced 17.4650 pp MAE,
  R² -1.2697 on that same untouched final batch. The candidate failed the
  frozen promotion rule and **was not promoted**. See `external_validation.json`
  for hashes and exact test predictions. Historical cross-validation is not a
  substitute for this time-batch validation.
- Explanation: actual `shap.TreeExplainer`, explicit `tree_path_dependent`
  mode. Background is the fitted trees' actual training-path counts. A strict
  additivity check ensures baseline + all feature SHAP values = model output.
- SHAP units: TPS-retention ratio. Multiply by 100 for percentage-point
  contributions. Positive means higher predicted retention, not higher latency.
  None of these associations establishes causality or guarantees tuning benefit.

Files: `forest.joblib` is locally generated trusted model data; `manifest.json`
pins hashes and runtime versions; `training_provenance.json` records the
measured features/labels, source file hashes and held-out predictions.
Both hashes are verified before deserialization. The worker has no model-upload
endpoint and does not accept an arbitrary user-supplied model path.
`versions/<model-hash-prefix>-<data-hash-prefix>/` retains immutable prior
model/data pairs for historical SHAP auditability. These derived reference
assets are not rolling operational SQL history.

Rebuild from the genuine original benchmark files:

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python3 perf-anomaly-demo/sysinsight_shap.py train
```

No training happens on HTTP requests or during alert analysis. An alert runs
only bounded inference in a separate process, and unavailable models or failed
checks leave explicit missing evidence instead of fabricating attributions.

Low-confidence, unsupported-workload/phase or out-of-range attributions are
display-only. New LLM prompts receive the exclusion reason, not numerical
predictions or SHAP directions. Existing source/perf/replay tuning continues.
This is an advisory-evidence gate, not a new human-approval step for tuning.
