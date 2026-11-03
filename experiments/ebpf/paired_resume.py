"""Finish a technical restoration-check failure, never rerun a low-gain result.

Reuse the SAME physical snapshot and the SAME fresh API-selected candidate.
No model call, no candidate replacement, no relaxed TPS/pressure/drift gate.
All previous partial trials remain in their original paths.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from .collector import atomic_json, inspect_server
from .equivalence import invariant_state, native_settings
from .overhead import LibpqSession
from .paired_tps import cloned_cluster, make_summary, one_trial, read, retest, sha
from .six_scenarios import original_cases


class ReloadAwareAdmin:
    """Bounded re-read of existing settings, not another ALTER/reload."""
    def __init__(self, admin, expected, timeout=5):
        self.admin, self.expected, self.timeout = admin, expected, timeout
        self.observations = []

    def __getattr__(self, name):
        return getattr(self.admin, name)

    def execute(self, sql, value=False):
        if not ("json_object_agg" in sql and "FROM pg_settings WHERE name IN" in sql):
            return self.admin.execute(sql, value=value)
        deadline = time.monotonic() + self.timeout
        while True:
            raw = self.admin.execute(sql, value=value)
            data = json.loads(raw)
            current = {name: int(row["value"]) if row["type"] == "integer" else
                       float(row["value"]) if row["type"] == "real" else row["value"]
                       for name, row in data.items()}
            if current == self.expected:
                return raw
            self.observations.append({"at_monotonic_ns": time.monotonic_ns(), "observed": current,
                                      "expected": self.expected})
            if time.monotonic() >= deadline:
                raise RuntimeError("settings restoration still mismatches after bounded SIGHUP wait")
            time.sleep(.05)


def snapshot_digest(template):
    digest = hashlib.sha256()
    files = sorted(path for path in template.rglob("*") if path.is_file() and
                   path.relative_to(template).parts[0] not in ("pg_wal", "pg_stat_tmp"))
    for path in files:
        digest.update(str(path.relative_to(template)).encode())
        digest.update(bytes.fromhex(sha(path)))
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--scenario", required=True, choices=[case["id"] for case in original_cases()])
    parser.add_argument("--arm", required=True, choices=("perf", "ebpf"))
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    options = parser.parse_args(argv)
    root = Path(options.input).resolve()
    for path in root.glob("*/*/result.json"):
        if read(path).get("status") == "running":
            parser.error("finish the serial main experiment before retrying; no concurrent TPS workloads")
    directory = root / options.scenario / options.arm
    result = read(directory / "result.json")
    if result["status"] != "failed" or result.get("error") != \
            "RuntimeError: settings not restored in private paired arm":
        parser.error("only a technical restoration-check failure may be retried, not completed low gains")
    if (directory / "retest-attempt-2").exists():
        parser.error("a retry exists already; do not select repeatedly for a better TPS outcome")
    experiment = read(root / "experiment.json")
    template = root / "snapshot-preserved"
    if snapshot_digest(template) != experiment["snapshot_sha256"]:
        raise RuntimeError("preserved snapshot does not match original experiment")
    log = (directory / "block-0-before/business.log").read_text()
    duration = re.search(r"duration:\s*(\d+)\s*s", log)
    if not duration or int(duration.group(1)) != 12:
        parser.error("this bounded retry supports the recorded 12s / three-block experiment only")
    case = next(case for case in original_cases() if case["id"] == options.scenario)
    declared = result["controlled_workload"]
    args = SimpleNamespace(arm=options.arm, db="postgres", db_user="postgres", run_as="postgres",
                           dbms="postgresql", pg_version="12", pg_cluster="", measure_seconds=12,
                           measure_repeats=3, business_rate=declared["business_offered_TPS"],
                           pressure_rate=declared["pressure_offered_TPS"])
    server = inspect_server(options.pg_config)
    server["initial_business_aggregate"] = experiment["initial_business_aggregate"]
    attempt = directory / "retest-attempt-2"
    attempt.mkdir(mode=0o700)
    atomic_json(directory / "result-attempt-1-technical-failure.json", result)
    with cloned_cluster(template, server) as connection:
        args.host, args.port = connection[connection.index("-h") + 1], 55432
        connection_admin = LibpqSession(connection)
        admin = ReloadAwareAdmin(connection_admin, result["initial_configuration"])
        try:
            if invariant_state(admin) != experiment["initial_business_aggregate"]:
                raise RuntimeError("retry clone data changed")
            native_settings(admin, result["allowed_knobs"])
            one_trial(args, case, attempt, server, connection, admin, "common-scoring-warmup")
            measured = retest(args, case, attempt, server, connection, admin,
                              result["configuration"], result["allowed_knobs"])
        finally:
            connection_admin.close()
            atomic_json(attempt / "reload-wait-observations.json",
                        {"observations": admin.observations, "timeout_seconds": admin.timeout,
                         "note": "waits for actual observed settings, not a forced successful restore"})
    result.update(status="completed", retest=measured, technical_retry={
        "original_error": result.pop("error"), "source": "result-attempt-1-technical-failure.json",
        "same_snapshot_sha256": experiment["snapshot_sha256"], "no_new_API_call_or_candidate": True,
        "attempt_directory": str(attempt), "controller_change": "bounded native-settings SIGHUP re-read",
        "controller_sha256": sha(Path(__file__)),
    })
    atomic_json(directory / "result.json", result)
    make_summary(root, original_cases())
    print("Completed technical retry:", options.scenario, options.arm)


if __name__ == "__main__":
    main()
