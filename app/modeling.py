"""Data-backed predictive and attribution analysis.

The paper describes a latency model, anomaly/root-cause modelling and SHAP
explanations.  This module deliberately trains only from observations that
were persisted by the PostgreSQL workload runner or the eBPF collector.  It
does not generate a synthetic training set and it never falls back to a
hand-written prediction when the optional ML stack is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .diagnose import explain_query, flatten_plan, validate_read_only_query


class ModelingError(RuntimeError):
    """The requested model operation cannot be performed with live evidence."""


FEATURE_NAMES = [
    "query_length",
    "plan_node_count",
    "plan_startup_cost",
    "plan_total_cost",
    "plan_rows",
    "plan_width",
    "estimated_rows_sum",
    "seq_scan_count",
    "index_scan_count",
    "bitmap_scan_count",
    "hash_join_count",
    "merge_join_count",
    "nested_loop_count",
    "sort_count",
    "aggregate_count",
    "workers_planned",
    "parallel_aware_count",
    "relation_count",
    "work_mem_kb",
    "parallel_workers_setting",
    "has_order_or_group",
]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _size_kb(value: Any) -> float:
    text = str(value or "").strip().lower()
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(b|kb|mb|gb|tb)?$", text)
    if not match:
        return 0.0
    amount = float(match.group(1))
    unit = match.group(2) or "kb"
    factors = {"b": 1 / 1024.0, "kb": 1.0, "mb": 1024.0, "gb": 1024.0 * 1024.0, "tb": 1024.0 * 1024.0 * 1024.0}
    return amount * factors[unit]


def _plan_from_observations(observations: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = observations.get("plan") or {}
    if not isinstance(plan, dict):
        return {}, []
    root = plan.get("root") or plan.get("document", {}).get("Plan") or {}
    nodes = plan.get("nodes") or flatten_plan(root)
    return root if isinstance(root, dict) else {}, [node for node in nodes if isinstance(node, dict)]


def extract_features(
    observations: dict[str, Any], *, query: str | None = None, settings: dict[str, Any] | None = None
) -> dict[str, float]:
    """Extract only plan/configuration features available before execution.

    Runtime-only fields such as temp blocks, actual hash batches and launched
    workers are intentionally excluded.  They remain evidence for the
    post-execution differential profiler, never inputs to this predictor.
    """
    root, nodes = _plan_from_observations(observations)
    query_text = query if query is not None else str(observations.get("query") or "")
    node_types = [str(node.get("Node Type") or "") for node in nodes]
    workers_planned = max([_number(node.get("Workers Planned")) for node in nodes] or [0.0])
    setting_map = settings or {}
    work_mem = setting_map.get("work_mem") or observations.get("phase_work_mem")
    parallel = setting_map.get("max_parallel_workers_per_gather") or observations.get("phase_parallel_workers")
    feature_values = {
        "query_length": float(len(query_text)),
        "plan_node_count": float(len(nodes)),
        "plan_startup_cost": _number(root.get("Startup Cost")),
        "plan_total_cost": _number(root.get("Total Cost")),
        "plan_rows": _number(root.get("Plan Rows")),
        "plan_width": _number(root.get("Plan Width")),
        "estimated_rows_sum": sum(_number(node.get("Plan Rows")) for node in nodes),
        "seq_scan_count": float(sum(1 for value in node_types if value == "Seq Scan")),
        "index_scan_count": float(sum(1 for value in node_types if "Index Scan" in value)),
        "bitmap_scan_count": float(sum(1 for value in node_types if "Bitmap" in value)),
        "hash_join_count": float(sum(1 for value in node_types if value == "Hash Join")),
        "merge_join_count": float(sum(1 for value in node_types if value == "Merge Join")),
        "nested_loop_count": float(sum(1 for value in node_types if value == "Nested Loop")),
        "sort_count": float(sum(1 for value in node_types if "Sort" in value)),
        "aggregate_count": float(sum(1 for value in node_types if "Aggregate" in value)),
        "workers_planned": workers_planned,
        "parallel_aware_count": float(sum(1 for node in nodes if node.get("Parallel Aware"))),
        "relation_count": float(len({str(node.get("Relation Name")) for node in nodes if node.get("Relation Name")})),
        "work_mem_kb": _size_kb(work_mem),
        "parallel_workers_setting": _number(parallel),
        "has_order_or_group": float(any(key in root for key in ("Sort Key", "Group Key", "Group Keys"))),
    }
    return {name: float(feature_values.get(name, 0.0)) for name in FEATURE_NAMES}


def _feature_vector(features: dict[str, Any]) -> list[float]:
    return [_number(features.get(name)) for name in FEATURE_NAMES]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class ModelManager:
    """Train/load models and persist every prediction/attribution."""

    def __init__(self, db: Database, model_dir: str | Path = "artifacts/models") -> None:
        self.db = db
        self.model_dir = Path(model_dir)
        self._lock = __import__("threading").RLock()

    def status(self) -> dict[str, Any]:
        rows = self.db.fetch_all(
            """
            SELECT id, model_type, algorithm, status, feature_names, target_name,
                   train_count, metrics, artifact_path, model_digest, created_at,
                   finished_at, error
            FROM keeninsight.model_runs ORDER BY created_at DESC LIMIT 20
            """
        )
        return {
            "model_dir": str(self.model_dir),
            "optional_dependencies": self._dependency_status(),
            "runs": rows,
            "latest_ready": next((row for row in rows if row.get("status") == "ready"), None),
        }

    @staticmethod
    def _dependency_status() -> dict[str, Any]:
        status = {}
        for module in ("xgboost", "shap", "numpy", "sklearn"):
            try:
                imported = __import__(module)
                status[module] = {"available": True, "version": getattr(imported, "__version__", "unknown")}
            except Exception as exc:
                status[module] = {"available": False, "error": str(exc)}
        return status

    def train(self, model_type: str = "latency_prediction", limit: int = 2000) -> dict[str, Any]:
        normalized_type = str(model_type or "latency_prediction").strip().lower()
        if normalized_type not in {"latency_prediction", "root_cause"}:
            raise ModelingError("model_type must be latency_prediction or root_cause")
        try:
            import numpy as np
            from sklearn.metrics import accuracy_score, mean_absolute_error, mean_squared_error, r2_score
            from sklearn.preprocessing import LabelEncoder
            from xgboost import XGBClassifier, XGBRegressor
        except Exception as exc:
            raise ModelingError("training requires numpy, scikit-learn, and xgboost: %s" % exc) from exc

        if normalized_type == "latency_prediction":
            rows = self._workload_rows(limit)
            excluded_probe_runs = self._excluded_probe_overlap_count(limit)
            data = []
            for row in rows:
                observations = row.get("observations") or {}
                target = _number(observations.get("median_query_time_ms"), -1.0)
                plan = observations.get("plan") or {}
                if target < 0 or not isinstance(plan, dict) or not plan.get("nodes"):
                    continue
                data.append((row, extract_features(observations), target))
            target_name = "median_query_time_ms"
        else:
            rows = self._workload_rows(limit)
            excluded_probe_runs = self._excluded_probe_overlap_count(limit)
            data = []
            for row in rows:
                observations = row.get("observations") or {}
                plan = observations.get("plan") or {}
                if not isinstance(plan, dict) or not plan.get("nodes") or not row.get("scenario"):
                    continue
                data.append((row, extract_features(observations), str(row["scenario"])))
            target_name = "scenario"
        if len(data) < 8:
            raise ModelingError("at least 8 completed, explainable workload observations are required; found %d" % len(data))

        self.model_dir.mkdir(parents=True, exist_ok=True)
        run_row = self.db.fetch_one(
            """
            INSERT INTO keeninsight.model_runs
                (model_type, algorithm, status, feature_names, target_name, train_count)
            VALUES (%s, %s, 'training', %s::jsonb, %s, %s)
            RETURNING id
            """,
            (normalized_type, "XGBoost", _json(FEATURE_NAMES), target_name, len(data)),
        )
        if not run_row:
            raise ModelingError("could not persist model run")
        run_id = int(run_row["id"])
        try:
            # Keep temporal ordering: old runs train the model and newer runs
            # are held out.  This prevents a random split from leaking a
            # repeated scenario's neighbouring measurements into validation.
            split = max(4, int(len(data) * 0.8))
            if split >= len(data):
                split = len(data) - 2
            X = np.asarray([_feature_vector(item[1]) for item in data], dtype=float)
            X_train, X_test = X[:split], X[split:]
            artifact = self.model_dir / ("%s_xgb_%s.json" % (normalized_type, run_id))
            if normalized_type == "latency_prediction":
                y = np.asarray([float(item[2]) for item in data], dtype=float)
                model = XGBRegressor(
                    n_estimators=160,
                    max_depth=4,
                    learning_rate=0.05,
                    subsample=0.9,
                    colsample_bytree=0.9,
                    objective="reg:squarederror",
                    random_state=17,
                    n_jobs=1,
                )
                model.fit(X_train, y[:split], verbose=False)
                predictions = model.predict(X_test)
                metrics = {
                    "rmse_ms": float(mean_squared_error(y[split:], predictions, squared=False)),
                    "mae_ms": float(mean_absolute_error(y[split:], predictions)),
                    "r2": float(r2_score(y[split:], predictions)) if len(y[split:]) > 1 else 0.0,
                    "train_count": int(len(X_train)),
                    "test_count": int(len(X_test)),
                    "target_min_ms": float(min(y)),
                    "target_max_ms": float(max(y)),
                    "split": "temporal_80_20",
                    "excluded_probe_overlap_runs": int(excluded_probe_runs),
                    "training_observation_policy": "completed workload runs not overlapping an eBPF probe session",
                }
            else:
                labels = LabelEncoder()
                y = labels.fit_transform([str(item[2]) for item in data])
                if len(labels.classes_) < 2:
                    raise ModelingError("root-cause model needs at least two observed scenario classes")
                # XGBoost's multiclass objective requires the class count in
                # the estimator.  The observed labels, not a fixed list, set it.
                model = XGBClassifier(
                    n_estimators=120,
                    max_depth=4,
                    learning_rate=0.06,
                    subsample=0.9,
                    colsample_bytree=0.9,
                    objective="multi:softprob",
                    num_class=len(labels.classes_),
                    eval_metric="mlogloss",
                    random_state=17,
                    n_jobs=1,
                )
                model.fit(X_train, y[:split], verbose=False)
                predicted = model.predict(X_test).astype(int)
                metrics = {
                    "accuracy": float(accuracy_score(y[split:], predicted)),
                    "class_names": [str(item) for item in labels.classes_],
                    "train_count": int(len(X_train)),
                    "test_count": int(len(X_test)),
                    "split": "temporal_80_20",
                    "excluded_probe_overlap_runs": int(excluded_probe_runs),
                    "training_observation_policy": "completed workload runs not overlapping an eBPF probe session",
                }
                metrics["label_encoder"] = [str(item) for item in labels.classes_]
            model.save_model(str(artifact))
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            updated = self.db.fetch_one(
                """
                UPDATE keeninsight.model_runs
                SET status = 'ready', metrics = %s::jsonb, artifact_path = %s,
                    model_digest = %s, finished_at = now()
                WHERE id = %s
                RETURNING id, model_type, algorithm, status, feature_names,
                          target_name, train_count, metrics, artifact_path,
                          model_digest, created_at, finished_at, error
                """,
                (_json(metrics), str(artifact), digest, run_id),
            )
            return {"run": updated, "metrics": metrics, "dependency_status": self._dependency_status()}
        except Exception as exc:
            self.db.execute(
                "UPDATE keeninsight.model_runs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                (str(exc), run_id),
            )
            if isinstance(exc, ModelingError):
                raise
            raise ModelingError("model training failed: %s" % exc) from exc

    def predict(self, query: str, *, model_type: str = "latency_prediction") -> dict[str, Any]:
        normalized = validate_read_only_query(query)
        model_rows = self.db.fetch_all(
            """
            SELECT id, model_type, algorithm, status, feature_names, target_name,
                   train_count, metrics, artifact_path, model_digest
            FROM keeninsight.model_runs
            WHERE model_type = %s AND status = 'ready'
            ORDER BY created_at DESC LIMIT 20
            """,
            (model_type,),
        )
        model_row = next(
            (row for row in model_rows if list(row.get("feature_names") or []) == FEATURE_NAMES),
            None,
        )
        if not model_row or not model_row.get("artifact_path"):
            raise ModelingError(
                "no ready compatible %s model; retrain it from persisted workload observations first"
                % model_type
            )
        artifact = Path(str(model_row["artifact_path"]))
        if not artifact.is_file():
            raise ModelingError("model artifact is missing: %s" % artifact)
        try:
            import numpy as np
            from xgboost import XGBClassifier, XGBRegressor
        except Exception as exc:
            raise ModelingError("prediction requires numpy and xgboost: %s" % exc) from exc
        plan = explain_query(self.db, normalized, analyze=False)
        setting_rows = self.db.fetch_all(
            """
            SELECT name, setting, COALESCE(unit, '') AS unit
            FROM pg_settings
            WHERE name IN ('work_mem', 'max_parallel_workers_per_gather')
            """
        )
        live_settings = {
            row["name"]: "%s%s" % (row.get("setting") or "", row.get("unit") or "")
            for row in setting_rows
        }
        observations = {"query": normalized, "plan": plan}
        features = extract_features(observations, settings=live_settings)
        vector = np.asarray([_feature_vector(features)], dtype=float)
        shap_class_index = 0
        if model_type == "latency_prediction":
            model = XGBRegressor(n_jobs=1)
            model.load_model(str(artifact))
            value = float(model.predict(vector)[0])
            prediction = {"median_query_time_ms": max(0.0, value)}
        else:
            model = XGBClassifier(n_jobs=1)
            model.load_model(str(artifact))
            probabilities = model.predict_proba(vector)[0]
            labels = (model_row.get("metrics") or {}).get("label_encoder") or []
            index = int(max(range(len(probabilities)), key=lambda item: probabilities[item]))
            shap_class_index = index
            prediction = {
                "root_cause": labels[index] if index < len(labels) else str(index),
                "probabilities": {
                    labels[item] if item < len(labels) else str(item): float(probabilities[item])
                    for item in range(len(probabilities))
                },
            }
        inserted = self.db.fetch_one(
            """
            INSERT INTO keeninsight.model_predictions
                (model_run_id, model_type, query, features, prediction)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING id, model_run_id, model_type, query, features, prediction, created_at
            """,
            (model_row["id"], model_type, normalized, _json(features), _json(prediction)),
        )
        attribution = self._shap_attribution(
            model,
            vector,
            model_row,
            inserted["id"] if inserted else None,
            class_index=shap_class_index,
        )
        return {
            "model_run": model_row,
            "prediction": prediction,
            "features": features,
            "attribution": attribution,
            "plan": plan,
            "prediction_record": inserted,
        }

    def screen(self, query: str, *, threshold_ms: float = 1000.0) -> dict[str, Any]:
        """Apply the plan-only latency screen before the query executes."""
        threshold = max(0.0, float(threshold_ms))
        result = self.predict(query, model_type="latency_prediction")
        predicted = float((result.get("prediction") or {}).get("median_query_time_ms") or 0.0)
        historical_matches = self._historical_query_matches(query, result.get("plan") or {})
        best_match = historical_matches[0] if historical_matches else None
        prediction_record_id = (result.get("prediction_record") or {}).get("id")
        if prediction_record_id:
            self.db.execute(
                """
                UPDATE keeninsight.model_predictions
                SET prediction = prediction || %s::jsonb
                WHERE id = %s
                """,
                (
                    _json(
                        {
                            "screen_threshold_ms": threshold,
                            "predicted_slow": predicted >= threshold,
                            "historical_matches": historical_matches,
                        }
                    ),
                    prediction_record_id,
                ),
            )
        return {
            "available": True,
            "method": "XGBoost pre-execution latency screening",
            "threshold_ms": threshold,
            "predicted_median_query_time_ms": predicted,
            "predicted_slow": predicted >= threshold,
            "model_run_id": (result.get("model_run") or {}).get("id"),
            "prediction_record_id": prediction_record_id,
            "attribution": (result.get("attribution") or [])[:10],
            "historical_matches": historical_matches,
            "reuse_candidate": (best_match or {}).get("action_proposal"),
        }

    def _shap_attribution(
        self,
        model: Any,
        vector: Any,
        model_row: dict[str, Any],
        prediction_id: int | None,
        *,
        class_index: int = 0,
    ) -> list[dict[str, Any]]:
        try:
            import numpy as np
            import shap

            explainer = shap.TreeExplainer(model)
            values = explainer.shap_values(vector)
            base = explainer.expected_value
            # Multiclass SHAP can return one matrix per class (older SHAP) or
            # a sample x feature x class tensor (newer SHAP).  Explain the
            # class actually selected by the classifier, not class zero.
            if isinstance(values, list):
                selected = min(max(int(class_index), 0), len(values) - 1)
                values = values[selected]
                base = base[selected] if isinstance(base, (list, tuple, np.ndarray)) else base
            values_array = np.asarray(values)
            if values_array.ndim == 3:
                selected = min(max(int(class_index), 0), values_array.shape[2] - 1)
                values_array = values_array[0, :, selected]
            elif values_array.ndim == 2:
                values_array = values_array[0]
            else:
                values_array = values_array.reshape(-1)
            base_array = np.asarray(base)
            if base_array.ndim > 0 and base_array.size > 1:
                base_value = float(base_array.reshape(-1)[min(max(int(class_index), 0), base_array.size - 1)])
            else:
                base_value = float(base_array.reshape(-1)[0])
            return self._persist_shap_rows(
                vector,
                FEATURE_NAMES,
                values_array,
                base_value,
                model_row,
                prediction_id,
                method="TreeSHAP",
                metadata={"model_type": model_row.get("model_type")},
            )
        except Exception as exc:
            raise ModelingError("TreeSHAP attribution failed: %s" % exc) from exc

    def _shap_attribution_for_names(
        self,
        model: Any,
        vector: Any,
        feature_names: list[str],
        model_row: dict[str, Any],
        prediction_id: int | None,
        *,
        method: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Explain a model whose feature names were learned from event data."""
        try:
            import numpy as np
            import shap

            explainer = shap.TreeExplainer(model)
            values = explainer.shap_values(vector)
            base = explainer.expected_value
            values_array = np.asarray(values)
            if values_array.ndim == 3:
                values_array = values_array[0, :, 0]
            elif values_array.ndim == 2:
                values_array = values_array[0]
            else:
                values_array = values_array.reshape(-1)
            base_array = np.asarray(base)
            base_value = float(base_array.reshape(-1)[0])
            return self._persist_shap_rows(
                vector,
                feature_names,
                values_array,
                base_value,
                model_row,
                prediction_id,
                method=method,
                metadata=metadata or {},
            )
        except Exception as exc:
            raise ModelingError("function-level TreeSHAP attribution failed: %s" % exc) from exc

    def _persist_shap_rows(
        self,
        vector: Any,
        feature_names: list[str],
        values_array: Any,
        base_value: float,
        model_row: dict[str, Any],
        prediction_id: int | None,
        *,
        method: str,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = []
        for name, value, contribution in sorted(
            zip(feature_names, vector[0], values_array),
            key=lambda item: abs(float(item[2])),
            reverse=True,
        ):
            item = {
                "feature_name": name,
                "feature_value": float(value),
                "contribution": float(contribution),
                "base_value": float(base_value),
                "method": method,
                **metadata,
            }
            rows.append(item)
            self.db.execute(
                """
                INSERT INTO keeninsight.model_attributions
                    (model_run_id, prediction_id, method, feature_name,
                     feature_value, contribution, base_value, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    model_row.get("id"),
                    prediction_id,
                    method,
                    name,
                    float(value),
                    float(contribution),
                    float(base_value),
                    _json(metadata),
                ),
            )
        return rows

    def attributions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT id, model_run_id, prediction_id, method, feature_name,
                   feature_value, contribution, base_value, metadata, created_at
            FROM keeninsight.model_attributions
            ORDER BY created_at DESC, abs(contribution) DESC LIMIT %s
            """,
            (max(1, min(int(limit), 1000)),),
        )

    def differential_profile(self, baseline_session_id: int, degraded_session_id: int) -> dict[str, Any]:
        baseline = self._event_vector(baseline_session_id)
        degraded = self._event_vector(degraded_session_id)
        functions = sorted(set(baseline) | set(degraded))
        rows = []
        for function in functions:
            left = baseline.get(function, {"count": 0.0, "duration_ns": 0.0, "bytes": 0.0})
            right = degraded.get(function, {"count": 0.0, "duration_ns": 0.0, "bytes": 0.0})
            rows.append(
                {
                    "function_name": function,
                    "baseline": left,
                    "degraded": right,
                    "delta": {key: float(right.get(key, 0.0) - left.get(key, 0.0)) for key in ("count", "duration_ns", "bytes")},
                    "relative_duration_change": float((right.get("duration_ns", 0.0) - left.get("duration_ns", 0.0)) / max(1.0, left.get("duration_ns", 0.0))),
                }
            )
        rows.sort(key=lambda item: abs(item["delta"]["duration_ns"]), reverse=True)
        evidence = {
            "method": "event-vector differential profiling",
            "baseline_session_id": baseline_session_id,
            "degraded_session_id": degraded_session_id,
            "functions": rows,
            "observed_event_count": sum(item["degraded"]["count"] for item in rows),
        }
        try:
            evidence["function_shap"] = self._function_shap(
                baseline_session_id, degraded_session_id
            )
        except ModelingError as exc:
            evidence["function_shap"] = {
                "available": False,
                "method": "XGBoost + TreeSHAP over function event vectors",
                "reason": str(exc),
            }
        inserted = self.db.fetch_one(
            """
            INSERT INTO keeninsight.differential_profiles
                (baseline_session_id, degraded_session_id, method, features)
            VALUES (%s, %s, 'eBPF event-vector differential profiling', %s::jsonb)
            ON CONFLICT (baseline_session_id, degraded_session_id, method)
            DO UPDATE SET features = EXCLUDED.features, created_at = now()
            RETURNING id, baseline_session_id, degraded_session_id, method, features, created_at
            """,
            (baseline_session_id, degraded_session_id, _json(evidence)),
        )
        return {"profile": inserted, **evidence}

    def _function_shap(
        self, baseline_session_id: int, degraded_session_id: int
    ) -> dict[str, Any]:
        """Fit and explain a function-level model over measured probe data.

        A sample is one stopped probe session matched by timestamp to a
        completed workload run.  Features are aggregated durations from the
        persisted eBPF/Kafka event stream and the target is that run's
        measured median latency.  Thus the hotspot explanation is learned
        from observed execution data rather than inserted as a scenario rule.
        """
        try:
            import numpy as np
            from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
            from xgboost import XGBRegressor
        except Exception as exc:
            raise ModelingError("function-level SHAP requires numpy, scikit-learn, and xgboost: %s" % exc) from exc

        observations = self._probe_workload_rows()
        if len(observations) < 4:
            raise ModelingError(
                "at least four completed probe sessions matched to workload runs are required; found %d"
                % len(observations)
            )
        session_ids = {int(item["session"]["id"]) for item in observations}
        if int(baseline_session_id) not in session_ids or int(degraded_session_id) not in session_ids:
            raise ModelingError("both requested sessions must have a measured workload match")
        functions = sorted({name for item in observations for name in item["vector"]})
        if len(functions) < 2:
            raise ModelingError("at least two observed functions are required for function-level SHAP")
        feature_names = ["function:%s:duration_ns" % name for name in functions]
        X = np.asarray(
            [
                [_number(item["vector"].get(name, {}).get("duration_ns")) for name in functions]
                for item in observations
            ],
            dtype=float,
        )
        y = np.asarray([float(item["latency_ms"]) for item in observations], dtype=float)
        if not np.isfinite(X).all() or not np.isfinite(y).all() or max(y) <= 0:
            raise ModelingError("function event vectors or latency labels are not finite")

        split = max(2, int(len(observations) * 0.8))
        if split >= len(observations):
            split = len(observations) - 1
        run_row = self.db.fetch_one(
            """
            INSERT INTO keeninsight.model_runs
                (model_type, algorithm, status, feature_names, target_name, train_count)
            VALUES ('function_hotspot', 'XGBoost + TreeSHAP', 'training', %s::jsonb,
                    'median_query_time_ms', %s)
            RETURNING id
            """,
            (_json(feature_names), len(observations)),
        )
        if not run_row:
            raise ModelingError("could not persist function-level model run")
        run_id = int(run_row["id"])
        self.model_dir.mkdir(parents=True, exist_ok=True)
        artifact = self.model_dir / ("function_hotspot_xgb_%s.json" % run_id)
        try:
            model = XGBRegressor(
                n_estimators=140,
                max_depth=3,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                objective="reg:squarederror",
                random_state=17,
                n_jobs=1,
                tree_method="hist",
            )
            model.fit(X[:split], y[:split], verbose=False)
            holdout = model.predict(X[split:])
            metrics = {
                "rmse_ms": float(mean_squared_error(y[split:], holdout, squared=False)),
                "mae_ms": float(mean_absolute_error(y[split:], holdout)),
                "r2": float(r2_score(y[split:], holdout)) if len(holdout) > 1 else 0.0,
                "train_count": int(split),
                "test_count": int(len(y) - split),
                "function_count": int(len(functions)),
                "split": "temporal_80_20",
                "matched_sessions": [int(item["session"]["id"]) for item in observations],
                "matched_workload_runs": [int(item["run"]["id"]) for item in observations],
                "target_min_ms": float(min(y)),
                "target_max_ms": float(max(y)),
            }
            model.save_model(str(artifact))
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            model_row = self.db.fetch_one(
                """
                UPDATE keeninsight.model_runs
                SET status = 'ready', metrics = %s::jsonb, artifact_path = %s,
                    model_digest = %s, finished_at = now()
                WHERE id = %s
                RETURNING id, model_type, algorithm, status, feature_names,
                          target_name, train_count, metrics, artifact_path,
                          model_digest, created_at, finished_at, error
                """,
                (_json(metrics), str(artifact), digest, run_id),
            )
        except Exception as exc:
            self.db.execute(
                "UPDATE keeninsight.model_runs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                (str(exc), run_id),
            )
            raise ModelingError("function-level model training failed: %s" % exc) from exc

        degraded_item = next(
            item for item in observations if int(item["session"]["id"]) == int(degraded_session_id)
        )
        baseline_item = next(
            item for item in observations if int(item["session"]["id"]) == int(baseline_session_id)
        )
        degraded_vector = np.asarray(
            [[_number(degraded_item["vector"].get(name, {}).get("duration_ns")) for name in functions]],
            dtype=float,
        )
        predicted = float(model.predict(degraded_vector)[0])
        prediction = self.db.fetch_one(
            """
            INSERT INTO keeninsight.model_predictions
                (model_run_id, model_type, query, features, prediction)
            VALUES (%s, 'function_hotspot', %s, %s::jsonb, %s::jsonb)
            RETURNING id, model_run_id, model_type, query, features, prediction, created_at
            """,
            (
                run_id,
                "session:%d" % int(degraded_session_id),
                _json({name: float(value) for name, value in zip(feature_names, degraded_vector[0])}),
                _json(
                    {
                        "predicted_median_query_time_ms": max(0.0, predicted),
                        "baseline_session_id": int(baseline_session_id),
                        "degraded_session_id": int(degraded_session_id),
                    }
                ),
            ),
        )
        attribution = self._shap_attribution_for_names(
            model,
            degraded_vector,
            feature_names,
            model_row or {},
            prediction["id"] if prediction else None,
            method="TreeSHAP:function_hotspot",
            metadata={
                "function_level": True,
                "baseline_session_id": int(baseline_session_id),
                "degraded_session_id": int(degraded_session_id),
            },
        )
        for item in attribution:
            item["function_name"] = item["feature_name"].split(":", 2)[1]
        return {
            "available": True,
            "method": "XGBoost + TreeSHAP over function event vectors",
            "model_run": model_row,
            "prediction": prediction,
            "baseline_latency_ms": float(baseline_item["latency_ms"]),
            "degraded_latency_ms": float(degraded_item["latency_ms"]),
            "top_functions": attribution[:10],
            "attribution": attribution,
        }

    def _event_vector(self, session_id: int) -> dict[str, dict[str, float]]:
        rows = self.db.fetch_all(
            """
            SELECT function_name, duration_ns, bytes, payload
            FROM keeninsight.ebpf_events WHERE session_id = %s
            """,
            (session_id,),
        )
        vector: dict[str, dict[str, float]] = {}
        for row in rows:
            name = str(row.get("function_name") or "unknown")
            payload = row.get("payload") or {}
            count = _number(payload.get("count"), 1.0)
            duration = _number(row.get("duration_ns"), _number(payload.get("duration_ns_total")))
            bytes_value = _number(row.get("bytes"), _number(payload.get("bytes_total")))
            item = vector.setdefault(name, {"count": 0.0, "duration_ns": 0.0, "bytes": 0.0})
            item["count"] += count
            item["duration_ns"] += duration
            item["bytes"] += bytes_value
        return vector

    def _workload_rows(self, limit: int) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT id, scenario, phase, status, parameters, observations,
                   started_at, finished_at
            FROM keeninsight.workload_runs
            WHERE status = 'completed'
              AND NOT EXISTS (
                    SELECT 1
                    FROM keeninsight.probe_sessions AS probe
                    WHERE probe.status IN ('starting', 'running', 'stopped')
                      AND probe.started_at IS NOT NULL
                      AND probe.started_at <= COALESCE(workload_runs.finished_at, now())
                      AND COALESCE(probe.stopped_at, now()) >= workload_runs.started_at
              )
            ORDER BY started_at ASC LIMIT %s
            """,
            (max(8, min(int(limit), 10000)),),
        )

    def _excluded_probe_overlap_count(self, limit: int) -> int:
        row = self.db.fetch_one(
            """
            SELECT count(*)::int AS count
            FROM (
                SELECT w.id
                FROM keeninsight.workload_runs AS w
                WHERE w.status = 'completed'
                  AND EXISTS (
                        SELECT 1
                        FROM keeninsight.probe_sessions AS probe
                        WHERE probe.status IN ('starting', 'running', 'stopped')
                          AND probe.started_at IS NOT NULL
                          AND probe.started_at <= COALESCE(w.finished_at, now())
                          AND COALESCE(probe.stopped_at, now()) >= w.started_at
                  )
                ORDER BY w.started_at ASC
                LIMIT %s
            ) AS excluded
            """,
            (max(8, min(int(limit), 10000)),),
        ) or {}
        return int(row.get("count") or 0)

    def _historical_query_matches(
        self, query: str, plan: dict[str, Any], *, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Find measured historical queries with lexical and plan similarity."""
        query_tokens = Counter(re.findall(r"[a-z_][a-z0-9_]*", query.lower()))
        query_nodes = {
            str(node.get("Node Type"))
            for node in (plan.get("nodes") or [])
            if node.get("Node Type")
        }
        matches = []
        for row in self._workload_rows(500):
            observations = row.get("observations") or {}
            historical_query = str(observations.get("query") or "")
            if not historical_query:
                continue
            historical_tokens = Counter(
                re.findall(r"[a-z_][a-z0-9_]*", historical_query.lower())
            )
            token_union = set(query_tokens) | set(historical_tokens)
            token_similarity = (
                sum(min(query_tokens[item], historical_tokens[item]) for item in token_union)
                / max(1, sum(max(query_tokens[item], historical_tokens[item]) for item in token_union))
            )
            historical_plan = observations.get("plan") or {}
            historical_nodes = {
                str(node.get("Node Type"))
                for node in (historical_plan.get("nodes") or [])
                if node.get("Node Type")
            }
            node_union = query_nodes | historical_nodes
            plan_similarity = (
                len(query_nodes & historical_nodes) / max(1, len(node_union))
            )
            similarity = 0.65 * token_similarity + 0.35 * plan_similarity
            matches.append(
                {
                    "run_id": int(row["id"]),
                    "scenario": row.get("scenario"),
                    "query": historical_query,
                    "similarity": float(similarity),
                    "token_similarity": float(token_similarity),
                    "plan_similarity": float(plan_similarity),
                    "median_query_time_ms": _number(
                        observations.get("median_query_time_ms")
                    ),
                    "action_proposal": observations.get("action_proposal"),
                    "evidence": "completed workload_run query, plan, and measured latency",
                }
            )
        matches.sort(key=lambda item: (item["similarity"], item["run_id"]), reverse=True)
        return matches[: max(1, min(int(limit), 10))]

    def _probe_workload_rows(self) -> list[dict[str, Any]]:
        """Pair stopped probe sessions with overlapping measured workloads."""
        sessions = self.db.fetch_all(
            """
            SELECT id, started_at, stopped_at, target_pids, selected_probes
            FROM keeninsight.probe_sessions
            WHERE status = 'stopped' AND started_at IS NOT NULL AND stopped_at IS NOT NULL
            ORDER BY started_at ASC
            """
        )
        observations = []
        for session in sessions:
            run = self.db.fetch_one(
                """
                SELECT id, scenario, observations, started_at, finished_at
                FROM keeninsight.workload_runs
                WHERE status = 'completed'
                  AND started_at <= %s AND finished_at >= %s
                ORDER BY abs(extract(epoch from (started_at - %s))) ASC LIMIT 1
                """,
                (session["stopped_at"], session["started_at"], session["started_at"]),
            )
            if not run:
                continue
            latency = _number((run.get("observations") or {}).get("median_query_time_ms"), -1.0)
            vector = self._event_vector(int(session["id"]))
            if latency <= 0 or not vector:
                continue
            observations.append(
                {
                    "session": session,
                    "run": run,
                    "vector": vector,
                    "latency_ms": latency,
                }
            )
        return observations
