"""No root/BCC/PostgreSQL needed. Real attachment is an explicit lab command."""
import ctypes as ct
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from experiments.ebpf.catalog import CORE, choose_probes, prune_candidate
from experiments.ebpf.collector import (Event, Sink, decode_event, execution_id,
                                        mark_coverage, mark_measurements, process_identity,
                                        resolve_collection, source, validate_args)
from experiments.ebpf.overhead import WORKLOADS, percentile, summarize
from experiments.ebpf.selection import fit, train_file


class ProbeSelectionTest(unittest.TestCase):
    def test_low_fanout_default_never_enables_per_tuple_or_global_hooks(self):
        probes, source = choose_probes("events")
        self.assertEqual(set(probes), {"temp_file_create", "wal_flush"})
        self.assertNotIn("lasso", source.replace("not_lasso", ""))
        self.assertEqual(choose_probes("boundary")[0], [])

    def test_bootstrap_is_not_mislabeled_as_lasso(self):
        names, kind = choose_probes("selected")
        self.assertEqual(kind, "untrained_bootstrap_not_lasso")
        self.assertTrue(set(CORE).issubset(names))

    def test_lasso_preserves_boundaries_and_core_but_selects_optional_probes(self):
        model = {"schema": "keeninsight.lasso.v1",
                 "coefficients": {"hash_spill": -.7, "wal_flush": 0}}
        names, kind = choose_probes("selected", model)
        self.assertEqual(kind, "lasso")
        self.assertEqual(set(names), set(CORE) | {"hash_spill"})
        self.assertEqual(prune_candidate(names, model), "hash_spill")
        self.assertIsNone(prune_candidate(CORE, model))

    def test_bad_selector_rejected(self):
        for weights in ({"unknown": 1}, {"hash_spill": math.nan}, {"wal_flush": math.inf}):
            with self.assertRaises(ValueError):
                choose_probes("selected", {"schema": "keeninsight.lasso.v1",
                                            "coefficients": weights})

    def test_standardized_lasso_finds_nonconstant_signal(self):
        rows = [{"trial_id": str(i), "features": {"hash_spill": i, "wal_flush": 4},
                 "degradation": i * 2} for i in range(12)]
        model = fit(rows, alpha=.05)
        self.assertGreater(model["coefficients"]["hash_spill"], 1)
        self.assertEqual(model["coefficients"]["wal_flush"], 0)
        self.assertIn("training_only", model["validation"])
        self.assertEqual(len(model["training_trial_ids"]), 12)

    def test_unknown_missing_nonfinite_duplicate_trials_are_not_zeros(self):
        good = [{"trial_id": str(i), "features": {"hash_spill": i}, "degradation": i}
                for i in range(3)]
        bad = [
            good[:2], [good[0], good[0], good[2]],
            [good[0], good[1], {**good[2], "features": {}}],
            [good[0], good[1], {**good[2], "degradation": math.nan}],
            [good[0], good[1], {**good[2], "features": {"fake": 1}}],
        ]
        for rows in bad:
            with self.assertRaises(ValueError):
                fit(rows)

    def test_training_provenance_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "trials.jsonl"
            destination = Path(tmp) / "model.json"
            source_path.write_text("\n".join(json.dumps({
                "trial_id": str(i), "features": {"hash_spill": i}, "degradation": i
            }) for i in range(6)))
            result = train_file(source_path, destination)
            self.assertEqual(len(result["training_sha256"]), 64)
            self.assertEqual(json.loads(destination.read_text()), result)
            with self.assertRaises(FileExistsError):
                train_file(source_path, destination)


class EventContractTest(unittest.TestCase):
    def test_lightweight_defaults_do_not_attach_every_query_or_scheduler(self):
        config = resolve_collection(SimpleNamespace())
        self.assertEqual(config, {"scope": "system", "query_output": "aggregate",
                                  "metric_mode": "counts", "cpu_mode": "off"})
        full = resolve_collection(SimpleNamespace(detail_level="full", scope="query"))
        self.assertEqual(full["metric_mode"], "timed")
        self.assertEqual(full["query_output"], "all")
        self.assertEqual(full["cpu_mode"], "sched")

    def test_entry_counts_and_disabled_cpu_are_missing_not_fake_zero_measurements(self):
        event = self.event(2)
        event.metrics[0].calls = 5
        record = self.decode(event)
        mark_measurements(record, resolve_collection(SimpleNamespace()))
        self.assertNotIn("on_cpu_ns", record)
        self.assertNotIn("off_cpu_ns", record)
        self.assertNotIn("inclusive_duration_ns", record["metrics"]["vfs_read"])
        self.assertNotIn("value", record["metrics"]["vfs_read"])
        self.assertEqual(record["metrics"]["vfs_read"]["calls"], 5)

    def decode(self, event, size=None):
        return decode_event("run", ct.addressof(event), size or ct.sizeof(event),
                            ("vfs_read", "hash_spill"))

    def event(self, kind):
        value = Event()
        value.kind, value.pid, value.tid, value.depth = kind, 10, 10, 1
        value.process_start_ticks, value.query_start_ns, value.timestamp_ns = 20, 100, 300
        return value

    def test_query_identity_never_uses_sql_text_or_just_pid(self):
        self.assertNotEqual(execution_id("r", 1, 2, 3), execution_id("r", 1, 4, 3))
        self.assertNotEqual(execution_id("r", 1, 2, 3), execution_id("r", 1, 2, 4))
        record = self.decode(self.event(1))
        self.assertIsNone(record["pg_query_id"])
        self.assertEqual(record["query_id_source"], "unavailable")
        self.assertNotIn("sql_prefix", record)

    def test_unsigned_pg_query_id_not_rounded_by_javascript(self):
        value = self.event(1)
        value.query_id = 2 ** 64 - 1
        self.assertEqual(self.decode(value)["pg_query_id"], str(2 ** 64 - 1))

    def test_totals_have_explicit_scope_and_unavailable_not_zero(self):
        value = self.event(2)
        value.duration_ns, value.cpu_ns = 200, 125
        value.metrics[0].calls, value.metrics[0].value = 4, 1024
        record = self.decode(value)
        self.assertEqual(record["off_cpu_ns"], 75)
        self.assertEqual(record["metrics"]["vfs_read"]["value"], 1024)
        self.assertNotIn("wal_flush", record["metrics"])
        self.assertIn("innermost", record["resource_scope"])

    def test_immediate_alert_is_not_double_counted_as_an_extra_measurement(self):
        value = self.event(4)
        value.flags, value.metric, value.count = 5, 0, 1
        record = self.decode(value, Event.metrics.offset)
        self.assertEqual(record["reasons"], ["call_latency", "long_running_query"])
        self.assertIn("notification_only", record["accounting"])

    def test_bad_event_lengths_or_kind_rejected(self):
        value = self.event(1)
        for length in (1, Event.metrics.offset - 1, ct.sizeof(Event) - 1):
            with self.assertRaises(ValueError):
                self.decode(value, length)
        value.kind = 99
        with self.assertRaises(ValueError):
            self.decode(value)

    def test_legacy_perf_transport_padding_is_not_part_of_the_payload(self):
        value = self.event(1)
        raw = ct.create_string_buffer(ct.string_at(ct.addressof(value), ct.sizeof(value)) + b"\0" * 4)
        decoded = decode_event("run", ct.addressof(raw), ct.sizeof(value) + 4, ())
        self.assertEqual(decoded["type"], "query_begin")
        self.assertNotIn("sql_prefix", decoded)

    def test_compact_boundary_messages_omit_unused_payload(self):
        value = self.event(1)
        self.assertEqual(self.decode(value, Event.metrics.offset)["type"], "query_begin")
        value.kind = 2
        value.metrics[0].calls = 8
        record = self.decode(value, Event.sql.offset)
        self.assertEqual(record["metrics"]["vfs_read"]["calls"], 8)
        self.assertNotIn("sql_prefix", record)

    def test_system_totals_have_no_fake_query_identity(self):
        value = self.event(5)
        value.depth, value.flags = 0, 2
        value.metrics[0].calls = 10
        record = self.decode(value, Event.sql.offset)
        self.assertEqual(record["type"], "backend_end")
        self.assertEqual(record["reason"], "backend_exit")
        self.assertNotIn("execution_id", record)
        self.assertIsNone(record["query_start_ns"])
        self.assertIn("activity_id", record)

    def test_abort_is_not_a_successful_completion(self):
        value = self.event(3)
        value.flags = 1
        record = self.decode(value)
        self.assertEqual(record["type"], "query_aborted")
        self.assertIn("abort", record["reason"])

    def test_sensitive_source_text_requires_actual_payload(self):
        value = self.event(1)
        value.sql = b"select 42"
        self.assertEqual(self.decode(value)["sql_prefix"], "select 42")

    def test_dynamic_detach_is_partial_or_missing_not_an_observed_zero(self):
        record = {"timestamp_ns": 30, "query_start_ns": 10, "metrics": {"hash_spill": {"calls": 1}}}
        mark_coverage(record, ["hash_spill"], {"hash_spill": 20})
        self.assertEqual(record["probe_coverage"]["hash_spill"], "partial_pruned_during_span")
        self.assertEqual(record["metrics"]["hash_spill"]["calls"], 1)
        record["query_start_ns"] = 25
        mark_coverage(record, ["hash_spill"], {"hash_spill": 20})
        self.assertEqual(record["probe_coverage"]["hash_spill"], "not_observed_pruned")
        self.assertNotIn("hash_spill", record["metrics"])


class SafetyTest(unittest.TestCase):
    def test_private_bounded_output_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sink = Sink(root, 64)
            sink.write({"type": "test"})
            with self.assertRaises(RuntimeError):
                sink.write({"large": "x" * 100})
            sink.close()
            self.assertEqual((root / "events.jsonl").stat().st_mode & 0o777, 0o600)
            self.assertEqual(len((root / "events.jsonl").read_text().splitlines()), 1)
            with self.assertRaises(FileExistsError):
                Sink(root, 64)

    def test_non_postgres_target_rejected(self):
        with self.assertRaises(ValueError):
            process_identity(os.getpid(), "/definitely/not/the/current/executable")

    def test_cli_bounds(self):
        good = dict(duration=10, report_seconds=1, long_query_ms=1000,
                    event_latency_ms=10, max_output_mb=1, collector_cpu_budget=5,
                    overhead_budget=2, event_bytes=10, pid=[42])
        validate_args(SimpleNamespace(**good))
        for name, value in (("duration", 3601), ("report_seconds", .001),
                            ("long_query_ms", float("nan")), ("pid", []),
                            ("pid", list(range(100))), ("event_bytes", 0)):
            with self.assertRaises(ValueError):
                validate_args(SimpleNamespace(**{**good, name: value}))

    def test_program_filters_before_collection_and_has_exit_cleanup(self):
        text = source()
        self.assertIn("targets.lookup", text)
        self.assertIn("targets.delete(&pid)", text)
        self.assertIn("q->start_ns != query_start", text)
        self.assertIn("QUERY_DEPTH 4", text)
        self.assertNotIn("bpf_get_stack", text)
        self.assertNotIn("bpf_trace_printk", text)

    def test_throughput_loss_and_latency_increase_are_not_interchanged(self):
        trials = []
        for repeat in range(3):
            for mode, qps, latency in (("baseline", 100, 10), ("events", 80, 12.5)):
                trials.append({"repeat": repeat, "workload": "short", "mode": mode,
                               "qps": qps, "mean_latency_ns": latency, "p99_latency_ns": latency,
                               "collector_cpu_percent": 0, "queries": 100, "warmup_queries": 5})
        summary = summarize(trials)["short"]["events"]
        self.assertAlmostEqual(summary["median_paired_qps_loss_percent"], 20)
        self.assertAlmostEqual(summary["median_paired_mean_latency_increase_percent"], 25)
        self.assertEqual(percentile([1, 2, 3, 4], .99), 4)

    def test_wal_cost_test_uses_logged_table_not_temporary_table(self):
        self.assertEqual(WORKLOADS["wal"], "INSERT INTO wal_fixture VALUES (1)")


if __name__ == "__main__":
    unittest.main()
