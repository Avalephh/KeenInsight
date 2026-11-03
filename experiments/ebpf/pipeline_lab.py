"""Real eBPF + original API closed loop in a disposable PostgreSQL cluster."""
import argparse
import json
from pathlib import Path
import threading
import time

from .collector import atomic_json
from .lab import cluster
from .overhead import LibpqSession
from .pipeline import argument_parser, prepare_args, run_once


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--rows", type=int, default=80000)
    parser.add_argument("--select-target", action="store_true")
    parser.add_argument("--detail-seconds", type=float, default=5)
    parser.add_argument("--detail-max-events", type=int, default=10000)
    parser.add_argument("--api-timeout", type=float, default=240)
    options = parser.parse_args(argv)
    if not 10000 <= options.rows <= 200000:
        parser.error("use 10000..200000 fixture rows")
    output = Path(options.output).resolve()
    if output.exists():
        parser.error("output directory must be new")
    stop = threading.Event()
    worker = None
    with cluster(options.pg_config) as (_, connection):
        session = LibpqSession(connection)
        errors = []
        counts = [0]
        try:
            session.execute("SET work_mem='64kB'")
            session.execute("SET statement_timeout='10s'")
            session.execute("CREATE TABLE ebpf_pipeline_fixture AS SELECT g, "
                            "repeat(md5(g::text),8) AS payload FROM generate_series(1,{}) AS g".format(options.rows))
            session.execute("ANALYZE ebpf_pipeline_fixture")
            sql = ("SELECT count(*) FROM (SELECT g,payload FROM public.ebpf_pipeline_fixture "
                   "ORDER BY payload) AS sorted_rows")
            temporary = Path(connection[connection.index("-h") + 1]).parent
            sql_file = temporary / "replay.sql"
            sql_file.write_text(sql)
            args = argument_parser().parse_args([
                "--pid", str(session.pid), "--sql-file", str(sql_file), "--output", str(output),
                "--pg-config", options.pg_config, "--db", "postgres",
                "--host", connection[connection.index("-h") + 1],
                "--port", connection[connection.index("-p") + 1],
                "--baseline-settings", '{"work_mem":64}',
                "--detail-seconds", str(options.detail_seconds),
                "--detail-max-events", str(options.detail_max_events),
                "--api-timeout", str(options.api_timeout),
            ] + (["--select-target"] if options.select_target else []))
            args = prepare_args(args)

            def workload():
                deadline = time.monotonic() + 90
                try:
                    while not stop.is_set() and time.monotonic() < deadline:
                        session.execute(sql)
                        counts[0] += 1
                except Exception as exc:
                    errors.append(str(exc))

            def start_workload():
                nonlocal worker
                worker = threading.Thread(target=workload, name="ebpf-lab-readonly-workload")
                worker.start()

            def stop_workload():
                stop.set()
                if worker:
                    worker.join(timeout=15)
                    if worker.is_alive():
                        raise RuntimeError("lab workload did not stop before tuning replay")
                if errors:
                    raise RuntimeError("lab workload failed: " + "; ".join(errors))

            result = run_once(args, start_workload, stop_workload)
            stages = [s["stage"] for s in result["stages"]]
            best = result.get("tuning", {}).get("best") or {}
            checks = {
                "resident_event_or_explicit_selection_triggered": "trigger" in stages,
                "real_bounded_ebpf_detail_captured": "detail_attached" in stages,
                "detail_unloaded_before_model": ("detail_detached" in stages and
                    ("original_model_acquisition" not in stages or
                     stages.index("detail_detached") < stages.index("original_model_acquisition"))),
                "problem_recognized_from_ebpf": "problem_recognized" in stages,
                "original_source_match_ran": "original_source_matching" in stages,
                "original_model_and_candidate_path_ran": "candidate_apply_retest_restore" in stages,
                "measured_improvement": result["status"] == "tuned_and_restored",
                "real_result_equivalent": best.get("result_equivalent") is True,
                "settings_restored": bool(best) and best.get("before_settings") == best.get("restored_settings"),
                "resident_resumed": "resident_resumed" in stages,
                "no_prometheus_or_perf": not result["uses_prometheus"] and not result["uses_perf_record"],
                "all_child_collectors_stopped": result.get("resident_manifest", {}).get("status") == "completed",
            }
            if output.exists():
                atomic_json(output / "lab_checks.json", {"checks": checks,
                            "status": "passed" if all(checks.values()) else "failed",
                            "workload_iterations": counts[0], "workload_errors": errors})
            print(json.dumps({"status": result["status"], "checks": checks,
                              "error": result.get("error"),
                              "improvement_ratio": best.get("improvement_ratio"),
                              "output": str(output)}, indent=2))
            if not all(checks.values()):
                raise SystemExit(1)
        finally:
            stop.set()
            if worker:
                worker.join(timeout=15)
            session.close()


if __name__ == "__main__":
    main()
