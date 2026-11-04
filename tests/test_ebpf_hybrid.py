"""Hermetic hybrid alarms/lifecycle tests; no BPF, perf, PG or model service."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from experiments.ebpf.equivalence import Workers
from experiments.ebpf.hybrid_controller import HybridController
from experiments.ebpf.hybrid_perf import CaptureBudgetExceeded, capture, living_group_members, supervised, worker
from experiments.ebpf.hybrid_policy import IncidentGate, RATE_FIELDS, RateDetector, rates
from experiments.ebpf.hybrid_sentinel import PROGRAM, program, stats_layout
from experiments.ebpf.hybrid_report import report
from experiments.ebpf.hybrid_lab import attach_trigger_metadata


def counters(at, scale=1):
    values = {name: 0 for name in RATE_FIELDS}
    values.update(commits=1000 * at * scale, returned=10000 * at * scale,
                  fetched=10000 * at * scale, blocks=10000 * at * scale)
    return {"mono": at, "errors": 0, **values}


def detector():
    result = RateDetector()
    result.fit([rates(counters(1), counters(3)) for _ in range(6)])
    return result


class SentinelRates(unittest.TestCase):
    def test_rates_are_counter_deltas_not_global_totals(self):
        result = rates(counters(5), counters(7))
        self.assertEqual(result["transactions"], 1000)
        self.assertEqual(result["blocks"], 10000)

    def test_clock_error_and_reset_do_not_turn_into_zero_or_synthetic_alert(self):
        for before, after in ((counters(3), counters(1)),
                              (counters(1), dict(counters(3), commits=0)),
                              (counters(1), dict(counters(3), errors=1))):
            with self.assertRaises(ValueError):
                rates(before, after)

    def test_healthy_baseline_requires_actual_activity_and_enough_windows(self):
        with self.assertRaises(ValueError):
            RateDetector().fit([rates(counters(1), counters(3))])
        idle = rates(counters(1, 0), counters(3, 0))
        with self.assertRaisesRegex(ValueError, "no healthy"):
            RateDetector().fit([idle] * 6)

    def test_normal_windows_never_trigger_heavy_capture(self):
        policy = detector()
        for _ in range(10):
            self.assertIsNone(policy.observe(rates(counters(1), counters(3))))

    def test_one_spike_is_not_an_alarm(self):
        policy = detector()
        self.assertIsNone(policy.observe(rates(counters(1, 2), counters(3, 2))))
        self.assertIsNone(policy.observe(rates(counters(1), counters(3))))
        self.assertIsNone(policy.observe(rates(counters(1, 2), counters(3, 2))))

    def test_sustained_cpu_read_case_can_trigger_without_spill_or_lock_probe(self):
        policy = detector()
        window = rates(counters(1, 2), counters(3, 2))
        self.assertIsNone(policy.observe(window))
        alert = policy.observe(window)
        self.assertTrue(alert["signals"])
        self.assertEqual(alert["source"], "ebpf_native_PG12_received_statistics")
        self.assertEqual(window["temp_files"], 0)

    def test_sustained_drop_detects_database_transaction_drop_not_business_TPS(self):
        policy = detector()
        window = rates(counters(1, .4), counters(3, .4))
        for _ in range(4):
            self.assertIsNone(policy.observe(window))
        alert = policy.observe(window)
        self.assertEqual(alert["signals"][0]["rule"], "transaction_rate_drop")
        self.assertIn("business TPS", alert["not_claimed"])

    def test_short_completion_dip_does_not_pretend_to_be_sustained_stall(self):
        policy = detector()
        window = rates(counters(1, .4), counters(3, .4))
        for _ in range(2):
            self.assertIsNone(policy.observe(window))
        self.assertIsNone(policy.observe(rates(counters(1), counters(3))))

    def test_different_isolated_signals_do_not_make_one_sustained_alarm(self):
        policy = detector()
        base = rates(counters(1), counters(3))
        self.assertIsNone(policy.observe(dict(base, commits=3000)))
        self.assertIsNone(policy.observe(dict(base, blocks=30000)))

    def test_thresholds_and_baseline_values_must_be_valid(self):
        for args in ((1, .6, 2), (float("nan"), .6, 2), (1.6, 1, 2), (1.6, .6, 1)):
            with self.assertRaises(ValueError):
                RateDetector(*args)
        bad = dict(rates(counters(1), counters(3)), blocks=float("nan"))
        with self.assertRaises(ValueError):
            RateDetector().fit([bad] * 4)


class GateAndController(unittest.TestCase):
    def make_controller(self):
        journal = Mock()
        value = HybridController(detector(), journal, clock=lambda: 100)
        capture_rich = Mock(return_value={"counts": "real.csv",
                                          "profile": {"all_perf_processes_stopped": True}})
        resolve, diagnose = Mock(return_value=[101, 102]), Mock(return_value={"original": True})
        return value, journal, resolve, capture_rich, diagnose

    def fire(self, value, resolve, collect, diagnose):
        # Two windows with exactly twice the healthy rate; no scenario labels.
        args = (counters(1, 2), counters(3, 2), resolve, collect, diagnose)
        value.observe(*args)
        return value.observe(*args)

    def test_real_automatic_sustained_alarm_is_required_before_any_rich_callback(self):
        value, journal, resolve, collect, diagnose = self.make_controller()
        value.observe(counters(1, 2), counters(3, 2), resolve, collect, diagnose)
        resolve.assert_not_called()
        collect.assert_not_called()
        diagnose.assert_not_called()
        incident = value.observe(counters(3, 2), counters(5, 2), resolve, collect, diagnose)
        self.assertEqual(incident["diagnosis"], {"original": True})
        self.assertEqual([call.args[0] for call in journal.stage.call_args_list],
                         ["ebpf_alert", "perf_burst_started", "perf_burst_stopped",
                          "diagnosis_completed", "resident_resumed"])
        self.assertFalse(value.gate.active)
        self.assertEqual(value.gate.state, "resident")

    def test_per_incident_budget_prevents_repeated_perf_under_sustained_load(self):
        value, _, resolve, collect, diagnose = self.make_controller()
        self.fire(value, resolve, collect, diagnose)
        for _ in range(10):
            value.observe(counters(1, 2), counters(3, 2), resolve, collect, diagnose)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(diagnose.call_count, 1)

    def test_capture_failure_does_not_call_model_and_always_returns_to_resident(self):
        value, journal, resolve, collect, diagnose = self.make_controller()
        collect.side_effect = CaptureBudgetExceeded("disk quota")
        with self.assertRaises(CaptureBudgetExceeded):
            self.fire(value, resolve, collect, diagnose)
        diagnose.assert_not_called()
        self.assertEqual(value.gate.state, "resident")
        self.assertEqual(journal.stage.call_args_list[-1].args[0], "resident_resumed")

    def test_uncertain_perf_cleanup_is_not_accepted_as_valid_evidence(self):
        value, _, resolve, collect, diagnose = self.make_controller()
        collect.return_value = {"counts": "untrusted.csv"}
        with self.assertRaisesRegex(RuntimeError, "cleanup"):
            self.fire(value, resolve, collect, diagnose)
        diagnose.assert_not_called()
        self.assertFalse(value.gate.active)

    def test_model_failure_releases_gate_but_is_not_claimed_as_success(self):
        value, _, resolve, collect, diagnose = self.make_controller()
        diagnose.side_effect = RuntimeError("real model unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.fire(value, resolve, collect, diagnose)
        self.assertFalse(value.gate.active)

    def test_cancellation_also_releases_incident_without_faking_diagnosis(self):
        value, journal, resolve, collect, diagnose = self.make_controller()
        diagnose.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.fire(value, resolve, collect, diagnose)
        self.assertFalse(value.gate.active)
        names = [call.args[0] for call in journal.stage.call_args_list]
        self.assertNotIn("diagnosis_completed", names)
        self.assertEqual(names[-1], "resident_resumed")

    def test_cooldown_and_concurrency_are_independent_of_count_budget(self):
        gate = IncidentGate(max_incidents=2, cooldown_seconds=30)
        self.assertTrue(gate.start(10))
        self.assertFalse(gate.start(100))
        gate.diagnosis()
        gate.finish(20)
        self.assertFalse(gate.start(49))
        self.assertTrue(gate.start(50))
        gate.finish(60)
        self.assertFalse(gate.start(100))


class PerfSupervisor(unittest.TestCase):
    @staticmethod
    def command(code):
        return [sys.executable, "-c", code]

    def run_child(self, code, directory, seconds=2, size=1024 * 1024):
        manifest = {}
        supervised(self.command(code), directory, os.environ.copy(), seconds, size, manifest)
        return manifest

    def test_successful_child_is_owned_and_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_child("pass", Path(tmp))
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["all_owned_processes_stopped"])

    def test_wallclock_budget_actually_stops_child_not_only_logs_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            with self.assertRaises(CaptureBudgetExceeded):
                self.run_child("import time; time.sleep(60)", Path(tmp), seconds=.1)
            result = json.loads((Path(tmp) / "supervisor.json").read_text())
            self.assertEqual(result["reason"], "wall_clock_budget")
            self.assertLess(time.monotonic() - start, 6)

    def test_aggregate_disk_budget_catches_multiple_individually_valid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ("from pathlib import Path; "
                    "Path({!r}).write_bytes(b'x'*8000); "
                    "Path({!r}).write_bytes(b'x'*8000)").format(
                        str(Path(tmp) / "one"), str(Path(tmp) / "two"))
            with self.assertRaises(CaptureBudgetExceeded):
                self.run_child(code, Path(tmp), size=14000)
            result = json.loads((Path(tmp) / "supervisor.json").read_text())
            self.assertEqual(result["reason"], "aggregate_output_budget")

    def test_per_file_limit_stops_unbounded_file_even_between_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "unbounded"
            with self.assertRaises(RuntimeError):
                self.run_child("open({!r},'wb').write(b'x'*1000000)".format(str(file)),
                               Path(tmp), size=16000)
            self.assertLessEqual(file.stat().st_size, 16000)

    def test_timeout_stops_sleep_descendant_not_only_leader(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ("import subprocess,time,signal; "
                    "p=subprocess.Popen(['sleep','60']); "
                    "signal.signal(signal.SIGINT,lambda s,f:(p.wait(),exit(0))); "
                    "time.sleep(60)")
            with self.assertRaises(CaptureBudgetExceeded):
                self.run_child(code, Path(tmp), seconds=.2)
            result = json.loads((Path(tmp) / "supervisor.json").read_text())
            self.assertTrue(result["all_owned_processes_stopped"])
            self.assertEqual(living_group_members(result["owned_process_group"]), [])

    def test_uncertain_cleanup_is_recorded_as_failure_not_success(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("experiments.ebpf.hybrid_perf.stop_group", side_effect=RuntimeError("not confirmed")):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                self.run_child("pass", Path(tmp))
            result = json.loads((Path(tmp) / "supervisor.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["all_owned_processes_stopped"])

    def test_invalid_PID_time_or_scope_is_rejected_before_spawning(self):
        for pids, seconds in (([], 15), ([10, 10], 15), ([10], 0), ([10], 61)):
            with self.assertRaises(ValueError):
                capture("not-created", pids, {}, seconds, 300, "missing")
        self.assertFalse(Path("not-created").exists())

    def test_PID_reuse_during_completed_perf_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = {"pid": 123, "start_ticks": 10}
            (directory / "request.json").write_text(json.dumps({
                "pids": [123], "targets": [target], "seconds": 15, "frequency": 300,
                "stackcollapse": "test-only", "binary": "postgres"}))
            measured = {"perf": {"returncode": 0},
                        "postprocess": {"status": "sysinsight_counts_generated"}}
            with patch("sysinsight_live_chain._capture_perf", return_value=measured), \
                    patch("experiments.ebpf.hybrid_perf.process_identity",
                          side_effect=[target, {"pid": 123, "start_ticks": 11}]):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    worker(directory)


class PG12ABIAndScope(unittest.TestCase):
    LAYOUT = {"pg_version_num": 120022, "pointer_bytes": 8, "header_bytes": 8,
              "tab_header_bytes": 40, "entry_bytes": 112, "entries": 8,
              "database_offset": 8, "entries_offset": 12, "commits_offset": 16,
              "rollbacks_offset": 20, "read_us_offset": 24, "write_us_offset": 32,
              "returned_offset": 16, "fetched_offset": 24, "blocks_offset": 96,
              "hit_offset": 104, "temp_bytes": 24, "temp_size_offset": 16,
              "tab_type": 2, "temp_type": 16, "deadlock_type": 17}

    def test_compiled_header_offsets_drive_bpf_not_unverified_assumptions(self):
        source = program(self.LAYOUT, 13461)
        self.assertIn("#define BLOCKS_OFFSET 96", source)
        self.assertIn("#define DATABASE_OID 13461", source)
        self.assertNotIn("PortalRun", source)
        self.assertNotIn("ExecutorRun", source)
        self.assertNotIn("get_stackid", source)

    def test_invalid_database_scope_is_rejected(self):
        for value in (0, -1, 2 ** 32, "1; dangerous"):
            with self.assertRaises(ValueError):
                program(self.LAYOUT, value)

    def test_non_pg12_header_abi_is_not_silently_supported(self):
        layout = dict(self.LAYOUT, pg_version_num=170000)
        with patch("experiments.ebpf.hybrid_sentinel.subprocess.run"), \
                patch("experiments.ebpf.hybrid_sentinel.subprocess.check_output",
                      side_effect=["/headers", json.dumps(layout)]):
            with self.assertRaisesRegex(ValueError, "ABI"):
                stats_layout("pg_config")

    def test_even_pg12_rejects_incompatible_prefix_offsets(self):
        layout = dict(self.LAYOUT, commits_offset=20)
        with patch("experiments.ebpf.hybrid_sentinel.subprocess.run"), \
                patch("experiments.ebpf.hybrid_sentinel.subprocess.check_output",
                      side_effect=["/headers", json.dumps(layout)]):
            with self.assertRaisesRegex(ValueError, "ABI"):
                stats_layout("pg_config")

    def test_workload_roles_do_not_allow_double_or_unknown_pressure(self):
        for roles in ((), ("pressure", "pressure"), ("shell",), ("pressure",)):
            with self.assertRaises(ValueError):
                Workers({}, None, None, None, None, 10, 1600, 2500, False, roles=roles)


class HonestReports(unittest.TestCase):
    def test_true_ebpf_alarm_replaces_only_stale_operator_selected_metadata(self):
        metric = {"blocks_read": {"status": "completed", "series": [{"value": 123}]}}
        payload = {"alert": None, "database_metrics": metric,
                   "prometheus_capture": {"alerts": {"status": "operator_selected_controlled_case"}},
                   "steps": [{"step": 4, "status": "not_triggered", "data": {}}]}
        case = {"anomaly": {"samples": {}}}
        alert = {"source": "ebpf_native_PG12_received_statistics",
                 "signals": [{"rule": "blocks_surge"}], "not_claimed": "root cause",
                 "observed_at_unix_ns": 100000000000}
        trigger = attach_trigger_metadata(payload, case, alert)
        self.assertTrue(payload["alert"]["triggered"])
        self.assertEqual(trigger["source"], alert["source"])
        self.assertEqual(case["anomaly"]["samples"]["trigger"], trigger)
        self.assertIs(payload["database_metrics"], metric)
        self.assertEqual(payload["steps"][0]["status"], "completed")
        self.assertEqual(payload["prometheus_capture"]["alerts"]["status"], "completed")

    def test_six_functional_successes_are_not_six_positive_TPS_gains_or_low_overhead(self):
        case = {"collection_chain_completed": True, "pipeline_completed": True,
                "validated_gain": False}
        with patch("experiments.ebpf.hybrid_report.Path.glob",
                   return_value=[Path(str(i)) / "result.json" for i in range(6)]), \
                patch("experiments.ebpf.hybrid_report.audit_case", return_value=case):
            value = report("synthetic-unit-only")
        self.assertEqual(value["collection_chain_completed_count"], 6)
        self.assertEqual(value["pipeline_completed_count"], 6)
        self.assertEqual(value["validated_gain_count"], 0)
        self.assertFalse(value["same_TPS_benefit_proven"])
        self.assertFalse(value["sentinel_low_overhead_proven"])


if __name__ == "__main__":
    unittest.main()
