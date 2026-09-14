#!/usr/bin/env python3
"""Prometheus input adapter for the PostgreSQL SysInsight pipeline.

The monitoring stack exposes Prometheus as the source of truth for host and
PostgreSQL observations.  This module keeps the wire format of Prometheus
separate from the input format consumed by the SysInsight wrapper.  It is
deliberately read-only: it never changes Prometheus, PostgreSQL, or a
workload.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class PrometheusError(RuntimeError):
    """An HTTP, transport, or API-level Prometheus error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _number(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number):
        return value
    return number


def _promql_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class PrometheusClient:
    """Small dependency-free client for the Prometheus HTTP API."""

    def __init__(self, base_url: str = "http://127.0.0.1:9090", timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        query = urlencode({key: str(value) for key, value in params.items()})
        request = Request(
            "{}{}?{}".format(self.base_url, path, query),
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise PrometheusError("{} HTTP {}: {}".format(path, exc.code, detail)) from exc
        except URLError as exc:
            raise PrometheusError("{} unavailable: {}".format(path, exc.reason)) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PrometheusError("{} returned invalid JSON".format(path)) from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise PrometheusError("{} returned {}".format(path, raw[:1000]))
        return payload

    def query(self, expression: str, timestamp: Optional[float] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"query": expression}
        if timestamp is not None:
            params["time"] = timestamp
        return self._get("/api/v1/query", params)

    def query_range(
        self, expression: str, start: float, end: float, step: float = 15.0
    ) -> Dict[str, Any]:
        return self._get(
            "/api/v1/query_range",
            {"query": expression, "start": start, "end": end, "step": step},
        )

    def alerts(self) -> Dict[str, Any]:
        return self._get("/api/v1/alerts", {})

    def firing_alert(self, alert_name: str = "") -> Optional[Dict[str, Any]]:
        payload = self.alerts()
        alerts = payload.get("data", {}).get("alerts", [])
        for alert in alerts:
            if not isinstance(alert, dict) or alert.get("state") != "firing":
                continue
            labels = alert.get("labels", {})
            if alert_name and labels.get("alertname") != alert_name:
                continue
            return alert
        return None


def default_queries(database: str) -> Dict[str, str]:
    """Return queries matching the metrics provisioned in this checkout."""

    db = _promql_label(database)
    selector = 'job="postgres",datname="{}"'.format(db)
    return {
        "active_sessions": 'pg_stat_activity_count{{{},state="active"}}'.format(selector),
        # postgres_exporter exposes activity grouped by state, but not the
        # per-backend wait_event_type label.  Keep this as a real exported
        # state rather than emitting a query that Prometheus would accept but
        # never match.
        "idle_in_transaction_sessions": 'pg_stat_activity_count{{{},state="idle in transaction"}}'.format(selector),
        "transactions_committed": "pg_stat_database_xact_commit{{{}}}".format(selector),
        "transactions_rolled_back": "pg_stat_database_xact_rollback{{{}}}".format(selector),
        "tuples_returned": "pg_stat_database_tup_returned{{{}}}".format(selector),
        "tuples_fetched": "pg_stat_database_tup_fetched{{{}}}".format(selector),
        "blocks_read": "pg_stat_database_blks_read{{{}}}".format(selector),
        "blocks_hit": "pg_stat_database_blks_hit{{{}}}".format(selector),
        "temporary_files": "pg_stat_database_temp_files{{{}}}".format(selector),
        "temporary_bytes": "pg_stat_database_temp_bytes{{{}}}".format(selector),
        "locks": "pg_locks_count{{{}}}".format(selector),
        "database_size_bytes": "pg_database_size{{{}}}".format(selector),
        "cpu_percent": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m])))',
        "memory_used_percent": (
            '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
        ),
        "disk_read_bytes_per_second": "sum(rate(node_disk_read_bytes_total[1m]))",
        "disk_write_bytes_per_second": "sum(rate(node_disk_written_bytes_total[1m]))",
        "load1": "node_load1",
    }


def _series(result: Any) -> List[Dict[str, Any]]:
    if not isinstance(result, list):
        return []
    output: List[Dict[str, Any]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        labels = item.get("metric", {})
        values = item.get("values")
        if values is None and item.get("value") is not None:
            values = [item.get("value")]
        parsed_values: List[Dict[str, Any]] = []
        for pair in values or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            parsed_values.append({"timestamp": _number(pair[0]), "value": _number(pair[1])})
        output.append({"labels": labels if isinstance(labels, dict) else {}, "values": parsed_values})
    return output


def _query_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data", {})
    result = data.get("result", []) if isinstance(data, dict) else []
    return {
        "status": "completed",
        "result_type": data.get("resultType") if isinstance(data, dict) else None,
        "series": _series(result),
    }


def collect_window(
    client: PrometheusClient,
    database: str,
    start: float,
    end: float,
    step: float = 15.0,
    alert_name: str = "",
    queries: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Collect a complete, auditable Prometheus observation window.

    Individual metric failures are retained in the result so a missing custom
    exporter metric cannot be confused with a zero measurement.
    """

    expressions = dict(queries or default_queries(database))
    result: Dict[str, Any] = {
        "source": {"type": "prometheus", "url": client.base_url},
        "database": database,
        "window": {"start": start, "end": end, "step": step},
        "queries": {},
        "alerts": {"status": "not_collected", "selected": None, "all": []},
        "collected_at": utc_now(),
    }
    failures = 0
    for name, expression in expressions.items():
        try:
            result["queries"][name] = {
                "expression": expression,
                **_query_record(client.query_range(expression, start, end, step)),
            }
        except PrometheusError as exc:
            failures += 1
            result["queries"][name] = {
                "expression": expression,
                "status": "failed",
                "error": str(exc),
                "series": [],
            }
    try:
        alert_payload = client.alerts()
        alerts = alert_payload.get("data", {}).get("alerts", [])
        if not isinstance(alerts, list):
            alerts = []
        selected = None
        for alert in alerts:
            if not isinstance(alert, dict) or alert.get("state") != "firing":
                continue
            if not alert_name or alert.get("labels", {}).get("alertname") == alert_name:
                selected = alert
                break
        result["alerts"] = {"status": "completed", "selected": selected, "all": alerts}
    except PrometheusError as exc:
        failures += 1
        result["alerts"] = {"status": "failed", "error": str(exc), "selected": None, "all": []}
    result["status"] = "completed" if failures == 0 else "partial"
    result["failed_query_count"] = failures
    return result


def _case_window(case_result: Mapping[str, Any]) -> Tuple[float, float]:
    samples = case_result.get("anomaly", {}).get("samples", {})
    first = samples.get("first", {}) if isinstance(samples, dict) else {}
    last = samples.get("last", {}) if isinstance(samples, dict) else {}
    first_ts = first.get("ts") or first.get("timestamp")
    last_ts = last.get("ts") or last.get("timestamp")
    if isinstance(first_ts, (int, float)) and isinstance(last_ts, (int, float)):
        return float(first_ts), max(float(first_ts) + 1.0, float(last_ts))
    for key in ("observed_at", "started_at", "generated_at"):
        value = case_result.get(key)
        if isinstance(value, str):
            try:
                timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                return timestamp - 60.0, timestamp
            except ValueError:
                pass
    now = time.time()
    return now - 300.0, now


def _numeric_delta(first: Mapping[str, Any], last: Mapping[str, Any], key: str) -> Optional[float]:
    before, after = first.get(key), last.get(key)
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return float(after) - float(before)
    return None


def build_sysinsight_input(
    case_result: Mapping[str, Any],
    prometheus: Mapping[str, Any],
    profile: Mapping[str, Any],
    case_path: Optional[Path] = None,
    database_name: str = "keeninsight",
    database_schema: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the eight-section input contract used by the unified runner."""

    anomaly = case_result.get("anomaly", {})
    samples = anomaly.get("samples", {}) if isinstance(anomaly, dict) else {}
    first = samples.get("first", {}) if isinstance(samples, dict) else {}
    last = samples.get("last", {}) if isinstance(samples, dict) else {}
    query_records = prometheus.get("queries", {})
    trigger = samples.get("trigger") if isinstance(samples, dict) else None
    if trigger is None:
        trigger = prometheus.get("alerts", {}).get("selected")

    database_metrics: Dict[str, Any] = {}
    for key in (
        "transactions_committed",
        "transactions_rolled_back",
        "tuples_returned",
        "tuples_fetched",
        "blocks_read",
        "blocks_hit",
        "temporary_files",
        "temporary_bytes",
        "locks",
        "database_size_bytes",
    ):
        database_metrics[key] = query_records.get(key, {})

    host_metrics = {
        key: query_records.get(key, {})
        for key in (
            "cpu_percent",
            "memory_used_percent",
            "disk_read_bytes_per_second",
            "disk_write_bytes_per_second",
            "load1",
        )
    }
    source_detection = case_result.get("sysinsight_source_detection", {})
    if not isinstance(source_detection, dict) or not source_detection:
        # The API-validation wrapper stores the same original detector result
        # under anomaly, while the pre-API wrapper stores it at the case root.
        source_detection = anomaly.get("sysinsight_source_detection", {})
    source_compare = source_detection.get("source_compare", {}) if isinstance(source_detection, dict) else {}
    source_match = source_detection.get("source_match", {}) if isinstance(source_detection, dict) else {}
    anomaly_deltas = {
        key: _numeric_delta(first, last, key)
        for key in (
            "xact_commit",
            "xact_rollback",
            "tup_returned",
            "tup_fetched",
            "blks_read",
            "blks_hit",
            "temp_files",
            "temp_bytes",
            "checkpoint_write_time",
            "buffers_backend",
            "lock_waits",
        )
    }
    window = prometheus.get("window", {})
    if not isinstance(window, dict) or not isinstance(window.get("start"), (int, float)):
        case_start, case_end = _case_window(case_result)
        window = {"start": case_start, "end": case_end, "step": 15.0}
    workload = {
        "name": case_result.get("workload", "tpcc"),
        "benchmark": "TPCC" if str(case_result.get("id", "")).startswith(("tp_", "d", "c")) else "unknown",
        "case_id": case_result.get("id"),
        "title": case_result.get("title"),
        "mode": case_result.get("mode"),
        "external_event": case_result.get("external_event"),
    }
    schema_name = database_schema or case_result.get("database_schema") or "keeninsight_tpcc"

    steps: List[Dict[str, Any]] = [
        {"step": 1, "name": "environment", "status": "completed", "data": profile},
        {"step": 2, "name": "workload", "status": "completed", "data": workload},
        {
            "step": 3,
            "name": "time_window",
            "status": "completed",
            "data": {"prometheus": window, "case_sample_first": first.get("observed_at"), "case_sample_last": last.get("observed_at")},
        },
        {
            "step": 4,
            "name": "prometheus_alert",
            "status": "completed" if trigger else "not_triggered",
            "data": {"selected": trigger, "alert_status": prometheus.get("alerts", {}).get("status")},
        },
        {"step": 5, "name": "host_resources", "status": "completed", "data": host_metrics},
        {
            "step": 6,
            "name": "database_observations",
            "status": "completed",
            "data": {
                "prometheus": database_metrics,
                "case_snapshot_delta": anomaly_deltas,
                "slow_queries": case_result.get("slow_queries", []),
            },
        },
        {
            "step": 7,
            "name": "perf_function_anomalies",
            "status": "completed" if source_compare else "missing",
            "data": {
                "function_count": source_compare.get("key_function_count"),
                "key_functions": source_compare.get("key_functions", [])[:50],
                "matched_knobs": source_match.get("matched_knob", []),
            },
        },
        {
            "step": 8,
            "name": "tuning_context",
            "status": "completed",
            "data": {
                "baseline_metrics": case_result.get("baseline_metrics", {}),
                "anomaly_metrics": anomaly.get("metrics", {}),
                "metric_deltas": case_result.get("metric_deltas", {}),
                "before_settings": case_result.get("before_settings", {}),
                "profile": profile,
                "slow_queries": case_result.get("slow_queries", []),
            },
        },
    ]
    return {
        "schema": "sysinsight.postgresql.input.v1",
        "generated_at": utc_now(),
        "source_case_result": str(case_path) if case_path else None,
        "database": {
            "dbms": profile.get("name", "postgresql/12").split("/", 1)[0],
            "version": profile.get("name", "postgresql/12").split("/", 1)[-1],
            "name": database_name,
            "schema": schema_name,
        },
        "workload": workload,
        "time_window": window,
        "alert": trigger,
        "host_metrics": host_metrics,
        "database_metrics": database_metrics,
        "function_anomalies": steps[6]["data"],
        "tuning_context": steps[7]["data"],
        "prometheus_capture": prometheus,
        "steps": steps,
    }


def main() -> int:
    """Provide a small live adapter for monitoring-only validation."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--db", default="keeninsight")
    parser.add_argument("--alert-name", default="SysInsightDemoAnomaly")
    parser.add_argument("--window-seconds", type=float, default=300.0)
    parser.add_argument("--step", type=float, default=15.0)
    parser.add_argument("--wait", action="store_true", help="wait until the selected alert is firing")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    client = PrometheusClient(args.prometheus_url)
    alert = None
    if args.wait:
        while alert is None:
            try:
                alert = client.firing_alert(args.alert_name)
            except PrometheusError as exc:
                raise SystemExit(str(exc))
            if alert is None:
                time.sleep(max(0.2, args.poll_interval))
    else:
        try:
            alert = client.firing_alert(args.alert_name)
        except PrometheusError:
            alert = None
    end = time.time()
    payload = collect_window(
        client, args.db, end - args.window_seconds, end, args.step, args.alert_name
    )
    payload["trigger_mode"] = "wait_for_firing_alert" if args.wait else "snapshot"
    payload["trigger_alert"] = alert
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Prometheus 输入已生成：{}".format(output))
    print("告警：{}".format("firing" if alert else "未触发"))
    print("查询状态：{}".format(payload.get("status")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
