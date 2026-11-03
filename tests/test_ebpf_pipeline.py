"""Pipeline contract tests. No BPF, database, paid API or training assets."""
import ctypes as ct
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from experiments.ebpf.evidence import diagnosis
from experiments.ebpf.catalog import choose_probes
from experiments.ebpf.pipeline import (Journal, Replay, argument_parser, capture_detail,
                                       run_once, validate_arguments)
from experiments.ebpf.runtime import EventTail

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
from sysinsight_original_llm import compact_sysinsight_observation, prompt_resources, resource_snapshot
from sysinsight_source_evidence import render_source_evidence


def events(pid=42, birth=99, completed=2):
    result = []
    for index in range(completed):
        identity = "execution-" + str(index)
        common = {"pid": pid, "process_start_ticks": birth, "execution_id": identity}
        result += [
            {**common, "type": "query_begin", "sql_prefix": "SELECT count(*) FROM data"},
            {**common, "type": "query_end", "metrics": {"temp_file_read": {"calls": 10}},
             "probe_coverage": {"temp_file_read": "attached"}},
        ]
    return result


def arguments(output):
    args = argument_parser().parse_args(["--pid", "42", "--sql-file", "unused.sql",
                                        "--output", str(output), "--db", "postgres"])
    args.sql = "SELECT count(*) FROM data"
    args.allowed_knobs = ["work_mem"]
    args.baseline_settings = {}
    args.knob_limits = {"work_mem": [64, 65536]}
    args.dbms, args.pg_version = "postgresql", "12"
    return args


class EvidenceContracts(unittest.TestCase):
    def test_resident_spill_sentinel_does_not_keep_commit_or_tuple_probes(self):
        names, selection = choose_probes("resident")
        self.assertEqual(names, ["temp_file_create"])
        self.assertEqual(selection, "resident_temporary_file_sentinel")

    def test_only_completed_target_identity_and_explicit_replay_prefix_are_used(self):
        target = {"pid": 42, "start_ticks": 99}
        data = events() + events(43) + events(birth=100)
        result = diagnosis(data, target, "SELECT count(*) FROM data")
        self.assertEqual(result["status"], "recognized")
        self.assertEqual(result["observed_functions"], [{"function": "BufFileRead", "calls": 20}])
        self.assertEqual(len(result["completed_execution_ids"]), 2)
        self.assertIn("truncated", result["identity_limit"])
        self.assertEqual(diagnosis(data, target, "SELECT 1")["status"], "insufficient_evidence")

    def test_aborted_partial_or_unobserved_functions_do_not_trigger_tuning(self):
        for mutation in ("abort", "partial", "missing_begin"):
            data = events(completed=1)
            if mutation == "abort":
                data[-1]["type"] = "query_aborted"
            elif mutation == "partial":
                data[-1]["probe_coverage"]["temp_file_read"] = "partial_pruned_during_span"
            else:
                data = data[-1:]
            self.assertEqual(diagnosis(data, {"pid": 42, "start_ticks": 99},
                                      "SELECT count(*) FROM data")["status"], "insufficient_evidence")

    def test_completed_spans_are_deduplicated_and_begin_identity_must_match(self):
        data = events(completed=1)
        result = diagnosis(data + [data[-1]], {"pid": 42, "start_ticks": 99},
                           "SELECT count(*) FROM data")
        self.assertEqual(result["observed_functions"], [{"function": "BufFileRead", "calls": 10}])
        self.assertEqual(len(result["completed_execution_ids"]), 1)
        data[0]["process_start_ticks"] = 100
        self.assertEqual(diagnosis(data, {"pid": 42, "start_ticks": 99},
                                  "SELECT count(*) FROM data")["status"], "insufficient_evidence")

    def test_legacy_model_compactor_preserves_actual_ebpf_input(self):
        payload = {"collection": {"type": "ebpf"}, "ebpf_observations": {"calls": 10},
                   "tuning_context": {"objective": {"metric": "read_only_sql_duration_ms"}}}
        compact = compact_sysinsight_observation(payload)
        self.assertEqual(compact["collection"], payload["collection"])
        self.assertEqual(compact["ebpf_observations"], payload["ebpf_observations"])
        self.assertEqual(compact["database_metrics"], {})

    def test_ebpf_input_does_not_invent_zero_postgres_statistics(self):
        with patch("sysinsight_original_llm.host_cpu_percent", return_value=5):
            result = resource_snapshot({"collection": {"type": "ebpf"}})
        for name in ("readIO", "writeIO", "hit"):
            self.assertIsNone(result[name])

    def test_ebpf_source_prompt_never_claims_perf_samples(self):
        text = render_source_evidence({"semantics": {"runtime_observation": "ebpf_function_invocations"},
                                       "runtime_call_chains": [], "static_parameter_evidence": []})
        self.assertIn("Counts are invocations, not CPU samples", text)
        self.assertNotIn("call chains below are real perf", text)

    def test_legacy_numeric_formatter_renders_unknown_instead_of_inventing_zero(self):
        with patch("sysinsight_original_llm.host_cpu_percent", return_value=5):
            resources = prompt_resources({"collection": {"type": "ebpf"}})
        self.assertEqual(format(resources["readIO"], ".4f"), "unknown (not collected)")
        self.assertIn("unknown", json.dumps(resources))


class LifecycleContracts(unittest.TestCase):
    def test_event_tail_preserves_partial_lines_without_duplicate_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events"
            tail = EventTail(path)
            self.assertEqual(tail.poll(), [])
            path.write_text('{"type":')
            self.assertEqual(tail.poll(), [])
            with path.open("a") as handle:
                handle.write('"metric_alert"}\n{"n":2}\n')
            self.assertEqual(tail.poll(), [{"type": "metric_alert"}, {"n": 2}])
            self.assertEqual(tail.poll(), [])

    def fake_child(self, directory, data):
        child = Mock()
        child.started = time.monotonic()
        child.directory = directory / "detail"
        child.process.poll.side_effect = lambda: 0 if getattr(child, "_closed", False) else None
        child.events.poll.side_effect = [data, []]

        def close(check=False):
            child._closed = True
            return {"status": "completed", "perf_lost_notifications": 0, "bpf_health": {}}
        child._closed = False
        child.close.side_effect = close
        return child

    def test_detail_is_unloaded_as_soon_as_evidence_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = self.fake_child(root, events())
            callback = Mock()
            with patch("experiments.ebpf.pipeline.CollectorProcess", return_value=child):
                observed = capture_detail(arguments(root), root, {"pid": 42, "start_ticks": 99},
                                          Journal(root), callback)
            self.assertEqual(observed["detach_reason"], "diagnosis_evidence_complete")
            child.close.assert_called_once()
            callback.assert_called_once()
            self.assertTrue(json.loads((root / "workflow.json").read_text())["stages"][-1]["child_exited"])

    def test_event_budget_unloads_and_blocks_model_without_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = arguments(root)
            args.detail_max_events = 1
            child = self.fake_child(root, events(completed=1)[:1])
            callback = Mock()
            with patch("experiments.ebpf.pipeline.CollectorProcess", return_value=child):
                with self.assertRaisesRegex(RuntimeError, "sufficient diagnostic evidence"):
                    capture_detail(args, root, {"pid": 42, "start_ticks": 99}, Journal(root), callback)
            stages = json.loads((root / "workflow.json").read_text())["stages"]
            self.assertEqual(stages[-1]["reason"], "event_budget_exceeded")
            child.close.assert_called_once()
            callback.assert_called_once()

    def test_detail_shutdown_failure_still_stops_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = self.fake_child(root, events())
            child.close.side_effect = RuntimeError("shutdown failed")
            callback = Mock()
            with patch("experiments.ebpf.pipeline.CollectorProcess", return_value=child):
                with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
                    capture_detail(arguments(root), root, {"pid": 42, "start_ticks": 99},
                                   Journal(root), callback)
            callback.assert_called_once()

    def test_budget_exceeded_blocks_tuning_even_if_the_same_batch_contains_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = arguments(root)
            args.detail_max_events = 1
            child = self.fake_child(root, events())
            with patch("experiments.ebpf.pipeline.CollectorProcess", return_value=child):
                with self.assertRaisesRegex(RuntimeError, "sufficient diagnostic evidence"):
                    capture_detail(args, root, {"pid": 42, "start_ticks": 99}, Journal(root))
            child.close.assert_called_once()

    def test_expired_window_with_only_one_completed_query_does_not_tune(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = arguments(root)
            child = self.fake_child(root, [])
            child.started = time.monotonic() - args.detail_seconds - 1
            child.events.poll.side_effect = [events(completed=1)]
            with patch("experiments.ebpf.pipeline.CollectorProcess", return_value=child):
                with self.assertRaisesRegex(RuntimeError, "sufficient diagnostic evidence"):
                    capture_detail(args, root, {"pid": 42, "start_ticks": 99}, Journal(root))
            child.close.assert_called_once()

    def test_no_evidence_never_calls_model_and_resident_is_always_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "new"
            args = arguments(output)
            args.select_target = True
            resident = Mock()
            resident.events.poll.return_value = []
            resident.close.return_value = {"status": "completed"}
            resident.process.poll.return_value = 0
            with patch.dict(os.environ, {"SYSINSIGHT_GPT_API_KEY": "unit-test-not-a-credential"}), \
                    patch("experiments.ebpf.pipeline.inspect_server", return_value={"binary": "postgres"}), \
                    patch("experiments.ebpf.pipeline.process_identity", return_value={"pid": 42, "start_ticks": 99}), \
                    patch("experiments.ebpf.pipeline.CollectorProcess", return_value=resident), \
                    patch("experiments.ebpf.pipeline.capture_detail", side_effect=RuntimeError("no evidence")), \
                    patch("sysinsight_live_chain._api_result") as model:
                result = run_once(args)
            self.assertEqual(result["status"], "failed")
            model.assert_not_called()
            resident.close.assert_called_once()
            self.assertEqual(result["stages"][-1]["stage"], "resident_stopped")

    def test_transaction_local_measurement_always_rolls_back_on_query_failure(self):
        replay = Replay.__new__(Replay)
        replay.args = SimpleNamespace(measure_repeats=1)
        replay.session = Mock()
        replay._result_hash = Mock(side_effect=RuntimeError("query failed"))
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            replay.measure("SELECT 1", {"work_mem": "128kB"})
        self.assertEqual(replay.session.execute.call_args_list[-1].args[0], "ROLLBACK")
        self.assertEqual(replay.session.execute.call_args_list[0].args[0], "BEGIN READ ONLY")

    def test_unsafe_arguments_and_fake_model_fallback_are_rejected(self):
        args = arguments("unused")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "credentials"):
                validate_arguments(args)
        for name, value in (("detail_seconds", 61), ("detail_max_events", 0),
                            ("knob_limits", {"work_mem": [65536, 64]}),
                            ("baseline_settings", {"fsync": "off"})):
            bad = arguments("unused")
            setattr(bad, name, value)
            with patch.dict(os.environ, {"SYSINSIGHT_GPT_API_KEY": "unit-test-only"}):
                with self.assertRaises(ValueError):
                    validate_arguments(bad)


if __name__ == "__main__":
    unittest.main()
