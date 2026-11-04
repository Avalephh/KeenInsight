"""Opt-in REAL eBPF->perf file-budget failure, with no model call.

Reuses a stopped private snapshot, not a running PGDATA. Unlike a mocked
supervisor test this induces an actual original-perf budget failure after a
real sustained eBPF alarm, checks the same sentinel still receives reports,
and checks no living member remains in the owned perf process group.
Run serially AFTER TPS trials, never alongside benchmark measurements.
"""
import argparse
import json
from pathlib import Path
import time
from types import SimpleNamespace

from .collector import atomic_json, inspect_server
from .hybrid_lab import detect_and_capture
from .hybrid_perf import living_group_members
from .hybrid_sentinel import StatsSentinel, receiver_target
from .overhead import LibpqSession
from .paired_tps import cloned_cluster
from .pipeline import Journal
from .six_scenarios import original_cases


def run(args):
    directory = Path(args.output).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    template = Path(args.template).resolve(strict=True)
    if ((template / "postmaster.pid").exists() or not (template / "PG_VERSION").is_file()
            or (template / "PG_VERSION").read_text().strip() != "12"):
        raise ValueError("a stopped private PG12 snapshot is required")
    server = inspect_server(args.pg_config)
    case = next(case for case in original_cases() if case["id"] == "tp_order_status_hot")
    options = SimpleNamespace(
        pg_config=args.pg_config, stackcollapse=args.stackcollapse, profile_seconds=15,
        frequency=300, window_seconds=2, calibration_windows=6, guard_windows=3,
        consecutive_windows=2, surge_ratio=1.6, drop_ratio=.6, drop_sustain_seconds=10,
        trigger_timeout=30, warmup_seconds=4, cooldown_seconds=30, max_incidents=1,
        max_output_mb=1, max_pids=16, business_rate=1600, pressure_rate=2500)
    journal = Journal(directory)
    result = {"status": "running", "expected_failure": "real original perf output budget",
              "max_output_bytes": 1024 ** 2, "model_calls": 0}
    calls = []

    def forbidden_model(bundle, alert, stop_workloads):
        calls.append(True)
        raise AssertionError("budget failure must never reach diagnosis/model")

    sentinel = None
    try:
        with cloned_cluster(template, server) as connection:
            admin = LibpqSession(connection)
            try:
                sentinel = StatsSentinel(receiver_target(admin, server), server,
                                         directory / "sentinel", args.pg_config)
                error = None
                try:
                    detect_and_capture(options, case, directory, server, connection, admin,
                                       sentinel, journal, forbidden_model)
                except RuntimeError as exc:
                    error = type(exc).__name__ + ": " + str(exc)
                supervisors = list(directory.glob("anomaly/profile/supervisor.json"))
                if len(supervisors) != 1:
                    raise RuntimeError("no actual triggered perf supervisor; failure was not the intended budget")
                supervisor = json.loads(supervisors[0].read_text())
                if (not error or supervisor["status"] != "failed"
                        or supervisor["reason"] not in ("child_failed_or_file_limit", "aggregate_output_budget")
                        or not supervisor["all_owned_processes_stopped"] or calls):
                    raise RuntimeError("did not observe the expected bounded capture failure")
                limited_files = [str(path) for path in supervisors[0].parent.iterdir()
                                 if path.is_file() and path.stat().st_size == result["max_output_bytes"]]
                if supervisor["reason"] == "child_failed_or_file_limit" and not limited_files:
                    raise RuntimeError("child failed, but no file reached the injected byte limit")
                if living_group_members(supervisor["owned_process_group"]):
                    raise RuntimeError("owned perf/sleep process still alive after abort")
                stages = [row["stage"] for row in journal.entries]
                if not ("ebpf_alert" in stages and "incident_aborted" in stages
                        and stages[-1] == "resident_resumed"):
                    raise RuntimeError("real BPF trigger or return-to-resident stage missing")
                before = sentinel.snapshot()
                # Generate native reports AFTER abort, not a synthetic snapshot.
                for _ in range(3):
                    admin.execute("SELECT count(*) FROM pg_class")
                    time.sleep(.6)
                after = sentinel.snapshot()
                if after["recv_calls"] <= before["recv_calls"]:
                    raise RuntimeError("same sentinel stopped receiving after perf budget abort")
                result.update(status="completed", error_observed=error,
                              automatic_ebpf_alarm=True, diagnosis_calls=len(calls),
                              no_living_perf_processes=True, same_sentinel_still_receiving=True,
                              files_at_injected_limit=limited_files,
                              supervisor=supervisor)
            finally:
                try:
                    if sentinel:
                        result["sentinel_cleanup"] = sentinel.close()
                        sentinel = None
                finally:
                    admin.close()
    except BaseException as exc:
        result.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        atomic_json(directory / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--stackcollapse", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args), indent=2))


if __name__ == "__main__":
    main()
