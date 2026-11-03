"""Investigate gain failures; candidate replay is NOT a new model evaluation.

Offline audit preserves the original artifacts.  Optional controls only use a
new disposable cluster and the original Order Status workload/candidate.  No
credentials, production endpoint, perf process or fine-grained profiler.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
from types import SimpleNamespace

from .collector import atomic_json
from .equivalence import Workers, collect_phase, invariant_state, startup_options
from .equivalent_profiles import compare, normal_profile
from .equivalent_resident import Resident
from .lab import cluster
from .overhead import LibpqSession
from .pipeline import Journal, sql_literal
from .six_scenarios import ROOT, original_cases


def read(path):
    return json.loads(Path(path).read_text())


def profile_quality(directory):
    manifest = read(directory / "manifest.json")
    counts = Counter()
    rows = read(directory / "samples.json")["rows"]
    for row in rows:
        weight = row["sample_count"]
        counts["samples"] += weight
        if not row["user_frames"]:
            counts["no_user_frames"] += weight
        if any("uprobe" in name or name in ("handle_swbp", "handle_trampoline", "do_int3")
               for name in row["kernel_frames"]):
            counts["uprobe_path_samples"] += weight
    return {**counts, "uprobe_path_sample_percent":
            100 * counts["uprobe_path_samples"] / counts["samples"] if counts["samples"] else None,
            "health": manifest.get("health"), "sample_event": manifest["sample_event"]}


def audit_artifacts(source):
    cases = []
    for case in original_cases():
        directory = source / case["id"]
        before = read(directory / "pressure-before.json")
        candidate = read(directory / "tuning_result.json")["candidates"][0]
        detection = read(directory / "source_detection.json")
        normal = [profile_quality(directory / ("normal-" + str(index)) / "profile")
                  for index in range(2)]
        anomaly = profile_quality(directory / "anomaly/profile")
        baseline_tps = before["median_business_tps"]
        restored_tps = candidate["restored"]["median_business_tps"]
        cases.append({
            "case": case["id"], "configuration": candidate["configuration"],
            "session_settings_repeated_each_transaction":
                candidate["application"]["session_configuration"],
            "before_tps": baseline_tps, "tuned_tps": candidate["tuned"]["median_business_tps"],
            "restored_tps": restored_tps,
            "raw_gain_percent": 100 * candidate["gain_ratio"],
            "restored_drift_percent": 100 * (restored_tps / baseline_tps - 1),
            "original_pressure_comparison": candidate["original_TPS_comparison"],
            "trials": {name: [
                {"business_tps": row["business"]["steady_tps"],
                 "business_cv_percent": row["business"]["steady_summary"]["cv_percent"],
                 "pressure_tps": row["pressure"]["steady_tps"]} for row in value["trials"]]
                for name, value in (("before", before), ("tuned", candidate["tuned"]),
                                    ("restored", candidate["restored"]))},
            "profiles": {"normal": normal, "anomaly": anomaly},
            "top_differential": detection["source_compare"]["key_functions"][:20],
            "normal_has_resource_detail": (directory / "normal-0/detail/manifest.json").exists(),
            "anomaly_resource_probes": read(directory / "anomaly/detail/manifest.json")["attached_probes"],
        })
    return {"source": str(source), "cases": cases,
            "meaning": "audit, not retrospective proof of candidate benefit or collection overhead"}


def control_order(round_index):
    # Reverse each pair of rounds; rotate pairs.  Every cell is run once per
    # round. This reduces order bias but does NOT eliminate shared-host noise.
    cells = [(mode, resident) for mode in ("baseline", "startup", "legacy_SET")
             for resident in (False, True)]
    shift = (round_index // 2 * 2) % len(cells)
    cells = cells[shift:] + cells[:shift]
    return list(reversed(cells)) if round_index % 2 else cells


def control_summary(trials):
    cells = {}
    for mode in ("baseline", "startup", "legacy_SET"):
        for resident in (False, True):
            selected = [row for row in trials if row["mode"] == mode and row["resident"] == resident]
            cells[mode + ("_resident" if resident else "_no_BPF")] = {
                "business_tps": [row["business"]["steady_tps"] for row in selected],
                "median_business_tps": statistics.median(row["business"]["steady_tps"] for row in selected),
                "pressure_tps": [row["pressure"]["steady_tps"] for row in selected],
            }
    comparisons = []
    for repeat in sorted({row["repeat"] for row in trials}):
        rows = {(row["mode"], row["resident"]): row for row in trials if row["repeat"] == repeat}
        tps = lambda mode, resident: rows[(mode, resident)]["business"]["steady_tps"]
        comparisons.append({
            "repeat": repeat,
            "resident_loss_percent_by_mode": {
                mode: 100 * (1 - tps(mode, True) / tps(mode, False))
                for mode in ("baseline", "startup", "legacy_SET")},
            "legacy_SET_vs_startup_loss_percent": {
                str(resident): 100 * (1 - tps("legacy_SET", resident) / tps("startup", resident))
                for resident in (False, True)},
            "candidate_gain_percent": {
                str(resident): 100 * (tps("startup", resident) / tps("baseline", resident) - 1)
                for resident in (False, True)},
        })
    return {"cells": cells, "within_round_comparisons": comparisons,
            "causal_limit": "short repeated ordered shared-host control, not a confidence interval"}


def prepare_fixture(root, connection):
    with (root / "fixture.log").open("w") as log:
        subprocess.run(connection + ["--single-transaction", "-v", "warehouses=5", "-v", "seed=1",
                       "-f", str(ROOT / "scripts/tpcc_schema.sql")], stdout=log,
                       stderr=subprocess.STDOUT, check=True, timeout=180)


def run_controls(args, source, root):
    case = next(item for item in original_cases() if item["id"] == "tp_order_status_hot")
    candidate = read(source / case["id"] / "tuning_result.json")["candidates"][0]
    application = candidate["application"]
    if application["global_configuration"] or application["connection_configuration"]:
        raise ValueError("this bounded control only supports the original session-only Order Status candidate")
    settings = application["session_configuration"]
    options = startup_options(settings)  # Validate before starting any database.
    legacy = dict(case)
    prefix = "".join('SET "{}"={};\n'.format(name, sql_literal(value))
                     for name, value in settings.items())
    hashes = {}
    for key in ("sql", "normal_sql"):
        original = ROOT / "app/tpcc_cases" / case[key]
        path = root / ("legacy-" + key + ".sql")
        path.write_text(prefix + original.read_text())
        legacy[key] = str(path)
        hashes[key] = hashlib.sha256(original.read_bytes()).hexdigest()
    trials = []
    with cluster(args.pg_config, max_connections=20) as (server, connection):
        prepare_fixture(root, connection)
        # Validate actual server acceptance and native units of PGOPTIONS
        # through the same libpq startup mechanism used by pgbench.
        previous = os.environ.pop("PGOPTIONS", None)
        admin = None
        try:
            admin = LibpqSession(connection)
            os.environ["PGOPTIONS"] = options
            check = LibpqSession(connection)
            try:
                startup_seen = {name: check.execute("SHOW " + name, value=True) for name in settings}
            finally:
                check.close()
                os.environ.pop("PGOPTIONS", None)
            invariant = invariant_state(admin)
            for repeat in range(args.repeats):
                for position, (mode, enabled) in enumerate(control_order(repeat)):
                    label = "{}-{}-{}-{}".format(repeat, position, mode, "resident" if enabled else "no_BPF")
                    directory = root / label
                    directory.mkdir(mode=0o700)
                    resident = workers = None
                    started = time.monotonic()
                    try:
                        if enabled:
                            resident = Resident(server, directory / "resident")
                            checkpointer = int(admin.execute(
                                "SELECT pid FROM pg_stat_activity WHERE backend_type='checkpointer'", value=True))
                            resident.add([admin.pid, checkpointer])
                        workers = Workers(legacy if mode == "legacy_SET" else case,
                                          directory, server, connection, admin, args.seconds,
                                          1600, 2500, True,
                                          settings if mode == "startup" else None, resident=resident)
                        row = workers.finish(timeout=args.seconds + 15)
                    finally:
                        try:
                            if workers:
                                workers.close()
                        finally:
                            manifest = resident.close() if resident else None
                    row.update(repeat=repeat, position=position, mode=mode, resident=enabled,
                               wall_seconds=time.monotonic() - started,
                               detailed_profiling_enabled=False, resident_manifest=manifest)
                    trials.append(row)
                    atomic_json(directory / "result.json", row)
                    atomic_json(root / "trials.json", {"trials": trials})
                    print(label, "business_TPS", row["business"]["steady_tps"],
                          "pressure_TPS", row["pressure"]["steady_tps"], flush=True)
            unchanged = invariant_state(admin) == invariant
            if not unchanged:
                raise RuntimeError("business aggregate invariant changed in disposable control")
        finally:
            if admin:
                admin.close()
            os.environ.pop("PGOPTIONS", None)
            if previous is not None:
                os.environ["PGOPTIONS"] = previous
    result = {"status": "completed", "candidate_source": str(source / case["id"] / "tuning_result.json"),
              "candidate_replay_not_new_model_call": True, "configuration": settings,
              "PGOPTIONS": options, "startup_settings_seen_by_server": startup_seen,
              "original_SQL_sha256": hashes, "seconds": args.seconds, "repeats": args.repeats,
              "fixture": "new disposable five-warehouse cluster, same SQL/clients/offered rates as source",
              "business_aggregate_unchanged": unchanged, "trials": trials,
              "summary": control_summary(trials),
              "not_claimed": "six-case gain equivalence, profiler overhead, or stable statistical significance"}
    atomic_json(root / "control.json", result)
    return result


def run_capture_check(args, root):
    """Real collection/differential only; no model call or gain claim."""
    from db_profile import resolve_profile
    case = next(item for item in original_cases() if item["id"] == "tp_order_status_hot")
    options = SimpleNamespace(pg_config=args.pg_config, profile_seconds=15, frequency=300,
                              trigger_timeout=10, trigger_mode="activity", min_samples=100,
                              business_rate=1600, pressure_rate=2500, resident=None)
    journal = Journal(root)
    with cluster(args.pg_config, max_connections=20) as (server, connection):
        prepare_fixture(root, connection)
        admin = LibpqSession(connection)
        try:
            options.resident = Resident(server, root / "resident")
            checkpointer = int(admin.execute(
                "SELECT pid FROM pg_stat_activity WHERE backend_type='checkpointer'", value=True))
            options.resident.add([admin.pid, checkpointer])
            normal = [collect_phase(options, case, root, server, connection, admin,
                                    "normal-" + str(index), False, journal) for index in range(2)]
            normal_path = root / "normal_profile.csv"
            normal_profile([row["counts"] for row in normal], normal_path)
            anomaly = collect_phase(options, case, root, server, connection, admin,
                                    "anomaly", True, journal)
            detection = compare(anomaly["counts"], normal_path, root,
                                resolve_profile("postgresql", "12"), len(normal))
        finally:
            try:
                if options.resident:
                    manifest = options.resident.close()
            finally:
                admin.close()
    profiles = [row["profile"] for row in normal] + [anomaly["profile"]]
    configurations = {json.dumps(row["comparison_instrumentation"], sort_keys=True) for row in profiles}
    stages = [row["stage"] for row in journal.entries]
    ordering = stages.index("anomaly_detail_detached") < stages.index("targeted_resource_probes_attached")
    result = {
        "status": "completed", "case": case["id"],
        "same_CPU_profile_instrumentation": len(configurations) == 1,
        "resource_probes_attached_after_CPU_sampler_unloaded": ordering,
        "normal_profiles": [profile_quality(root / ("normal-" + str(index)) / "profile")
                            for index in range(2)],
        "anomaly_profile": profile_quality(root / "anomaly/profile"),
        "original_differential_key_function_count": detection["source_compare"]["key_function_count"],
        "top_differential": detection["source_compare"]["key_functions"][:20],
        "resident_manifest": manifest,
        "not_claimed": "new model selection, tuning success, or six-case acceptance",
    }
    if len(configurations) != 1 or not ordering or not all(row["all_probe_FDs_closed"] for row in profiles):
        raise RuntimeError("post-fix capture configuration/order/cleanup check failed")
    atomic_json(root / "capture-check.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--controls", action="store_true")
    mode.add_argument("--capture-check", action="store_true")
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=4)
    args = parser.parse_args(argv)
    if not 8 <= args.seconds <= 30 or not 2 <= args.repeats <= 6:
        parser.error("bounded control requires seconds 8..30 and repeats 2..6")
    source = Path(args.source).resolve()
    root = Path(args.output).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_json(root / "audit.json", audit_artifacts(source))
    if args.controls:
        run_controls(args, source, root)
    if args.capture_check:
        run_capture_check(args, root)
    print("Audit written:", root)


if __name__ == "__main__":
    main()
