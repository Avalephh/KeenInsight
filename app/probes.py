"""Evidence-driven adaptive eBPF probe selection.

The selector uses real event vectors from completed probe sessions and real
latency labels from workload runs whose timestamps overlap those sessions.
Lasso is used as a sparse selector; the selected set is persisted before the
monitor is restarted with only that set attached.  A lack of enough matched
observations is reported as a training error rather than converted into an
arbitrary probe list.
"""

from __future__ import annotations

import math
import json
import datetime
from typing import Any

from .db import Database


class ProbeSelectionError(RuntimeError):
    pass


def _number(value: Any) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def train_lasso_probe_selector(
    db: Database,
    *,
    min_sessions: int = 4,
    max_probes: int = 8,
    alpha: float = 0.001,
    max_event_rate: float = 1000.0,
) -> dict[str, Any]:
    """Fit a sparse model and enforce a measured event-rate budget.

    Lasso ranks diagnostic importance, while the second constraint keeps a
    high-frequency user-space probe from being attached merely because it is
    predictive.  The rate is computed from persisted event counts and event
    timestamps, not from a configured or synthetic workload estimate.  The
    kernel ``vfs_write`` probe remains a mandatory low-cardinality resource
    signal and is not counted as an optional user-space probe.
    """
    try:
        import numpy as np
        from sklearn.linear_model import Lasso
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        raise ProbeSelectionError("adaptive probe selection requires numpy and scikit-learn: %s" % exc) from exc
    sessions = db.fetch_all(
        """
        SELECT id, started_at, stopped_at, target_pids, selected_probes
        FROM keeninsight.probe_sessions
        WHERE status = 'stopped' AND started_at IS NOT NULL AND stopped_at IS NOT NULL
        ORDER BY started_at ASC
        """
    )
    observations: list[dict[str, Any]] = []
    observed_event_rates: dict[str, float] = {}
    for session in sessions:
        run = db.fetch_one(
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
        latency = _number((run.get("observations") or {}).get("median_query_time_ms"))
        if latency <= 0:
            continue
        events = db.fetch_all(
            """
            SELECT function_name, observed_at, duration_ns, bytes, payload
            FROM keeninsight.ebpf_events WHERE session_id = %s
            """,
            (session["id"],),
        )
        vector: dict[str, float] = {}
        session_function_counts: dict[str, float] = {}
        session_function_times: dict[str, list[datetime.datetime]] = {}
        for event in events:
            payload = event.get("payload") or {}
            duration = _number(event.get("duration_ns")) or _number(payload.get("duration_ns_total"))
            function_name = str(event.get("function_name") or "unknown")
            vector[function_name] = vector.get(function_name, 0.0) + duration
            try:
                count = max(0.0, float(payload.get("count", 1) or 0.0))
            except (TypeError, ValueError):
                count = 1.0
            session_function_counts[function_name] = session_function_counts.get(function_name, 0.0) + count
            observed_at = event.get("observed_at")
            if isinstance(observed_at, datetime.datetime):
                session_function_times.setdefault(function_name, []).append(observed_at)
        base_session_seconds = max(
            0.001,
            _number(
                (session.get("stopped_at") - session.get("started_at")).total_seconds()
                if isinstance(session.get("stopped_at"), datetime.datetime)
                and isinstance(session.get("started_at"), datetime.datetime)
                else 0.0
            ),
        )
        for function_name, count in session_function_counts.items():
            timestamps = session_function_times.get(function_name) or []
            function_seconds = base_session_seconds
            if len(timestamps) >= 2:
                function_seconds = max(
                    0.001,
                    (max(timestamps) - min(timestamps)).total_seconds(),
                )
            rate = count / function_seconds
            observed_event_rates[function_name] = max(
                observed_event_rates.get(function_name, 0.0), rate
            )
        if vector:
            observations.append({"session": session, "run": run, "vector": vector, "latency_ms": latency})
    if len(observations) < max(3, int(min_sessions)):
        raise ProbeSelectionError(
            "adaptive probe selection needs at least %d completed probe sessions matched to workload runs; found %d"
            % (max(3, int(min_sessions)), len(observations))
        )
    functions = sorted({name for item in observations for name in item["vector"]})
    if len(functions) < 2:
        raise ProbeSelectionError("at least two observed probe functions are required for sparse selection")
    matrix = np.asarray([[item["vector"].get(name, 0.0) for name in functions] for item in observations], dtype=float)
    target = np.asarray([item["latency_ms"] for item in observations], dtype=float)
    scaler = StandardScaler()
    scaled = scaler.fit_transform(matrix)
    model = Lasso(
        alpha=max(float(alpha), 1e-12),
        max_iter=100000,
        tol=1e-6,
        random_state=None,
    )
    model.fit(scaled, target)
    coefficients = model.coef_ / np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    importance = np.abs(coefficients)
    ranked = sorted(range(len(functions)), key=lambda index: float(importance[index]), reverse=True)
    nonzero = [index for index in ranked if float(importance[index]) > 1e-12]
    if not nonzero:
        raise ProbeSelectionError("Lasso selected no non-zero probe coefficients for the observed workload")
    budget = max(1.0, float(max_event_rate))
    budget_rejected = [
        functions[index]
        for index in nonzero
        if functions[index] != "vfs_write"
        and observed_event_rates.get(functions[index], 0.0) > budget
    ]
    selected_indexes = []
    for index in nonzero:
        function_name = functions[index]
        # vfs_write is a mandatory kprobe and is attached independently by
        # EbpfMonitor; do not spend the optional user-space probe budget on it.
        if function_name != "vfs_write" and observed_event_rates.get(function_name, 0.0) > budget:
            continue
        selected_indexes.append(index)
        if len(selected_indexes) >= max(1, min(int(max_probes), len(nonzero))):
            break
    if not selected_indexes:
        # Keep the selector operational when all learned functions are hot:
        # attach the least-rate informative function and expose the budget
        # exception to the operator instead of silently inventing a probe.
        fallback = min(
            nonzero,
            key=lambda index: observed_event_rates.get(functions[index], 0.0),
        )
        selected_indexes = [fallback]
    selected_names = [functions[index] for index in selected_indexes]
    for index, function in enumerate(functions):
        db.execute(
            """
            UPDATE keeninsight.probe_catalog
            SET lasso_coefficient = %s, importance = %s, selected = %s, updated_at = now()
            WHERE function_name = %s
            """,
            (float(coefficients[index]), float(importance[index]), index in selected_indexes, function),
        )
    result = {
        "method": "StandardScaler + Lasso",
        "alpha": float(alpha),
        "sample_count": len(observations),
        "feature_names": functions,
        "target": "median_query_time_ms from matched workload_runs",
        "selected_probes": selected_names,
        "selection_budget": {
            "max_event_rate_per_user_probe": budget,
            "observed_event_rates": {
                name: float(observed_event_rates.get(name, 0.0)) for name in functions
            },
            "budget_rejected_probes": budget_rejected,
            "budget_unit": "persisted eBPF invocations per second; vfs_write is mandatory kernel signal",
        },
        "coefficients": {
            name: {"coefficient": float(coefficients[index]), "importance": float(importance[index]), "selected": index in selected_indexes}
            for index, name in enumerate(functions)
        },
        "matched_sessions": [
            {"session_id": int(item["session"]["id"]), "workload_run_id": int(item["run"]["id"]), "latency_ms": item["latency_ms"]}
            for item in observations
        ],
    }
    return result
