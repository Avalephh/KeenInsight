"""Functional parity contracts, no database/BPF/API side effects."""
import csv
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from experiments.ebpf.equivalence import (
    POLICY, Workers, collect_phase, gain_control, parse_worker, startup_options,
    trigger, tune, validate_options,
)
from experiments.ebpf.gain_audit import control_order, control_summary, profile_quality
from experiments.ebpf.equivalent_profiles import compare, normal_profile, rates
from experiments.ebpf.equivalent_sampling import folded_rows, register_task_fd, validate
from experiments.ebpf.equivalent_resident import Resident
from experiments.ebpf.equivalent_acceptance import verify
from experiments.ebpf.pipeline import Journal
from experiments.ebpf.runtime import CollectorProcess

from sysinsight_source_evidence import render_source_evidence
from sysinsight_original_llm import live_workload_template


class SamplingParity(unittest.TestCase):
    def test_multi_backend_original_budget_is_allowed_not_smoke_sampler_limits(self):
        validate(list(range(42, 56)), 15, 300)
        for pids, duration, freq in (([], 15, 300), ([42, 42], 15, 300), ([1], 15, 300),
                                     ([42], float("nan"), 300), ([42], 121, 300),
                                     ([42], 15, 1001)):
            with self.assertRaises(ValueError):
                validate(pids, duration, freq)

    def test_folded_has_real_stack_order_and_retains_recursion_like_original_method(self):
        rows = [{"user_frames": ["leaf", "recursive", "recursive", "root"],
                 "kernel_frames": ["sys_leaf", "sys_root"], "sample_count": 7}]
        self.assertEqual(folded_rows(rows),
                         {"root;recursive;recursive;leaf;sys_root;sys_leaf": 7})

    def test_normal_envelope_uses_real_windows_and_missing_function_zero_only_within_measured_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b, out = (root / name for name in ("a.tsv", "b.tsv", "normal.csv"))
            a.write_text("Cycles\tFunction\tSampling Rate (%)\tAbsolute Count\n10\tread\t25.00%\n")
            b.write_text("Cycles\tFunction\tSampling Rate (%)\tAbsolute Count\n20\tread\t50.00%\n"
                         "10\twrite\t25.00%\n")
            normal_profile([a, b], out)
            with out.open() as handle:
                rows = {r["Function"]: r for r in csv.DictReader(handle)}
            self.assertEqual(float(rows["read"]["Min Sampling Rate (%)"]), 25)
            self.assertEqual(float(rows["read"]["Max Sampling Rate (%)"]), 50)
            self.assertEqual(float(rows["write"]["Average Sampling Rate (%)"]), 12.5)
            self.assertEqual(rates(a), {"read": 25})

    def test_real_stack_prompt_not_mislabeled_as_invocations_or_perf_record(self):
        rendered = render_source_evidence({"semantics": {
            "runtime_observation": "ebpf_cpu_stack_samples"}, "runtime_call_chains": []})
        self.assertIn("real on-CPU sampled stacks", rendered)
        self.assertNotIn("real perf folded-stack", rendered)

    def test_missing_activity_snapshot_does_not_claim_zero_backends_in_model_prompt(self):
        legacy = '- Benchmark tool: tpcc;\n - Data scale: 100 w;\n - Concurrent threads: 16 threads;'
        text = live_workload_template(legacy, {"mode": "live_alert", "collection": {"type": "ebpf"}})
        self.assertIn('"backend_sessions_in_alert_snapshot": null', text)
        self.assertIn("does not mean zero", text)
        self.assertNotIn("- Data scale: 100 w;", text)

    def test_all_task_FDs_are_owned_in_one_registry_for_detach_and_partial_attach_cleanup(self):
        bpf = Mock()
        bpf.open_perf_events = {}
        bpf._attach_perf_event.side_effect = [101, 102, RuntimeError("third attach failed")]
        pair = (1, 1)
        register_task_fd(bpf, pair, 42, 99, 300)
        register_task_fd(bpf, pair, 43, 99, 300)
        with self.assertRaises(RuntimeError):
            register_task_fd(bpf, pair, 44, 99, 300)
        self.assertEqual(bpf.open_perf_events[pair], {42: 101, 43: 102})
        self.assertEqual(bpf._attach_perf_event.call_args_list[0].args,
                         (99, 1, 1, 0, 300, 42, -1, -1))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            register_task_fd(bpf, pair, 42, 99, 300)

    def test_difference_calls_original_algorithm_and_audits_unseen_functions_without_fabricating_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, n, k = [root / name for name in ("anomaly.tsv", "normal.csv", "keys.tsv")]
            a.write_text("Cycles\tFunction\tSampling Rate (%)\tAbsolute Count\n"
                         "10\tread\t40.0%\n2\tnew_func\t8.0%\n")
            n.write_text("Function,Min Sampling Rate (%),Max Sampling Rate (%),Average Sampling Rate (%)\n"
                         "read,10,20,15\n")
            k.write_text("Function\tSample Rate(%)\tDiff From Mean\tChange\nread\t40\t25\t1\n")
            comparator, matcher, profile = Mock(), Mock(), Mock()
            comparator.compare_file_sample_rate.return_value = (k, [{"Function": "read"}])
            matcher.find_top_and_matched_functions.return_value = (
                ["read"], [{"knob_name": "work_mem"}], {"read": ["work_mem"]})
            profile.path.return_value = root / "original-association.json"
            with patch("experiments.ebpf.equivalent_profiles.original_root", return_value=root), \
                    patch("experiments.ebpf.equivalent_profiles.load", side_effect=[comparator, matcher]):
                d = compare(a, n, root, profile, normal_windows=2)
            comparator.compare_file_sample_rate.assert_called_once_with(str(a), str(n))
            self.assertEqual(d["source_compare"]["anomaly_only_functions_ignored_by_original"], ["new_func"])
            matcher.find_top_and_matched_functions.assert_called_once_with(str(k), str(profile.path()))

    def test_resident_detaches_owned_handles_without_symbol_lookup_after_backend_exit(self):
        resident = Resident.__new__(Resident)
        resident.targets = {42: {"pid": 42, "start_ticks": 99}}
        resident.event_names = {42: [b"entry_42", b"return_42"]}
        resident.bpf = Mock()
        allowed, states = Mock(), Mock()
        resident.bpf.__getitem__ = Mock(side_effect=lambda name: {"allowed": allowed, "states": states}[name])
        allowed.__delitem__, states.__delitem__ = Mock(), Mock()
        resident.remove([42])
        self.assertEqual(resident.targets, {})
        self.assertEqual([c.args[0] for c in resident.bpf.detach_uprobe_event.call_args_list],
                         [b"entry_42", b"return_42"])
        resident.bpf.detach_uprobe.assert_not_called()
        resident.bpf.detach_uretprobe.assert_not_called()


class TriggerParity(unittest.TestCase):
    def test_CPU_only_activity_triggers_without_spill_or_lock_events(self):
        rows = [{"type": "query_snapshot", "pid": pid, "depth": 1, "elapsed_ns": 1_000_000}
                for pid in (42, 43, 44)]
        result = trigger(rows, [42, 43, 44], "activity")
        self.assertEqual(result["reason"], "ebpf_active_executor_threshold")

    def test_selected_target_is_explicit_and_not_silent_insufficient_evidence_fallback(self):
        self.assertIsNone(trigger([], [42], "activity"))
        self.assertEqual(trigger([], [42], "selected")["source"], "explicit_selection")

    def test_long_query_general_trigger_is_not_a_root_cause_rule(self):
        rows = [{"type": "query_snapshot", "pid": 42, "depth": 1, "elapsed_ns": 200_000_000}]
        result = trigger(rows, [42], "activity")
        self.assertEqual(result["reason"], "ebpf_long_executor_threshold")
        self.assertIn("not a root-cause", result["semantics"])
        self.assertIsNone(trigger(rows, [43], "activity"))


class TPSParity(unittest.TestCase):
    def test_startup_options_only_accept_policy_numeric_and_boolean_values(self):
        self.assertEqual(startup_options({"work_mem": 8192, "jit": "off"}),
                         "-c jit=off -c work_mem=8192")
        self.assertEqual(startup_options(None), "")
        for settings in ({"fsync": "off"}, {"work_mem": "8192 -c fsync=off"},
                         {"work_mem": 1}, {"work_mem": float("nan")}, {"jit": "maybe"}):
            with self.assertRaises(ValueError):
                startup_options(settings)

    def test_candidate_does_not_add_per_transaction_SQL_or_inherit_PGOPTIONS(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.sql"
            original.write_text("BEGIN;\nSELECT 1;\nROLLBACK;\n")
            process = Mock()
            process.poll.return_value = None
            case = {"normal_sql": str(original), "clients": 2}
            for settings in (None, {"work_mem": 8192}):
                with patch("experiments.ebpf.equivalence.subprocess.Popen", return_value=process) as spawn, \
                        patch("experiments.ebpf.equivalence.process_ids", return_value=[42, 43]), \
                        patch.dict(os.environ, {"PGOPTIONS": "-c jit=on"}):
                    workers = Workers(case, root, {"binary": "/pg/postgres"},
                                      ["psql", "-h", "/tmp/test", "-p", "55432"], Mock(),
                                      8, 1600, 2500, False, settings=settings)
                    try:
                        self.assertEqual((root / "business.sql").read_bytes(), original.read_bytes())
                        self.assertEqual(spawn.call_args.kwargs["env"]["PGOPTIONS"],
                                         startup_options(settings))
                    finally:
                        workers.close()

    def test_anomaly_resource_probes_do_not_contaminate_CPU_differential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers = Mock()
            workers.ids = {"business": [42, 43], "pressure": [44]}
            workers.policy = {}
            workers.running.return_value = True
            detail = Mock()
            detail.process.poll.return_value = 0
            detail.close.return_value = {"bpf_health": {}, "perf_lost_notifications": 0}
            options = SimpleNamespace(profile_seconds=.1, trigger_timeout=.1, business_rate=1600,
                                      pressure_rate=2500, resident=Mock(), trigger_mode="selected",
                                      pg_config="pg_config", frequency=300, min_samples=10)
            with patch("experiments.ebpf.equivalence.Workers", return_value=workers), \
                    patch("experiments.ebpf.equivalence.CollectorProcess", return_value=detail) as spawn, \
                    patch("experiments.ebpf.equivalence.sample") as sampler, \
                    patch("experiments.ebpf.equivalence.function_counts", return_value=root / "counts"):
                def capture(*args, **kwargs):
                    spawn.assert_not_called()
                    (root / "anomaly/profile").mkdir()
                    return {"sample_count": 111, "all_probe_FDs_closed": True,
                            "folded_path": str(root / "profile.folded")}
                sampler.side_effect = capture
                journal = Journal(root)
                result = collect_phase(options, {}, root, {}, [], Mock(execute=Mock(return_value="55")),
                                       "anomaly", True, journal)
            self.assertEqual(result["profile"]["comparison_instrumentation"][
                "resource_probes_during_CPU_profile"], [])
            stages = [row["stage"] for row in journal.entries]
            self.assertLess(stages.index("anomaly_detail_detached"),
                            stages.index("targeted_resource_probes_attached"))
            workers.close.assert_called_once()

    def test_restored_baseline_drift_cannot_be_accepted_as_gain(self):
        before = {"median_business_tps": 1000}
        self.assertTrue(gain_control(before, {"median_business_tps": 1000})["valid"])
        self.assertTrue(gain_control(before, {"median_business_tps": 1050})["valid"])
        check = gain_control(before, {"median_business_tps": 800})
        self.assertFalse(check["valid"])
        self.assertEqual(check["reason"], "baseline_drift")
        self.assertFalse(gain_control({"median_business_tps": 0}, before)["valid"])

    def test_control_order_balances_all_six_cells_in_forward_and_reverse_rounds(self):
        self.assertEqual(control_order(1), list(reversed(control_order(0))))
        self.assertEqual(set(control_order(2)), set(control_order(0)))
        self.assertNotEqual(control_order(2), control_order(0))

    def test_control_summary_separates_resident_loss_and_repeated_SET_cost(self):
        trials = [{"repeat": 0, "mode": mode, "resident": enabled,
                   "business": {"steady_tps": value}, "pressure": {"steady_tps": 2500}}
                  for mode, value in (("baseline", 1000), ("startup", 1100), ("legacy_SET", 900))
                  for enabled, value in ((False, value), (True, value * .8))]
        result = control_summary(trials)
        comparison = result["within_round_comparisons"][0]
        self.assertAlmostEqual(comparison["resident_loss_percent_by_mode"]["baseline"], 20)
        self.assertAlmostEqual(comparison["candidate_gain_percent"]["False"], 10)

    def test_uprobe_path_fraction_is_weighted_samples_not_measured_overhead(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"sample_event": "task-clock", "health": {}}))
            (root / "samples.json").write_text(json.dumps({"rows": [
                {"sample_count": 3, "user_frames": ["SQL"], "kernel_frames": ["uprobe_dispatcher"]},
                {"sample_count": 7, "user_frames": ["SQL"], "kernel_frames": []}]}))
            result = profile_quality(root)
        self.assertEqual(result["uprobe_path_sample_percent"], 30)
        self.assertNotIn("overhead_percent", result)

    def test_missing_artifacts_cannot_pass_six_case_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify(tmp)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["passed_count"], 0)
            self.assertEqual(len(result["cases"]), 6)
            self.assertTrue((Path(tmp) / "acceptance.json").exists())

    def test_completed_business_TPS_not_SQL_span_counts_or_external_role(self):
        value = parse_worker("number of transactions actually processed: 100\n"
                             "tps = 19.8 (including connections establishing)\n"
                             "tps = 20.0 (excluding connections establishing)\n", 1600)
        self.assertEqual(value["tps"], 20)
        self.assertEqual(value["completed_transactions"], 100)
        self.assertEqual(value["offered_tps"], 1600)
        self.assertIn("not SQL spans", value["metric"])

    def test_failed_workload_does_not_produce_accepted_timing(self):
        for text in ("", "ERROR: boom\nnumber of transactions actually processed: 10\ntps = 3\n",
                     "number of transactions actually processed: 10\n"
                     "number of failed transactions: 1\ntps = 3\n"):
            with self.assertRaises(RuntimeError):
                parse_worker(text, 1600)

    def test_steady_TPS_reuses_original_warmup_and_median_not_final_whole_run_TPS(self):
        text = ("number of transactions actually processed: 100\n"
                "tps = 20 (excluding connections establishing)\n"
                + "".join("progress: {} s, {} tps, lat 1 ms\n".format(i, tps)
                          for i, tps in enumerate((1, 2, 30, 50, 40), 1)))
        result = parse_worker(text, 1600, duration=5)
        self.assertEqual(result["steady_tps"], 40)
        self.assertEqual(result["tps"], 20)
        self.assertEqual(result["steady_summary"]["warmup_seconds"], 3)

    def test_policy_has_no_durability_endpoint_or_restart_escape_hatch(self):
        self.assertTrue(POLICY.isdisjoint({"fsync", "synchronous_commit", "full_page_writes",
                                         "shared_buffers", "port", "unix_socket_directories",
                                         "listen_addresses", "max_connections"}))
        self.assertIn("jit", POLICY)
        self.assertIn("max_wal_size", POLICY)

    def test_resident_aggregate_does_not_force_every_SQL_boundary_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "capture"
            directory.mkdir()
            (directory / "manifest.json").write_text('{"status":"ready"}')
            process = Mock()
            process.poll.return_value = 0
            with patch("experiments.ebpf.runtime.subprocess.Popen", return_value=process) as spawn:
                child = CollectorProcess(directory, 42, "pg_config", "query", "six-resident", 15,
                                         query_output="aggregate")
                args = spawn.call_args.args[0]
                self.assertEqual(args[args.index("--query-output") + 1], "aggregate")
                child.close(check=False)

    def test_no_credentials_or_out_of_bounds_budget_has_no_model_fallback(self):
        options = SimpleNamespace(profile_seconds=15, frequency=300, normal_windows=2,
                                  measure_seconds=12, measure_repeats=2, max_candidates=1,
                                  min_samples=100, business_rate=1600, pressure_rate=2500,
                                  trigger_timeout=10, min_gain=.05)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "credentials"):
                validate_options(options)
        options.pressure_rate = 1000000
        with self.assertRaisesRegex(ValueError, "rate"):
            validate_options(options)

    def fake_candidate(self, configuration):
        return {"candidate_id": "candidate-000", "source": "original_LLM_ACQ_parser",
                "validation": {"valid": True, "configuration": configuration},
                "live_validation": {"status": "completed"}}

    def test_no_measured_gain_is_a_completed_workflow_not_a_fake_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = self.fake_candidate({"jit": "off"})
            applier = Mock()
            applier.state = {"status": "session_only_applied_and_ended"}
            applier.__enter__ = Mock(return_value={"session_configuration": {"jit": "off"},
                                                  "skipped_configuration": {}, "connection_configuration": {}})
            applier.__exit__ = Mock(return_value=False)
            options = SimpleNamespace(max_candidates=1, min_gain=.05)
            with patch("sysinsight_live_chain._candidate_and_validation",
                       return_value={"candidates": [candidate], "selected_candidate": candidate}), \
                    patch("pg_temporary_config.TemporaryPostgresConfiguration", return_value=applier), \
                    patch("experiments.ebpf.equivalence.native_settings", return_value=({"jit": "off"}, {})), \
                    patch("experiments.ebpf.equivalence.invariant_state", return_value={"stock": "stable"}), \
                    patch("experiments.ebpf.equivalence.measure", side_effect=[
                        {"median_business_tps": tps, "offered_pressure_tps": 2500,
                         "trials": [{"pressure": {"steady_tps": 2500}}]} for tps in (1020, 1000)]) as measure:
                result = tune(options, {}, root, {}, [], Mock(), {}, ["jit"],
                              {"median_business_tps": 1000, "offered_pressure_tps": 2500,
                               "trials": [{"pressure": {"steady_tps": 2500}}]}, Journal(root))
            self.assertEqual(result["status"], "completed_without_validated_gain")
            self.assertTrue(result["pipeline_completed"])
            self.assertIsNone(result["best"])
            self.assertEqual(measure.call_count, 2)
            applier.__exit__.assert_called_once()
            self.assertTrue(result["candidates"][0]["settings_restored"])

    def test_large_apparent_gain_with_drifted_control_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = self.fake_candidate({"jit": "off"})
            applier = Mock()
            applier.state = {}
            applier.__enter__ = Mock(return_value={"session_configuration": {"jit": "off"},
                                                  "skipped_configuration": {}, "connection_configuration": {}})
            applier.__exit__ = Mock(return_value=False)
            baseline = {"median_business_tps": 1000, "offered_pressure_tps": 2500,
                        "trials": [{"pressure": {"steady_tps": 2500}}]}
            with patch("sysinsight_live_chain._candidate_and_validation",
                       return_value={"candidates": [candidate], "selected_candidate": candidate}), \
                    patch("pg_temporary_config.TemporaryPostgresConfiguration", return_value=applier), \
                    patch("experiments.ebpf.equivalence.native_settings", return_value=({"jit": "off"}, {})), \
                    patch("experiments.ebpf.equivalence.invariant_state", return_value={}), \
                    patch("experiments.ebpf.equivalence.measure", side_effect=[
                        {**baseline, "median_business_tps": tps} for tps in (1600, 800)]):
                result = tune(SimpleNamespace(max_candidates=1, min_gain=.05), {}, root, {}, [], Mock(),
                              {}, ["jit"], baseline, Journal(root))
            self.assertTrue(result["pipeline_completed"])
            self.assertIsNone(result["best"])
            record = result["candidates"][0]
            self.assertEqual(record["status"], "inconclusive_measurement")
            self.assertEqual(record["gain_control"]["reason"], "baseline_drift")
            self.assertGreater(record["gain_ratio"], .05)

    def test_out_of_allowlist_candidate_is_never_applied_or_retested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = self.fake_candidate({"fsync": "off"})
            with patch("sysinsight_live_chain._candidate_and_validation",
                       return_value={"candidates": [candidate], "selected_candidate": candidate}), \
                    patch("pg_temporary_config.TemporaryPostgresConfiguration") as applier, \
                    patch("experiments.ebpf.equivalence.native_settings", return_value=({"jit": "off"}, {})), \
                    patch("experiments.ebpf.equivalence.invariant_state", return_value={}), \
                    patch("experiments.ebpf.equivalence.measure") as measure:
                result = tune(SimpleNamespace(max_candidates=1, min_gain=.05), {}, root, {}, [], Mock(),
                              {}, ["jit"], {"median_business_tps": 1000}, Journal(root))
            self.assertFalse(result["pipeline_completed"])
            applier.assert_not_called()
            measure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
