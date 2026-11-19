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
from sql_profiling import digest as sql_digest
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
    "sql": ("sql-log.html", "Fingerprint statistics and separate measured execution records"),
    "diagnoses": ("sql-detail.html", "Root causes, explanation, rule candidates, model, plan"),
    "profiles": ("alert-detail.html", "Differential profiling, SHAP, hot functions, runtime call chains"),
    "tuning": ("sql-detail.html", "Candidate action, old/new timings, validation, activation and hits"),
    "alertRules": ("alert-rules.html", "Actual loaded Prometheus alert expression and state"),
    "diagnosisRules": ("dba-rules.html", "DREAM SQL rewrite rules from the runtime rule library"),
    "code": ("code-source.html", "Versioned PostgreSQL function/parameter/source-line associations"),
    "manual": ("manual.html", "Parameter descriptions, unit, context, defaults and limits"),
    "knowledge": ("tuning-knowledge.html", "Empirical measured improvements with source and validation"),
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
    # Active actions and queued/running owners retain their supporting evidence.
    conn.execute("""DELETE FROM keeninsight_evidence WHERE captured_at < ?
        AND NOT (entity_type='alert' AND entity_id IN
            (SELECT incident_id FROM incidents WHERE status IN ('queued','running')))
        AND NOT (entity_type='sql' AND entity_id IN
            (SELECT job_id FROM dream_jobs WHERE status IN ('queued','running')))
        AND NOT (entity_type='sql' AND EXISTS
            (SELECT 1 FROM improvements WHERE status='active'
             AND json_extract(validation_json,'$.job_id')=keeninsight_evidence.entity_id))
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
            jobs = conn.execute("""SELECT job_id,result_json,finished_at,requested_at FROM dream_jobs WHERE result_json IS NOT NULL
                AND job_id NOT IN (SELECT entity_id FROM keeninsight_evidence WHERE entity_type='sql')
                ORDER BY requested_at DESC LIMIT 500""").fetchall()
            incidents = conn.execute("""SELECT i.*,e.payload_json AS retained_evidence FROM incidents i
                LEFT JOIN keeninsight_evidence e ON e.entity_type='alert' AND e.entity_id=i.incident_id
                WHERE e.entity_id IS NULL OR json_extract(e.payload_json,'$.evidenceVersion') IS NULL
                ORDER BY i.started_at DESC LIMIT 500""").fetchall()
            for row in jobs:
                capture(conn, "sql", row["job_id"], decode(row["result_json"], {}), row["finished_at"] or row["requested_at"])
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
                         "diagnoses": "dream_jobs + keeninsight_evidence"}.get(key, {
                             "hosts": "runtime host identity", "alerts": "incidents + keeninsight_evidence",
                             "sql": "sql_observations + lab_sql_executions", "tuning": "improvements",
                             "knowledge": "improvements (measured)"}.get(key, "versioned repository assets"))}
            for key, (page, desc) in CATEGORIES.items()],
            "retention": {"runtimeHours": 24, "activeEvidenceRetained": True,
                          "referenceAssets": "version controlled"},
            "unknownMeasurements": None,
            "sqlLogKinds": {"observation": "aggregated pg_stat_statements fingerprint; not a single execution",
                            "execution": "individual measured lab execution, including failed executions"},
            "sqlFields": ["id", "time", "host", "database", "sql", "type", "executionTime", "hasError",
                          "result", "lifecycle", "flameGraph", "diagnosis_results", "tuning_results"],
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

    def _alert(self, row, detail=True):
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
        return record

    def sql_logs(self, limit, offset, search, kind="all", filters=None):
        # SQL observation totals are never represented as per-execution timings.
        clauses = "WHERE query_text LIKE ? OR sql_key LIKE ?"
        pattern = "%" + search + "%"
        observations = self._rows("SELECT sql_key AS id,last_seen AS time,'observation' AS kind,query_text AS sql,"
                                  "mean_time_ms AS executionTime,'observed' AS result FROM sql_observations "
                                  + clauses, (pattern, pattern))
        executions = self._rows("SELECT execution_id AS id,started_at AS time,'execution' AS kind,query_text AS sql,"
                               "duration_ms AS executionTime,status AS result FROM lab_sql_executions "
                               "WHERE query_text LIKE ? OR sql_id LIKE ?", (pattern, pattern))
        if kind not in {"all", "execution", "observation"}:
            raise ValueError("invalid SQL record kind")
        identities = sorted((observations if kind != "execution" else []) +
                            (executions if kind != "observation" else []),
                            key=lambda row: (row["kind"] == "execution", row["time"] or ""), reverse=True)
        if filters:
            for row in identities:
                row["type"] = (re.sub(r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*", "", row["sql"], flags=re.S).split() or [""])[0].upper()
            identities = [row for row in identities if self._matches(row, filters)]
        return {"items": [self.sql_detail(row["id"], brief=True) for row in identities[offset:offset+limit]],
                "total": len(identities)}

    def sql_detail(self, identity, brief=False, job_id=None):
        executions = self._rows("SELECT * FROM lab_sql_executions WHERE execution_id=?", (identity,))
        execution = executions[0] if executions else None
        if execution:
            runs = self._rows("SELECT result_json FROM lab_runs WHERE run_id=?", (execution["run_id"],))
            run = decode(runs[0]["result_json"], {}) if runs else {}
            key = run.get("sql_key")
            rows = self._rows("SELECT * FROM sql_observations WHERE sql_key=?", (key,)) if key else []
        else:
            rows = self._rows("SELECT * FROM sql_observations WHERE sql_key=?", (identity,))
        observation = rows[0] if rows else {}
        if not observation and not execution:
            raise KeyError("SQL record not found")
        key = observation.get("sql_key")
        jobs = (self._rows("SELECT * FROM dream_jobs WHERE sql_key=? AND job_id=?", (key, job_id)) if job_id else
                self._rows("SELECT * FROM dream_jobs WHERE sql_key=? ORDER BY requested_at DESC LIMIT 1", (key,))) if key else []
        improvements = self._rows("SELECT * FROM improvements WHERE sql_key=? ORDER BY created_at DESC LIMIT 50", (key,)) if key else []
        job = jobs[0] if jobs else {}
        if not job:
            # Active actions retain their original evidence even when the
            # 24-hour task history expires. Resolve by this SQL's validated
            # job ID, never by a global "latest" diagnosis or another SQL.
            for item in improvements:
                retained_id = decode(item.get('validation_json'), {}).get('job_id')
                if item.get('status') != 'active' or not retained_id or (job_id and retained_id != job_id):
                    continue
                if not self._evidence("sql", retained_id):
                    continue
                job = {'job_id': retained_id, 'status': 'history_expired',
                       'history_expired': True,
                       'history_note': '24-hour task history expired; original evidence is retained because its improvement is still active.'}
                break
        if job_id and not job:
            raise KeyError('DREAM job does not belong to this SQL')
        improvement = next((item for item in improvements if not job or
            decode(item.get('validation_json'), {}).get('job_id') in {None, job.get('job_id')}), {})
        # Keep the active published action distinct from a newer failed or
        # no_action diagnosis, rather than attributing old wins to a new job.
        active_rows = self._rows("SELECT * FROM improvements WHERE sql_key=? AND status='active' ORDER BY created_at DESC LIMIT 1", (key,)) if key else []
        active = active_rows[0] if active_rows else {}
        result = decode(job.get("result_json"), {})
        timing = execution.get("duration_ms") if execution else observation.get("mean_time_ms")
        text = execution["query_text"] if execution else observation.get("replay_sql") or observation.get("query_text", "")
        record = {"id": identity, "sqlKey": key, "kind": "execution" if execution else "observation",
                  "time": execution["started_at"] if execution else observation.get("last_seen"),
                  "host": socket.gethostname(), "database": observation.get("database_name") or self.bridge.args.db,
                  "sql": text, "type": (re.sub(r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*", "", text, flags=re.S).split() or [""])[0].upper(),
                  "executionTime": timing, "calls": 1 if execution else observation.get("calls"),
                  "hasError": bool(execution["status"] == "failed") if execution else None,
                  "result": execution["status"] if execution else "observed",
                  "jobStatus": job.get("status"), "jobId": job.get("job_id"),
                  "improvementStatus": improvement.get("status"), "improvementId": improvement.get("improvement_id"),
                  "phase": execution.get("phase") if execution else None,
                  'runId': execution.get('run_id') if execution else None,
                  "source": "lab_sql_executions" if execution else "pg_stat_statements",
                  "schemaVersion": VERSION}
        if brief:
            return record
        evidence = self._evidence("sql", job.get("job_id", ""))
        result = evidence or result
        evaluation = result.get("evaluation") or {}
        diagnosis = result.get("diagnosis_results") or {
            "query_id": key, "root_causes": result.get("root_causes") or decode(improvement.get("root_causes"), []) or [], "confidence": {},
            "explanation": result.get("prediction_explanation") or result.get("explanation") or "",
            "rule_matches_detail": [],
        }
        new_time = improvement.get("new_time", result.get('new_time'))
        old_time = improvement.get("old_time", result.get('old_time'))
        lifecycle = []
        execution_rows = self._rows("SELECT e.* FROM lab_sql_executions e JOIN lab_runs r ON r.run_id=e.run_id WHERE json_extract(r.result_json,'$.sql_key')=? ORDER BY e.started_at DESC LIMIT 50", (key,)) if key else ([execution] if execution else [])
        for item in reversed(execution_rows):
            lifecycle.append({'phase': item['phase'], 'status': item['status'],
                'startedAt': item['started_at'], 'finishedAt': item['finished_at'],
                'durationMs': item['duration_ms'], 'method': item['method'],
                'executionId': item['execution_id'], 'runId': item['run_id'], 'source': 'lab_sql_executions'})
        if job and not job.get('history_expired'):
            lifecycle.append({'phase': 'DREAM diagnosis and evaluation', 'status': job.get('status'),
                'startedAt': job.get('started_at'), 'finishedAt': job.get('finished_at'),
                'durationMs': duration_ms(job.get('started_at'), job.get('finished_at')),
                'method': result.get('api_model'), 'jobId': job.get('job_id'), 'source': 'dream_jobs'})
        lifecycle.sort(key=lambda item: item.get('startedAt') or '')
        profiles = self._rows("SELECT payload_json FROM sql_profiles WHERE execution_id=? ORDER BY started_at DESC LIMIT 5", (identity,)) if execution else self._rows(
            "SELECT payload_json FROM sql_profiles WHERE database_name=? AND sql_sha256=? ORDER BY started_at DESC LIMIT 5",
            (record["database"], sql_digest(observation.get("canonical_sql") or text)))
        profiles = [decode(item['payload_json'], {}) for item in profiles]
        selected_profile = next((item for item in profiles if item.get('flameGraph')), profiles[0] if profiles else {})
        flame_graph = selected_profile.get('flameGraph')
        record.update({"lifecycle": lifecycle or result.get("lifecycle"), "flameGraph": flame_graph,
                       "sqlProfile": {k: v for k, v in selected_profile.items() if k != 'flameGraph'} or None,
                       "plan": result.get("plan_json"), "diagnosis_results": diagnosis,
                       'diagnosisTrace': result.get('diagnosis_trace') or None,
                       'actionTrace': result.get('action_trace') or evaluation.get('action_trace') or None,
                       'evaluation': evaluation,
                       'activeApplication': {'id': active.get('improvement_id'), 'status': active.get('status'),
                           'applyKind': active.get('apply_kind'), 'applyScope': active.get('apply_scope'),
                           'hitCount': active.get('hit_count'), 'validation': decode(active.get('validation_json'), {})} if active else None,
                       'executionHistory': lifecycle,
                       'jobHistory': [{k: item.get(k) for k in ('job_id', 'status', 'requested_at', 'finished_at', 'error')}
                                      for item in self._rows('SELECT * FROM dream_jobs WHERE sql_key=? ORDER BY requested_at DESC LIMIT 10', (key,))] if key else [],
                       "tuning_results": {
                           "query_id": key, "fix_action": improvement.get("fix_action") or result.get("fix_action"),
                           "rewrite_sql": improvement.get("rewrite_sql") or result.get("rewrite_sql"),
                           "action_explanation": evaluation.get("explanation"),
                           "root_causes": diagnosis.get("root_causes") or [],
                           "old_time": old_time, "new_time": new_time,
                           "improvement_ratio": improvement.get("improvement_ratio"),
                           "validation": decode(improvement.get("validation_json"), {}),
                           "status": improvement.get("status"), "applyKind": improvement.get("apply_kind"),
                           "applyScope": improvement.get("apply_scope"), "hitCount": improvement.get("hit_count"),
                           "sessionSettings": decode(improvement.get("session_settings_json"), {}),
                           'message': evaluation.get('msg') or (result.get('bridge_publication') or {}).get('reason') or result.get('explanation') or job.get('error'),
                           'evaluationStatus': result.get('evaluation_status'),
                           'baselineProvenance': 'Recorded original execution; see timestamps in executionHistory. A reused baseline is not a new paired benchmark.',
                       }, "job": {"id": job.get("job_id"), "status": job.get("status"),
                                   "startedAt": job.get("started_at"), "finishedAt": job.get("finished_at"),
                                   "error": job.get("error"), "model": result.get("api_model"),
                                   'historyExpired': bool(job.get('history_expired')),
                                   'historyNote': job.get('history_note'),
                                   'evidenceSource': 'keeninsight_evidence (active action)' if job.get('history_expired') else 'dream_jobs',
                                   'triggerSource': job.get('trigger_source'), 'sourceCompletionMs': job.get('source_completion_ms')},
                       "execution": execution, "statistics": observation,
                       "availability": {name: "recorded" if record_value is not None else reason for name, record_value, reason in [
                           ("plan", result.get("plan_json"), "not present in this historical worker result"),
                           ("lifecycle", lifecycle or result.get("lifecycle"), "no retained execution or job timestamps; SQL-internal CPU/memory stages are not instrumented"),
                           ("flameGraph", flame_graph, selected_profile.get('reason') or "SQL-scoped stack profile was not collected"),
                           ("confidence", diagnosis.get("confidence") or None, "planner did not return confidence"),
                       ]}})
        return clean(record)

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
            elif category == "diagnosisRules":
                path = REPO / "dream/dream/agent/action/rewrite_rules.jsonl"
                if path.exists():
                    for line in path.read_text().splitlines():
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        values.append({"id": item["name"], "name": item["name"], "category": "SQL rewrite",
                                       "status": "available", "description": item.get("conditions"),
                                       "condition": item.get("conditions"), "action": item.get("transformations"),
                                       "source": "DREAM rewrite_rules.jsonl"})
            self._cache[category] = values
            return values

    def knowledge(self):
        rows = self._rows("SELECT * FROM improvements ORDER BY created_at DESC LIMIT 500")
        return [{"id": row["improvement_id"], "condition": row.get("root_causes"),
                 "content": row.get("rewrite_sql") or row.get("fix_action") or row.get("hints"),
                 "confidence": None, "measuredImprovement": row.get("improvement_ratio"),
                 "oldTimeSeconds": row.get("old_time"), "newTimeSeconds": row.get("new_time"),
                 "status": row["status"], "hitCount": row["hit_count"], "sqlKey": row["sql_key"],
                 "validation": decode(row.get("validation_json"), {}), "source": "measured DREAM improvement",
                 "updatedAt": row.get("last_action_at") or row["created_at"]} for row in rows]

    def experiment(self, row):
        result = decode(row.get('result_json'), {}) if 'result_json' in row else row.get('result') or {}
        config = decode(row.get('config_json'), {}) if 'config_json' in row else row.get('config') or {}
        tuning = result.get('tuning') or {}
        comparison = result.get('comparison') or {}
        ids = [item.get('incident_id') for item in (result.get('sysinsight_state') or {}).get('current_incidents', [])]
        sql_key = result.get('sql_key')
        return {'run_id': row['run_id'], 'kind': row['kind'], 'scenario_id': row['scenario_id'],
            'title': row.get('title'), 'status': row['status'], 'phase': row.get('phase'),
            'requested_at': row['requested_at'], 'started_at': row.get('started_at'), 'finished_at': row.get('finished_at'),
            'error': row.get('error'), 'config': config, 'comparison': comparison,
            'candidateSource': tuning.get('source'), 'candidateSourceType': tuning.get('source_type'),
            'tuningStatus': tuning.get('status'), 'application': tuning.get('application') or {},
            'incidentIds': ids, 'sqlKey': sql_key, 'jobId': result.get('job_id'),
            'baselineExecutionId': result.get('baseline_execution_id'), 'optimizedExecutionId': result.get('optimized_execution_id'),
            'phases': {name: {k: phase.get(k) for k in ['phase', 'duration_seconds', 'business_tps_summary', 'pressure_tps_summary']}
                       for name, phase in (result.get('phases') or {}).items()},
            'evidenceNote': ('This experiment replays a previously measured LLM candidate. The separately linked alert workflow makes fresh LLM calls; they are different measurements.'
                             if row['kind'] == 'sysinsight' else 'Original and optimized executions are separate measured records; analysis completion alone does not mean an action was applied.')}

    def overview(self):
        counts = {table: self._rows('SELECT count(*) n FROM ' + table)[0]['n']
                  for table in ('incidents', 'sql_observations', 'dream_jobs', 'improvements', 'lab_runs')}
        statuses = {table: {row['status']: row['n'] for row in self._rows('SELECT status,count(*) n FROM '+table+' GROUP BY status')}
                    for table in ('incidents', 'dream_jobs', 'improvements')}
        latest_alerts = [self._alert(row, False) for row in self._rows('SELECT * FROM incidents ORDER BY started_at DESC LIMIT 8')]
        jobs = self._rows('SELECT j.*,o.queryid FROM dream_jobs j LEFT JOIN sql_observations o ON j.sql_key=o.sql_key ORDER BY j.requested_at DESC LIMIT 12')
        latest_jobs = []
        for row in jobs:
            result = decode(row.get('result_json'), {})
            publication = result.get('bridge_publication') or {}
            diagnosis = result.get('diagnosis_results') or {}
            latest_jobs.append({'id': row['job_id'], 'sqlKey': row['sql_key'], 'queryId': row.get('queryid'),
                'time': row['requested_at'], 'status': row['status'], 'analysisOutcome': result.get('status'),
                'triggerSource': row.get('trigger_source'), 'sourceCompletionMs': row.get('source_completion_ms'),
                'applicationStatus': publication.get('status'), 'model': result.get('api_model'),
                'rootCauses': diagnosis.get('root_causes') or result.get('root_causes') or [],
                'oldTimeSeconds': result.get('old_time'), 'newTimeSeconds': result.get('new_time'),
                'message': row.get('error') or result.get('error') or result.get('explanation'),
                'validation': publication.get('validation') or {}})
        runs = [self.experiment(row) for row in self._rows('SELECT * FROM lab_runs ORDER BY requested_at DESC LIMIT 12')]
        gate = getattr(self.bridge, 'last_dream_gate', None) or self.store.get_meta('last_dream_gate', {})
        args = self.bridge.args
        return {'hosts': self.hosts(), 'counts': counts, 'workflowCounts': statuses,
            'recentRuns': runs, 'recentAlerts': latest_alerts, 'recentDreamJobs': latest_jobs,
            'automation': {'model': getattr(args, 'model', None), 'dreamTriggerMs': getattr(args, 'dream_trigger_ms', 10000),
                'dreamGate': gate, 'autoApplyEnabled': getattr(args, 'auto_apply', None),
                'sysinsightFullChain': getattr(args, 'sysinsight_full_chain', None),
                'currentAlertFiring': bool(getattr(self.bridge, 'last_alert_snapshot', None)),
                'collectionError': self.bridge.last_collection_error},
            'capabilities': [
                {'name': 'Alert → perf → source → LLM → apply/retest/restore', 'state': 'enabled' if getattr(args,'sysinsight_full_chain',False) else 'disabled', 'detail': 'Open an alert to inspect measured evidence and restoration, not just workflow status.'},
                {'name': 'Completed long AP SQL → asynchronous DREAM', 'state': 'enabled', 'detail': 'Automatic gate uses the configured completed-query threshold; manual lab diagnosis is explicitly operator-triggered.'},
                {'name': 'Validated DREAM automatic application', 'state': 'enabled' if getattr(args,'auto_apply',False) else 'disabled', 'detail': 'Rewrites/session actions apply to matching bridge-managed SQL; direct PostgreSQL clients only receive supported pg_hint_plan actions.'},
                {'name': 'SHAP attribution', 'state': 'experimental' if (ROOT/'models/sysinsight_pg12_tps/manifest.json').is_file() else 'unavailable', 'detail': 'Tree-path-dependent TreeSHAP of a local PG12 foreground-TPS-retention model, trained on measured cases. Prediction explanation, not causality or LLM reasoning. Per-alert version, validation and range warnings retained.'},
                {'name': 'SQL-specific CPU flame graph', 'state': 'enabled' if getattr(getattr(self.bridge,'sql_profiler',None),'enabled',False) else 'disabled', 'detail': 'Bounded on-CPU samples for the leader and identity-verified PG12 parallel workers, linked to the execution ID. Per-PID validated windows and coverage counts are retained; short-lived workers may be missed. Zero samples are not zero SQL cost.'},
                {'name': 'SQL-internal CPU/memory and wait phases', 'state': 'not_instrumented', 'detail': 'CPU flame graphs are not wall-clock time axes or off-CPU/memory breakdowns. Execution/job timestamps remain separate.'},
                {'name': 'Host / database / TPS monitoring', 'state': 'external', 'detail': 'Available in the existing Grafana dashboards; not duplicated here.'},
            ], 'schema': self.schema(), 'links': {'lab': '/lab', 'automation': '/ui'}}

    def alert_rules(self):
        try:
            payload = self.bridge.prometheus._get("/api/v1/rules", {"type": "alert"})
            values = []
            for group in payload.get("data", {}).get("groups", []):
                for rule in group.get("rules", []):
                    values.append({"id": group["name"] + ":" + rule["name"], "name": rule["name"],
                                   "desc": rule.get("annotations", {}).get("description"), "reason": rule.get("annotations", {}).get("summary"),
                                   "alertField": rule.get("query"), "threshold": None,
                                   "period": rule.get("duration"), "state": rule.get("state"),
                                   "health": rule.get("health"), "lastError": rule.get("lastError"),
                                   "severity": rule.get("labels", {}).get("severity"),
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
        parts = resource.split("/", 1)
        category = parts[0]
        identity = parts[1] if len(parts)>1 else ""
        allowed_filters = {"hosts": ("status", "dbType", "ip"), "alerts": ("status", "severity", "reason"),
                           "sql": ("type", "result", "duration"), "code": ("db", "category"),
                           "diagnosisRules": ("category", "status")}
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
                data = self._alert(rows[0])
            else:
                data = self.alerts(limit, offset, search, filters)
        elif category == "sql":
            data = self.sql_detail(identity, job_id=arg('job_id', None)) if identity else self.sql_logs(limit, offset, search, str(arg("kind", "all")), filters)
        elif category in {"manual", "code", "diagnosisRules", "knowledge", "alertRules"}:
            values = self.references(category) if category in {"manual", "code", "diagnosisRules"} else (
                self.knowledge() if category=="knowledge" else self.alert_rules())
            if search:
                values = [v for v in values if search.lower() in json.dumps(v,ensure_ascii=False).lower()]
            values = [v for v in values if self._matches(v, filters)]
            data = {"items": values[offset:offset+limit], "total": len(values)}
        elif category == "overview":
            data = self.overview()
        else:
            raise KeyError("unknown KeenInsight resource")
        return dict(clean(data), schemaVersion=VERSION, updatedAt=now(), limit=limit, offset=offset)
