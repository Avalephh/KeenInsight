"""Versioned, read-only presentation model for the English KeenInsight UI.

The existing bridge remains the source of truth.  Supplementary evidence is
captured at completion so file cleanup cannot silently erase a result's meaning.
Unknown measurements stay null; never manufacture flamegraphs or confidence.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import socket
import threading
import time

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
VERSION = "keeninsight.ui.v1"
CATEGORIES = {
    "hosts": ("host-list.html", "Host identity, database, cluster, collection health"),
    "alerts": ("alert-list.html", "Alert trigger, raw state, diagnosis, apply/retest/restore evidence"),
    "profiles": ("alert-detail.html", "Differential profiling, SHAP, hot functions, runtime call chains"),
    "alertRules": ("alert-rules.html", "Actual loaded Prometheus alert expression and state"),
    "code": ("code-source.html", "Versioned PostgreSQL function/parameter/source-line associations"),
    "manual": ("manual.html", "Parameter descriptions, unit, context, defaults and limits"),
}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def decode(value, default=None):
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def clean(value):
    """Redact secrets even if a worker error embeds a header/configuration."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()
                if not re.search(r"api[_-]?key|authorization|password|secret|access[_-]?token", str(k), re.I)}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS keeninsight_evidence (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(entity_type, entity_id)
        );
        CREATE INDEX IF NOT EXISTS keeninsight_evidence_age
            ON keeninsight_evidence(captured_at);
    """)


def capture(conn, entity_type, entity_id, payload, captured_at=None):
    conn.execute("""INSERT INTO keeninsight_evidence VALUES(?,?,?,?,?)
        ON CONFLICT(entity_type,entity_id) DO UPDATE SET
        schema_version=excluded.schema_version,captured_at=excluded.captured_at,
        payload_json=excluded.payload_json""",
        (entity_type, entity_id, VERSION, captured_at or now(), json.dumps(clean(payload), ensure_ascii=False)))


def load_json(path, default=None):
    try:
        path = Path(path)
        if path.is_file() and path.stat().st_size <= 4 * 1024 * 1024:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    return default


def incident_evidence(directory, result):
    directory = Path(directory)
    # Only fixed artifact names; no path supplied by the browser is used.
    payload = load_json(directory / "sysinsight_input.json", {})
    chain = (result or {}).get("live_chain") or load_json(directory / "live_chain_result.json", {})
    if isinstance(chain.get("result"), dict):
        chain = chain["result"]
    return {"evidenceVersion": 2, "input": payload, "live_chain": chain, "result": result or {},
            "backendSnapshot": load_json(directory / "backend_snapshot.json", {}),
            "perfCapture": load_json(directory / "perf_capture.json", {}),
            "progress": load_json(directory / "workflow_progress.json", {}),
            "shap_analysis": load_json(directory / "shap_analysis.json", {}),
            "llm": load_json(directory / "sysinsight_api/result.json", {})}


def duration_ms(start, end):
    try:
        return round((dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)).total_seconds() * 1000, 3)
    except (TypeError, ValueError):
        return None


def prune(conn, cutoff):
    # Active alert workflows retain their supporting evidence.
    conn.execute("""DELETE FROM keeninsight_evidence WHERE captured_at < ?
        AND NOT (entity_type='alert' AND entity_id IN
            (SELECT incident_id FROM incidents WHERE status IN ('queued','running')))
        """, (cutoff,))


class KeenInsightPortal:
    def __init__(self, bridge):
        self.bridge = bridge
        self.store = bridge.store
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._host_metrics_cache = None
        self._backfill()

    def _backfill(self):
        """Bounded, idempotent migration of retained historical evidence."""
        with self.store._lock, self.store._connection() as conn:
            incidents = conn.execute("""SELECT i.*,e.payload_json AS retained_evidence FROM incidents i
                LEFT JOIN keeninsight_evidence e ON e.entity_type='alert' AND e.entity_id=i.incident_id
                WHERE e.entity_id IS NULL OR json_extract(e.payload_json,'$.evidenceVersion') IS NULL
                ORDER BY i.started_at DESC LIMIT 500""").fetchall()
            for row in incidents:
                old = decode(row['retained_evidence'], {})
                fresh = incident_evidence(row["directory"] or ROOT, decode(row["sysinsight_json"], {}))
                merged = {**old, **{key: value for key, value in fresh.items() if value or key not in old}}
                capture(conn, "alert", row["incident_id"], merged, row["finished_at"] or row["started_at"])

    def _rows(self, sql, params=()):
        with self.store._connection() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def _evidence(self, category, identity):
        rows = self._rows("SELECT payload_json FROM keeninsight_evidence WHERE entity_type=? AND entity_id=?",
                          (category, identity))
        return decode(rows[0]["payload_json"], {}) if rows else {}

    def schema(self):
        return {"schemaVersion": VERSION, "categories": [
            {"name": key, "page": page, "description": desc,
             "storage": {"profiles": "keeninsight_evidence",
                         "hosts": "runtime host identity",
                         "alerts": "incidents + keeninsight_evidence"}.get(
                             key, "versioned repository assets")}
            for key, (page, desc) in CATEGORIES.items()],
            "retention": {"runtimeHours": 24, "activeEvidenceRetained": True,
                          "referenceAssets": "version controlled"},
            "unknownMeasurements": None,
            "alertFields": ["id", "time", "host", "cluster", "reason", "severity", "status", "rawStatus",
                            "rootCause", "diagnosis", "detailAnalysis"],
            "provenanceFields": ["schemaVersion", "source", "availability", "updatedAt"]}

    def hosts(self):
        latest = self.store.get_meta("latest_collection", {}) or {}
        collected = latest.get("collected_at")
        age = None
        if collected:
            try:
                age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(collected)).total_seconds()
            except ValueError:
                pass
        return [{"id": "local-postgresql", "name": socket.gethostname(), "ip": "127.0.0.1",
                 "dbType": "PostgreSQL", "database": self.bridge.args.db, "cluster": "local",
                 "status": "online" if age is not None and age < 60 else "stale",
                 "updatedAt": collected, "collectionAgeSeconds": age,
                 "collectionError": self.bridge.last_collection_error,
                 "source": "bridge collector", "port": self.bridge.args.port,
                 "utilization": self._host_utilization()}]

    def _host_utilization(self):
        """One cached instant-vector query for the original host-list usage bars.

        No new time-series endpoint, synthetic values, or database queries.
        This deployment's node exporter is explicitly scoped to the local host.
        """
        with self._cache_lock:
            if self._host_metrics_cache and time.monotonic() - self._host_metrics_cache[0] < 15:
                return self._host_metrics_cache[1]
            selector = 'job="node",instance="127.0.0.1:9100"'
            expressions = {
                "cpuPercent": '100 * (1 - avg by(job,instance)(rate(node_cpu_seconds_total{' + selector + ',mode="idle"}[1m])))',
                "memoryPercent": '100 * (1 - node_memory_MemAvailable_bytes{' + selector + '} / node_memory_MemTotal_bytes{' + selector + '})',
                "diskPercent": '100 * (1 - node_filesystem_avail_bytes{' + selector + ',mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{' + selector + ',mountpoint="/",fstype!~"tmpfs|overlay"})',
            }
            combined = " or ".join('label_replace((' + expression + '),"ki_metric","' + key + '","","")'
                                   for key, expression in expressions.items())
            query = '(' + combined + ') and on(job,instance) (up{' + selector + '} == 1)'
            record = {key: None for key in expressions}
            record.update({"source": "Prometheus node exporter instant vector", "scope": "whole host; disk=root filesystem",
                           "queriedAt": now(), "status": "unavailable", "error": None})
            try:
                values = self.bridge.prometheus.query(query).get("data", {}).get("result", [])
                for item in values:
                    key = item.get("metric", {}).get("ki_metric")
                    value = float(item["value"][1])
                    if key in expressions and math.isfinite(value) and 0 <= value <= 100:
                        record[key] = value
                record["status"] = "recorded" if all(record[key] is not None for key in expressions) else "partial"
            except Exception as exc:
                record["error"] = str(exc)
            self._host_metrics_cache = (time.monotonic(), record)
            return record

    @staticmethod
    def _matches(record, filters):
        """Original-page filters apply before pagination, not just to visible rows."""
        for key, value in filters.items():
            if not value:
                continue
            if key == "duration":
                elapsed = record.get("executionTime")
                if elapsed is None or not {"fast": elapsed < 100,
                                           "medium": 100 <= elapsed <= 1000,
                                           "slow": elapsed > 1000}.get(value, False):
                    return False
            elif key == "ip":
                if value.lower() not in str(record.get(key) or "").lower():
                    return False
            elif str(record.get(key) or "").lower() != str(value).lower():
                return False
        return True

    def alerts(self, limit, offset, search, filters=None):
        where = "WHERE alert_name LIKE ? OR incident_id LIKE ? OR alert_json LIKE ?"
        pattern = "%" + search + "%"
        if filters:
            rows = self._rows("SELECT * FROM incidents " + where + " ORDER BY started_at DESC",
                              (pattern, pattern, pattern))
            records = [self._alert(row, False) for row in rows]
            records = [record for record in records if self._matches(record, filters)]
            return {"items": records[offset:offset+limit], "total": len(records)}
        rows = self._rows("SELECT * FROM incidents " + where + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                          (pattern, pattern, pattern, limit, offset))
        total = self._rows("SELECT count(*) n FROM incidents " + where, (pattern, pattern, pattern))[0]["n"]
        return {"items": [self._alert(row, False) for row in rows], "total": total}

    def _alert(self, row, detail=True, full=False):
        alert = decode(row.get("alert_json"), {})
        result = decode(row.get("sysinsight_json"), {})
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        record = {"id": row["incident_id"], "time": row["started_at"], "finishedAt": row.get("finished_at"),
                  "host": labels.get("instance") or socket.gethostname(), "cluster": "local",
                  "reason": annotations.get("summary") or row.get("alert_name"),
                  "severity": labels.get("severity", "info"), "rawStatus": row["status"],
                  "status": row["status"], "source": "incidents", "rootCause": [], "diagnosis": {},
                  "detailAnalysis": None, "error": result.get("error"), "schemaVersion": VERSION}
        chain = (result.get('live_chain') or {}).get('result') or result.get('live_chain') or {}
        evidence = {}
        if detail:
            evidence = self._evidence("alert", row["incident_id"])
            # A retained running snapshot cannot describe later stages. Read
            # only this incident's fixed artifacts until the final transaction.
            if row['status'] in {'running', 'queued'}:
                evidence = incident_evidence(row.get('directory') or ROOT, result)
        inputs = evidence.get("input") or {}
        chain = evidence.get("live_chain") or chain
        if isinstance(chain.get("result"), dict):
            chain = chain["result"]
        record["error"] = record.get("error") or chain.get("error")
        record['message'] = result.get('explanation') or result.get('reason') or chain.get('reason')
        source = chain.get("source_evidence") or inputs.get("source_evidence") or {}
        detection = chain.get("source_detection") or {}
        shap_analysis = chain.get('shap_analysis') or detection.get('shap') or evidence.get('shap_analysis') or None
        compare = detection.get('source_compare') or {}
        match = detection.get('source_match') or {}
        hot_functions = compare.get('key_functions') or []
        record['rootCause'] = [f.get('Function') for f in hot_functions[:5] if f.get('Function')]
        api = chain.get('api') or {}
        record['diagnosis'] = {**(result.get('analysis') or {}),
            'model': api.get('model') or (result.get('analysis') or {}).get('model'),
            'llmCallCount': api.get('llm_call_count'), 'candidateCount': api.get('candidate_count'),
            'hotFunctionCount': compare.get('key_function_count'),
            'matchedParameters': match.get('matched_knob') or [],
            'explanation': record['message'] or ('Measured differential profiling and source associations guide the LLM candidate; associations are not proof of causation.' if detection else 'Awaiting measured profiling/source evidence.'),
        }
        record['comparison'] = chain.get('comparison') or {}
        record['applicationStatus'] = (chain.get('temporary_application') or {}).get('status')
        record['durationMs'] = duration_ms(row.get('started_at'), row.get('finished_at'))
        record['triggerState'] = alert.get('state')
        if not detail:
            return record
        progress = evidence.get('progress') or {}
        running_stages = [stage for stage in progress.get('stages', []) if stage.get('status')=='running']
        if running_stages:
            record['diagnosis']['currentStage'] = running_stages[-1]['name']
        record.update({
                       "detailAnalysis": {
                           "triggeredRule": {"name": row.get("alert_name"), "actual": alert.get("value"),
                                             "state": alert.get("state"), "labels": labels,
                                             "description": annotations.get("description")},
                           "runtimeData": {"host": inputs.get("host_metrics") or {},
                                           "database": inputs.get("database_metrics") or {}},
                           "differentialProfiling": detection.get("source_compare") or None,
                           "shapAnalysis": shap_analysis,
                           "hotFunctions": (detection.get("source_compare") or {}).get("key_functions") or [],
                           "knobs": chain.get("candidate_validation") or {},
                           "runtimeCallChains": source.get("runtime_call_chains") or [],
                           "functionCodeExamples": source.get("static_parameter_evidence") or [],
                           "comparison": chain.get("comparison") or {},
                           "application": chain.get("temporary_application") or {},
                           "stages": chain.get("stages") or progress.get('stages') or [],
                           "backendSnapshot": evidence.get('backendSnapshot') or {},
                           "perfCapture": chain.get('perf') or evidence.get('perfCapture') or {},
                           "llm": api,
                           "validationSql": chain.get('validation_sql') or {},
                       }, "evidence": evidence,
                       "availability": {"shapAnalysis": "recorded" if (shap_analysis or {}).get('status') == 'completed' else (shap_analysis or {}).get('reason') or "not collected",
                                        "profiling": "recorded" if detection else "no profiler artifact retained"}})
        if not full:
            analysis = record.get("detailAnalysis") or {}
            truncated = {}
            for key, limit in (("hotFunctions", 30), ("functionCodeExamples", 30), ("runtimeCallChains", 20)):
                values = analysis.get(key)
                if isinstance(values, list) and len(values) > limit:
                    analysis[key] = values[:limit]
                    truncated[key] = {"shown": limit, "total": len(values)}
            profiling = analysis.get("differentialProfiling")
            if isinstance(profiling, dict):
                key_functions = profiling.get("key_functions")
                if isinstance(key_functions, list) and len(key_functions) > 30:
                    profiling = dict(profiling)
                    profiling["key_functions"] = key_functions[:30]
                    profiling["key_function_count"] = profiling.get("key_function_count", len(key_functions))
                    profiling["truncated"] = {"shown": 30, "total": len(key_functions)}
                    analysis["differentialProfiling"] = profiling
                    truncated["differentialProfiling.key_functions"] = {"shown": 30, "total": len(key_functions)}
            if truncated:
                analysis["truncated"] = truncated
            record.pop("evidence", None)
            record["fullEvidenceUrl"] = "/api/v1/keeninsight/alerts/{}?full=1".format(row["incident_id"])
        return record

    def references(self, category):
        with self._cache_lock:
            if category in self._cache:
                return self._cache[category]
            values = []
            common = ROOT / "db_profiles/postgresql/common"
            if category == "manual":
                for name, item in (load_json(common / "parameter_metadata.json", {}) or {}).items():
                    values.append({"id": name, "source": "PostgreSQL parameter_metadata.json",
                                   "content": re.sub(r"<[^>]+>", "", item.get("description", "")),
                                   "title": name, "description": item.get("short"),
                                   "type": item.get("type"), "default": item.get("default"),
                                   "unit": item.get("unit"), "context": item.get("context"),
                                   "min": item.get("min"), "max": item.get("max"),
                                   "restart": item.get("restart") if item.get('restart') is not None else item.get('context') == 'postmaster',
                                   'availability': {'unit': 'recorded' if item.get('unit') else 'unitless or not applicable',
                                                    'range': 'numeric limits' if item.get('min') is not None else 'see parameter type/description; numeric limits not applicable'}})
            elif category == "code":
                for association in load_json(common / "function_parameter_association.json", []) or []:
                    for evidence in association.get("source_evidence", []):
                        values.append({"id": "code-" + hashlib.sha1(json.dumps(evidence,sort_keys=True).encode()).hexdigest()[:16],
                                       "name": evidence.get("function"), "db": "pg", "category": evidence.get("context"),
                                       "description": association.get("knob_name"), "functionFlow": None,
                                       "dataFlow": evidence.get("variable"), "codeSnippet": evidence.get("source_line"),
                                       "file": evidence.get("file"), "line": evidence.get("line"),
                                       "detail": association.get("association_method"),
                                       'availability': {'functionFlow': 'Static parameter/function association, not a runtime trace. Actual runtime chains are retained per alert.',
                                                        'dataFlow': 'recorded' if evidence.get('variable') else 'association has no variable-level evidence'},
                                       "provenance": association.get("source_provenance"), "source": "PostgreSQL source association"})
            self._cache[category] = values
            return values

    def experiment(self, row):
        result = decode(row.get('result_json'), {}) if 'result_json' in row else row.get('result') or {}
        config = decode(row.get('config_json'), {}) if 'config_json' in row else row.get('config') or {}
        tuning = result.get('tuning') or {}
        comparison = result.get('comparison') or {}
        ids = [item.get('incident_id') for item in (result.get('sysinsight_state') or {}).get('current_incidents', [])]
        return {'run_id': row['run_id'], 'kind': row['kind'], 'scenario_id': row['scenario_id'],
            'title': row.get('title'), 'status': row['status'], 'phase': row.get('phase'),
            'requested_at': row['requested_at'], 'started_at': row.get('started_at'), 'finished_at': row.get('finished_at'),
            'error': row.get('error'), 'config': config, 'comparison': comparison,
            'candidateSource': tuning.get('source'), 'candidateSourceType': tuning.get('source_type'),
            'tuningStatus': tuning.get('status'), 'application': tuning.get('application') or {},
            'incidentIds': ids,
            'phases': {name: {k: phase.get(k) for k in ['phase', 'duration_seconds', 'business_tps_summary', 'pressure_tps_summary']}
                       for name, phase in (result.get('phases') or {}).items()},
            'evidenceNote': 'This experiment replays a previously measured LLM system-parameter candidate. The separately linked alert workflow makes fresh LLM calls; they are different measurements.'}

    def overview(self):
        counts = {table: self._rows('SELECT count(*) n FROM ' + table)[0]['n']
                  for table in ('incidents', 'lab_runs')}
        statuses = {row['status']: row['n'] for row in self._rows('SELECT status,count(*) n FROM incidents GROUP BY status')}
        latest_alerts = [self._alert(row, False) for row in self._rows('SELECT * FROM incidents ORDER BY started_at DESC LIMIT 8')]
        runs = [self.experiment(row) for row in self._rows('SELECT * FROM lab_runs ORDER BY requested_at DESC LIMIT 12')]
        args = self.bridge.args
        return {'hosts': self.hosts(), 'counts': counts, 'workflowCounts': {'incidents': statuses},
            'recentRuns': runs, 'recentAlerts': latest_alerts,
            'automation': {'model': getattr(args, 'model', None),
                'sysinsightFullChain': getattr(args, 'sysinsight_full_chain', None),
                'currentAlertFiring': bool(getattr(self.bridge, 'last_alert_snapshot', None)),
                'collectionError': self.bridge.last_collection_error},
            'capabilities': [
                {'name': 'Alert → perf → source → LLM → apply/retest/restore', 'state': 'enabled' if getattr(args,'sysinsight_full_chain',False) else 'disabled', 'detail': 'Open an alert to inspect measured evidence and restoration, not just workflow status.'},
                {'name': 'SHAP attribution', 'state': 'experimental' if (ROOT/'models/sysinsight_pg12_tps/manifest.json').is_file() else 'unavailable', 'detail': 'Tree-path-dependent TreeSHAP of a local PG12 foreground-TPS-retention model, trained on measured cases. Prediction explanation, not causality or LLM reasoning. Per-alert version, validation and range warnings retained.'},
                {'name': 'Host / database / TPS monitoring', 'state': 'external', 'detail': 'Available in the existing Grafana dashboards; not duplicated here.'},
            ], 'schema': self.schema(), 'links': {'lab': '/lab', 'automation': '/ui'}}

    def alert_rules(self):
        try:
            payload = self.bridge.prometheus._get("/api/v1/rules", {"type": "alert"})
            values = []
            for group in payload.get("data", {}).get("groups", []):
                for rule in group.get("rules", []):
                    labels = rule.get("labels", {}) or {}
                    try:
                        drop_percent = float(labels.get("drop_percent"))
                    except (TypeError, ValueError):
                        drop_percent = None
                    values.append({"id": group["name"] + ":" + rule["name"], "name": rule["name"],
                                   "desc": rule.get("annotations", {}).get("description"), "reason": rule.get("annotations", {}).get("summary"),
                                   "alertField": rule.get("query"), "threshold": None,
                                   "metric": labels.get("metric"), "dropPercent": drop_percent,
                                   "period": rule.get("duration"), "state": rule.get("state"),
                                   "health": rule.get("health"), "lastError": rule.get("lastError"),
                                   "severity": labels.get("severity"),
                                   "source": "Prometheus loaded rules", "file": group.get("file")})
            return values
        except Exception as exc:
            raise RuntimeError("Prometheus rules unavailable: " + str(exc))

    def get(self, resource, query):
        def arg(name, default):
            return (query.get(name) or [default])[-1]
        try:
            limit = max(1, min(200, int(arg("limit", 50))))
            offset = max(0, min(1000000, int(arg("offset", 0))))
        except (TypeError, ValueError):
            raise ValueError("invalid pagination")
        search = str(arg("q", ""))[:500]
        full = str(arg("full", "0")).lower() in {"1", "true", "yes", "on"}
        parts = resource.split("/", 1)
        category = parts[0]
        identity = parts[1] if len(parts)>1 else ""
        allowed_filters = {"hosts": ("status", "dbType", "ip"), "alerts": ("status", "severity", "reason"),
                           "code": ("db", "category")}
        filters = {key: str(arg(key, ""))[:500] for key in allowed_filters.get(category, ()) if arg(key, "")}
        if category == "schema":
            data = self.schema()
        elif category == "hosts":
            hosts = self.hosts()
            if search:
                hosts = [host for host in hosts if search.lower() in json.dumps(host).lower()]
            hosts = [host for host in hosts if self._matches(host, filters)]
            data = {"items": hosts[offset:offset+limit], "total": len(hosts)}
        elif category == "alerts":
            if identity:
                rows = self._rows("SELECT * FROM incidents WHERE incident_id=?", (identity,))
                if not rows:
                    raise KeyError("alert not found")
                data = self._alert(rows[0], full=full)
            else:
                data = self.alerts(limit, offset, search, filters)
        elif category in {"manual", "code", "alertRules"}:
            values = self.references(category) if category in {"manual", "code"} else self.alert_rules()
            if search:
                values = [v for v in values if search.lower() in json.dumps(v,ensure_ascii=False).lower()]
            values = [v for v in values if self._matches(v, filters)]
            data = {"items": values[offset:offset+limit], "total": len(values)}
        elif category == "overview":
            data = self.overview()
        else:
            raise KeyError("unknown KeenInsight resource")
        return dict(clean(data), schemaVersion=VERSION, updatedAt=now(), limit=limit, offset=offset)
