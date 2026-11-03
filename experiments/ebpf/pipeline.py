"""Resident eBPF -> bounded diagnosis -> original SysInsight -> apply/retest/restore.

One explicit backend and one explicit replay SQL per invocation. No Prometheus,
perf fallback, fixed tuning configuration, global GUC change or DDL execution.
"""
import argparse
import ctypes as ct
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time
from types import SimpleNamespace

from .collector import atomic_json, inspect_server, process_identity
from .evidence import APP, build_input, diagnosis
from .overhead import LibpqSession
from .runtime import CollectorProcess

sys.path.insert(0, str(APP))


class Journal:
    def __init__(self, directory):
        self.directory = directory
        self.entries = []

    def stage(self, name, status="completed", **detail):
        entry = {"stage": name, "status": status, "monotonic_ns": time.monotonic_ns(),
                 "at_unix_ns": time.time_ns(), **detail}
        self.entries.append(entry)
        atomic_json(self.directory / "workflow.json", {"stages": self.entries})


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def checked_sql(sql):
    from sysinsight_live_chain import _is_read_only_sql
    value = sql.strip().rstrip(";")
    if not _is_read_only_sql(value) or len(value.encode()) > 1024 * 1024:
        raise ValueError("replay requires one explicit bounded read-only SQL statement")
    return value


class Replay:
    def __init__(self, args):
        connection = ["psql", "-h", args.host, "-p", str(args.port),
                      "-U", args.db_user, "-d", args.db]
        # LibpqSession historically defaults to postgres; make database/user
        # explicit for this new integration path.
        self.session = LibpqSession(connection, database=args.db, user=args.db_user)
        self.args = args
        try:
            self.session.execute("SET statement_timeout={}".format(int(args.sql_timeout * 1000)))
            self.session.execute("SET search_path TO " + args.db_schema + ", public")
            for name, value in args.baseline_settings.items():
                if name not in args.allowed_knobs:
                    raise ValueError("baseline settings must be explicitly allowed session knobs")
                self.session.execute('SET "{}"={}'.format(name, sql_literal(value)))
        except BaseException:
            self.session.close()
            raise

    def settings(self):
        values = {}
        for name in self.args.allowed_knobs:
            raw = self.session.execute(
                "SELECT setting FROM pg_settings WHERE name=" + sql_literal(name), value=True)
            values[name] = int(raw) if re.fullmatch(r"[0-9]+", raw) else raw
        return values

    def _result_hash(self, sql):
        lib = self.session.lib
        lib.PQntuples.argtypes, lib.PQntuples.restype = [ct.c_void_p], ct.c_int
        lib.PQnfields.argtypes, lib.PQnfields.restype = [ct.c_void_p], ct.c_int
        lib.PQgetisnull.argtypes, lib.PQgetisnull.restype = [ct.c_void_p, ct.c_int, ct.c_int], ct.c_int
        result = lib.PQexec(self.session.handle, sql.encode())
        if not result:
            raise RuntimeError("SQL execution returned NULL")
        try:
            if lib.PQresultStatus(result) != 2:
                raise RuntimeError(lib.PQerrorMessage(self.session.handle).decode())
            rows, fields = lib.PQntuples(result), lib.PQnfields(result)
            if rows > 10000 or fields > 100:
                raise ValueError("result too large for bounded correctness comparison")
            encoded = []
            size = 0
            for i in range(rows):
                row = [None if lib.PQgetisnull(result, i, j)
                       else lib.PQgetvalue(result, i, j).decode("utf-8", errors="strict")
                       for j in range(fields)]
                text = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
                size += len(text)
                if size > 4 * 1024 * 1024:
                    raise ValueError("result comparison exceeded 4MiB")
                encoded.append(text)
            # Preserve multiplicities, compare result multisets, not row order.
            digest = hashlib.sha256(("\n".join(sorted(encoded))).encode()).hexdigest()
            return {"result_sha256": digest, "rows": rows, "fields": fields,
                    "semantics": "exact result multiset for this dataset, not formal equivalence"}
        finally:
            lib.PQclear(result)

    def measure(self, sql, configuration=None):
        attempts = []
        for _ in range(self.args.measure_repeats):
            self.session.execute("BEGIN READ ONLY")
            try:
                for name, value in (configuration or {}).items():
                    self.session.execute('SET LOCAL "{}"={}'.format(name, sql_literal(value)))
                began = time.perf_counter_ns()
                result = self._result_hash(sql)
                elapsed = (time.perf_counter_ns() - began) / 1e6
                attempts.append({"duration_ms": elapsed, **result})
            finally:
                self.session.execute("ROLLBACK")
        if len({r["result_sha256"] for r in attempts}) != 1:
            raise ValueError("replay result was unstable; do not tune against changing results")
        return {"status": "completed", "median_duration_ms": statistics.median(
                    r["duration_ms"] for r in attempts), "attempts": attempts,
                "result_sha256": attempts[0]["result_sha256"],
                "scope": "read-only SQL round-trip plus bounded result hashing on managed libpq session"}

    def close(self):
        self.session.close()


def capture_detail(args, root, target, journal, on_detached=None):
    child = None
    events = []
    reason = "window_expired"
    try:
        child = CollectorProcess(root / "detail", target["pid"], args.pg_config, "query",
                                 "diagnostic", args.detail_seconds, max_mb=args.detail_max_mb,
                                 capture_sql=True)
        journal.stage("detail_attached", pid=target["pid"], process_start_ticks=target["start_ticks"],
                      scope="one_backend", duration_budget_seconds=args.detail_seconds,
                      output_budget_mb=args.detail_max_mb, profiles="diagnostic")
        while time.monotonic() - child.started < args.detail_seconds:
            events.extend(child.events.poll())
            if len(events) >= args.detail_max_events:
                reason = "event_budget_exceeded"
                break
            path = child.directory / "events.jsonl"
            if path.exists() and path.stat().st_size >= args.detail_max_mb * 1024 * 1024 * .95:
                reason = "output_budget_exceeded"
                break
            health = [r for r in events[-100:] if r.get("type") == "collector_health"]
            if health and health[-1].get("collector_cpu_percent", 0) > args.detail_max_cpu:
                reason = "collector_cpu_budget_exceeded"
                break
            current = diagnosis(events, target, args.sql)
            if (current["status"] == "recognized"
                    and len(current["completed_execution_ids"]) >= args.min_completed):
                reason = "diagnosis_evidence_complete"
                break
            if child.process.poll() is not None:
                break
            time.sleep(.025)
    finally:
        try:
            if child:
                manifest = child.close(check=False)
                events.extend(child.events.poll())
                journal.stage("detail_detached", reason=reason, child_exited=child.process.poll() is not None,
                              status="completed" if manifest.get("status") == "completed" else "failed",
                              manifest_status=manifest.get("status"))
        finally:
            if on_detached:
                on_detached()
    if not child or manifest.get("status") != "completed":
        raise RuntimeError("bounded detail collector did not complete cleanly")
    if manifest.get("perf_lost_notifications") or any(manifest.get("bpf_health", {}).values()):
        raise RuntimeError("diagnostic capture has reported errors; refusing automatic tuning")
    observed = diagnosis(events, target, args.sql)
    observed["detach_reason"] = reason
    atomic_json(root / "diagnosis.json", observed)
    if (observed["status"] != "recognized"
            or len(observed["completed_execution_ids"]) < args.min_completed
            or reason.endswith("budget_exceeded")):
        raise RuntimeError("detail window/budget ended without sufficient diagnostic evidence")
    return observed


def validate_and_retest(args, root, api, replay, sql, baseline, journal):
    from sysinsight_live_chain import _candidate_and_validation
    from pg_temporary_config import TemporaryPostgresConfiguration
    validation = _candidate_and_validation(api, args, args)
    atomic_json(root / "candidate_validation.json", validation)
    before = replay.settings()
    records = []
    selected = validation.get("selected_candidate")
    ordered = list(validation["candidates"])
    if selected:
        ordered.sort(key=lambda candidate: candidate["candidate_id"] != selected["candidate_id"])
    for candidate in ordered:
        configuration = candidate["validation"].get("configuration", {})
        record = {"candidate_id": candidate["candidate_id"], "source": candidate["source"],
                  "configuration": configuration}
        if (not candidate["validation"].get("valid")
                or candidate["live_validation"].get("status") != "completed"):
            record["status"] = "invalid"
            records.append(record)
            continue
        if set(configuration) - set(args.allowed_knobs):
            record.update(status="rejected", reason="outside explicit session knob allowlist")
            records.append(record)
            continue
        if any(name in args.knob_limits and not (
                args.knob_limits[name][0] <= float(value) <= args.knob_limits[name][1])
               for name, value in configuration.items()):
            record.update(status="rejected", reason="outside experiment resource safety bounds")
            records.append(record)
            continue
        # No global/backend change may reach the old applier at all.
        from pg_temporary_config import settings_details
        live_details = settings_details(args, configuration, "ebpf-policy")
        if any(item.get("context") not in {"user", "superuser"} for item in live_details.values()):
            record.update(status="rejected", reason="global/restart settings forbidden")
            records.append(record)
            continue
        journal.stage("candidate_apply", status="running", candidate_id=candidate["candidate_id"])
        applier = TemporaryPostgresConfiguration(args, configuration, "ebpf-closed-loop",
                                                allow_restart=False)
        try:
            with applier as applied:
                if applied.get("global_configuration") or applied.get("connection_configuration"):
                    raise ValueError("only transaction-local session application is allowed")
                tuned = replay.measure(sql, applied["session_configuration"])
            restored_settings = replay.settings()
            if before != restored_settings:
                raise ValueError("session configuration was not restored")
            restored = replay.measure(sql)
            equivalent = baseline["result_sha256"] == tuned["result_sha256"] == restored["result_sha256"]
            gain = 1 - tuned["median_duration_ms"] / baseline["median_duration_ms"]
            record.update(status="accepted" if equivalent and gain >= args.min_gain else "candidate",
                          result_equivalent=equivalent, improvement_ratio=gain, tuned=tuned,
                          restored=restored, before_settings=before, restored_settings=restored_settings,
                          temporary_application=applier.state)
            journal.stage("candidate_apply_retest_restore", candidate_id=candidate["candidate_id"],
                          result=record["status"], result_equivalent=equivalent,
                          improvement_ratio=gain, restored=before == restored_settings)
        finally:
            # measure() rolls back even on exceptions; connection close is an
            # additional boundary. A mismatch must never become a successful result.
            if replay.settings() != before:
                raise RuntimeError("restore verification failed")
        records.append(record)
        if len(records) >= args.max_candidates:
            break
    accepted = [r for r in records if r["status"] == "accepted"]
    result = {"status": "tuned_and_restored" if accepted else "no_validated_improvement",
              "candidates": records, "best": min(accepted, key=lambda r: r["tuned"]["median_duration_ms"])
              if accepted else None, "scope": "managed SQL only; no persistent/global deployment"}
    atomic_json(root / "tuning_result.json", result)
    return result


def run_once(args, on_resident_ready=None, on_detail_detached=None):
    validate_arguments(args)
    root = Path(args.output).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    journal = Journal(root)
    resident = replay = None
    result = {"schema": "keeninsight.ebpf-closed-loop.v1", "status": "running",
              "observation_source": "ebpf_only", "uses_prometheus": False, "uses_perf_record": False,
              "proposal_source": "original_SysInsight_ParameterLibrary_LLM_ACQ_API",
              "objective": "read_only_sql_duration_ms_not_TPS"}
    try:
        server = inspect_server(args.pg_config)
        target = process_identity(args.pid, server["binary"])
        resident = CollectorProcess(root / "resident", args.pid, args.pg_config, "system", "resident",
                                    min(3600, args.trigger_timeout + args.api_timeout + 180),
                                    report_seconds=1)
        journal.stage("resident_monitoring", pid=args.pid, target=target, profile="resident")
        if on_resident_ready:
            on_resident_ready()
        deadline = time.monotonic() + args.trigger_timeout
        trigger = None
        while time.monotonic() < deadline:
            if args.select_target:
                trigger = {"reason": "operator_selected_target", "pid": args.pid}
                break
            for event in resident.events.poll():
                if (event.get("type") == "metric_alert" and event.get("pid") == args.pid
                        and event.get("process_start_ticks") == target["start_ticks"]
                        and event.get("probe") == "temp_file_create"):
                    trigger = {"reason": "resident_temp_file_creation", "event": event, "pid": args.pid}
                    break
            if trigger:
                break
            if resident.process.poll() is not None:
                raise RuntimeError("resident collector exited while waiting for anomaly")
            time.sleep(.025)
        if not trigger:
            raise RuntimeError("no resident anomaly before trigger deadline")
        journal.stage("trigger", **trigger)
        observed = capture_detail(args, root, target, journal, on_detail_detached)
        journal.stage("problem_recognized", anomaly=observed["anomaly_type"],
                      functions=observed["observed_functions"])
        replay = Replay(args)
        current = replay.settings()
        baseline = replay.measure(args.sql)
        atomic_json(root / "baseline.json", baseline)
        case_path, input_path, detection = build_input(
            root, observed, trigger, args.sql, args.db, args.db_schema, args.allowed_knobs,
            current, baseline["median_duration_ms"], args.knob_limits)
        journal.stage("original_source_matching", matched_knobs=[
            r["knob_name"] for r in detection["source_match"]["matched_knob"]])
        from sysinsight_live_chain import _api_result
        journal.stage("original_model_acquisition", status="running")
        api = _api_result(case_path, input_path, root / "sysinsight_api", args, os.environ.copy())
        journal.stage("original_model_acquisition", artifact=api["artifact"])
        tuning = validate_and_retest(args, root, api, replay, args.sql, baseline, journal)
        result.update(status=tuning["status"], trigger=trigger, diagnosis=observed, tuning=tuning)
        journal.stage("cooldown", seconds=args.cooldown_seconds)
        time.sleep(args.cooldown_seconds)
        # Consume self-generated/old alerts instead of recursively scheduling
        # an additional tuning incident while the current one is closing.
        resident.events.poll()
        if resident.process.poll() is not None:
            raise RuntimeError("resident collector stopped before monitoring could resume")
        journal.stage("resident_resumed", profile="resident", detail_process_stopped=True)
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status="failed", error="{}: {}".format(type(exc).__name__, str(exc)))
        journal.stage("failed", status="failed", error=result["error"])
    finally:
        if replay:
            try:
                replay.close()
            except Exception as exc:
                result.update(status="failed", error="replay shutdown: " + str(exc))
        if resident:
            try:
                result["resident_manifest"] = resident.close(check=False)
                if result["resident_manifest"].get("status") != "completed":
                    result.update(status="failed", error="resident collector did not complete cleanly")
                journal.stage("resident_stopped", child_exited=resident.process.poll() is not None)
            except Exception as exc:
                result.update(status="failed", error="resident shutdown: " + str(exc))
        result["stages"] = journal.entries
        atomic_json(root / "result.json", result)
    return result


def validate_arguments(args):
    args.sql = checked_sql(args.sql)
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", args.db_schema):
        raise ValueError("unsafe schema")
    if not args.allowed_knobs or any(not re.fullmatch(r"[a-z_][a-z0-9_]*", n) for n in args.allowed_knobs):
        raise ValueError("explicit safe knob names required")
    if len(set(args.allowed_knobs)) != len(args.allowed_knobs):
        raise ValueError("duplicate allowed knobs")
    if not isinstance(args.baseline_settings, dict) or not isinstance(args.knob_limits, dict):
        raise ValueError("settings and limits must be objects")
    if set(args.baseline_settings) - set(args.allowed_knobs) or set(args.knob_limits) - set(args.allowed_knobs):
        raise ValueError("baseline settings/limits must be within allowed knobs")
    for limit in args.knob_limits.values():
        if (not isinstance(limit, list) or len(limit) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for v in limit) or limit[0] > limit[1]):
            raise ValueError("invalid numeric knob limit")
    for name in ("trigger_timeout", "detail_seconds", "api_timeout", "sql_timeout", "detail_max_mb",
                 "detail_max_cpu"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("positive finite " + name + " required")
    if not .1 <= args.detail_seconds <= 60 or args.detail_max_events < 1:
        raise ValueError("diagnostic duration must be 0.1..60s, event budget positive")
    if not 1 <= args.measure_repeats <= 10 or not 1 <= args.min_completed <= 100:
        raise ValueError("invalid repeat/completion limit")
    if not 1 <= args.max_candidates <= 5 or not 0 <= args.min_gain < 1:
        raise ValueError("invalid candidate/gain limits")
    if not math.isfinite(args.cooldown_seconds) or not 0 <= args.cooldown_seconds <= 60:
        raise ValueError("invalid cooldown")
    if not any(os.environ.get(name) for name in
               ("SYSINSIGHT_GPT_API_KEY", "SYSINSIGHT_API_KEY", "OPENAI_API_KEY")):
        raise ValueError("existing SysInsight API credentials required; no preset fallback")


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--sql-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--db", required=True)
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--host", default="/var/run/postgresql")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--db-schema", default="public")
    parser.add_argument("--run-as", default="postgres")
    parser.add_argument("--select-target", action="store_true")
    parser.add_argument("--trigger-timeout", type=float, default=30)
    parser.add_argument("--detail-seconds", type=float, default=5)
    parser.add_argument("--detail-max-mb", type=float, default=8)
    parser.add_argument("--detail-max-events", type=int, default=10000)
    parser.add_argument("--detail-max-cpu", type=float, default=50)
    parser.add_argument("--min-completed", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=1)
    parser.add_argument("--api-base", default=os.environ.get("SYSINSIGHT_GPT_BASE_URL", "https://api.example.com/v1"))
    parser.add_argument("--model", default=os.environ.get("SYSINSIGHT_GPT_MODEL", "your-model-name"))
    parser.add_argument("--api-timeout", type=float, default=180)
    parser.add_argument("--sql-timeout", type=float, default=20)
    parser.add_argument("--measure-repeats", type=int, default=3)
    parser.add_argument("--max-candidates", type=int, default=2)
    parser.add_argument("--min-gain", type=float, default=.05)
    parser.add_argument("--allowed-knobs", default="work_mem")
    parser.add_argument("--baseline-settings", default="{}", help="explicit replay baseline JSON, not a tuning result")
    parser.add_argument("--knob-limits", default='{"work_mem":[64,65536]}')
    return parser


def prepare_args(args):
    args.sql = Path(args.sql_file).read_text()
    args.allowed_knobs = args.allowed_knobs.split(",")
    args.baseline_settings = json.loads(args.baseline_settings)
    args.knob_limits = json.loads(args.knob_limits)
    args.dbms, args.pg_version, args.pg_cluster, args.candidate_count = "postgresql", "12", "", 1
    return args


def main(argv=None):
    args = prepare_args(argument_parser().parse_args(argv))
    result = run_once(args)
    print(json.dumps({"status": result["status"], "output": args.output}, indent=2))
    raise SystemExit(0 if result["status"] == "tuned_and_restored" else 1)


if __name__ == "__main__":
    main()
