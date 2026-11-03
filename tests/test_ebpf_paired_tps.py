"""Controlled collection/TPS contracts; no DB/BPF/network/model calls."""
import json
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from experiments.ebpf.equivalent_sampling import folded_rows
from experiments.ebpf.paired_tps import (
    host_values, paired_result, pressure_comparison, record_series,
)
from experiments.ebpf.buffer_observer import symbol_address
from experiments.ebpf.paired_report import metric_available, weighted_rate_gap
from experiments.ebpf.paired_resume import ReloadAwareAdmin
from sysinsight_original_llm import (
    compact_sysinsight_observation, live_workload_template, resource_snapshot,
)


class PeriodWeights(unittest.TestCase):
    def test_original_flamegraph_period_weights_are_not_sample_counts(self):
        rows = [
            {"user_frames": ["read"], "kernel_frames": [], "sample_count": 5,
             "sample_period_weight": 100},
            {"user_frames": ["write"], "kernel_frames": [], "sample_count": 5,
             "sample_period_weight": 900},
        ]
        self.assertEqual(folded_rows(rows), {"read": 5, "write": 5})
        self.assertEqual(folded_rows(rows, "sample_period_weight"), {"read": 100, "write": 900})
        gap = weighted_rate_gap(rows)
        self.assertEqual(abs(gap["top_differences"][0]["difference_percentage_points"]), 40)

    def test_exported_PIE_counter_uses_target_load_bias(self):
        with patch("experiments.ebpf.buffer_observer.subprocess.check_output", side_effect=[
            "00000000007eaa20 B pgBufferUsage\n", "Type: DYN (Shared object file)\n"]), \
                patch("pathlib.Path.read_text",
                      return_value="55000000-55001000 r--p 00000000 00:00 1 /pg/postgres\n"):
            self.assertEqual(symbol_address("/pg/postgres", 42), 0x55000000 + 0x7eaa20)

    def test_missing_PIE_mapping_fails_instead_of_reading_a_wrong_address(self):
        with patch("experiments.ebpf.buffer_observer.subprocess.check_output", side_effect=[
            "00000000007eaa20 B pgBufferUsage\n", "Type: DYN (Shared object file)\n"]), \
                patch("pathlib.Path.read_text", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "load bias"):
                symbol_address("/pg/postgres", 42)


class ModelInputs(unittest.TestCase):
    def test_database_counter_with_null_values_is_not_reported_as_collected(self):
        self.assertFalse(metric_available(record_series(None, None, 100, 115, "missing")))
        self.assertTrue(metric_available(record_series(0, 0, 100, 115, "measured zero")))

    def test_canonical_flat_database_metrics_reach_original_model(self):
        metric = record_series(1, 2, 100, 115, "original PG view")
        for database in ({"blocks_read": metric}, {"prometheus": {"blocks_read": metric}}):
            compact = compact_sysinsight_observation({"database_metrics": database})
            record = compact["database_metrics"]["blocks_read"]
            self.assertEqual(record["series"][0]["first"]["value"], 1)
            self.assertEqual(record["series"][0]["last"]["value"], 2)

    def test_measured_BPF_cache_and_IO_are_consumed_not_replaced_with_unknown(self):
        measured = {"schema": "keeninsight.measured-resources.v1", "cpu": 25, "readIO": 123,
                    "writeIO": 0, "hit": 97, "virtualMem": 30, "physical": 2,
                    "source": "bounded BPF pgBufferUsage"}
        with patch("sysinsight_original_llm.host_cpu_percent", return_value=99):
            value = resource_snapshot({"collection": {"type": "ebpf"}, "resource_metrics": measured})
        self.assertEqual(value["cpu"], 25)
        self.assertEqual(value["readIO"], 123)
        self.assertEqual(value["writeIO"], 0)
        self.assertEqual(value["hit"], 97)

    def test_missing_measured_cache_stays_unknown(self):
        with patch("sysinsight_original_llm.host_cpu_percent", return_value=99):
            value = resource_snapshot({"collection": {"type": "ebpf"},
                                      "resource_metrics": {"schema": "keeninsight.measured-resources.v1",
                                                           "hit": None}})
        self.assertIsNone(value["hit"])
        self.assertIsNone(value["readIO"])

    def test_declared_workload_context_identical_for_both_collectors(self):
        template = "- Benchmark tool: tpcc;\n - Data scale: 100 w;\n - Concurrent threads: 16 threads;"
        declared = {"warehouses": 5, "business_clients": 2, "pressure_clients": 6}
        values = [live_workload_template(template, {"mode": "live_alert", "collection": {"type": arm},
                                                   "controlled_workload": declared})
                  for arm in ("perf", "ebpf")]
        self.assertEqual(values[0], values[1])
        self.assertNotIn("100 w", values[0])

    def test_host_window_uses_elapsed_measurements_not_model_call_time(self):
        before = {"mono": 1, "at": 100, "cpu_total": 1000, "cpu_idle": 500,
                  "disk_read": 100, "disk_write": 200}
        after = {"mono": 3, "at": 102, "cpu_total": 1200, "cpu_idle": 600,
                 "disk_read": 300, "disk_write": 800, "memory_available": 30,
                 "memory_total": 100, "load1": 1}
        result = host_values(before, after)
        self.assertEqual(result["cpu_percent"], 50)
        self.assertEqual(result["disk_read_bytes_per_second"], 100)
        self.assertEqual(result["memory_used_percent"], 70)


class GainComparisons(unittest.TestCase):
    def test_reload_wait_requires_observed_original_values_not_an_assumed_restore(self):
        query = "SELECT json_object_agg() FROM pg_settings WHERE name IN ('max_wal_size')"
        raw = lambda value: json.dumps({"max_wal_size": {"value": str(value), "type": "integer"}})
        connection = Mock()
        connection.execute.side_effect = [raw(3079), raw(1024)]
        admin = ReloadAwareAdmin(connection, {"max_wal_size": 1024})
        with patch("experiments.ebpf.paired_resume.time.sleep"):
            self.assertEqual(json.loads(admin.execute(query, value=True))["max_wal_size"]["value"], "1024")
        self.assertEqual(len(admin.observations), 1)
        self.assertEqual(connection.execute.call_count, 2)

    def test_reload_wait_never_turns_persistent_mismatch_into_success(self):
        connection = Mock()
        connection.execute.return_value = json.dumps({"max_wal_size": {"value": "3079", "type": "integer"}})
        admin = ReloadAwareAdmin(connection, {"max_wal_size": 1024}, timeout=0)
        with self.assertRaisesRegex(RuntimeError, "still mismatches"):
            admin.execute("SELECT json_object_agg() FROM pg_settings WHERE name IN ('max_wal_size')", value=True)

    @staticmethod
    def arm(gains, valid=True):
        return {"status": "completed", "retest": {
            "measurement_valid": valid, "validated_gain": valid and min(gains) >= .05,
            "median_gain_ratio": sorted(gains)[len(gains) // 2],
            "blocks": [{"gain_ratio": value} for value in gains]}}

    def test_zero_gain_closeness_is_not_six_case_positive_benefit_reproduction(self):
        result = paired_result(self.arm([0, 0, 0]), self.arm([0, 0, 0]))
        self.assertTrue(result["exploratory_comparable"])
        self.assertFalse(result["both_positive_gains_validated"])
        self.assertFalse(result["same_TPS_benefit_proven"])

    def test_invalid_drifting_measurement_cannot_pass_even_with_equal_gains(self):
        result = paired_result(self.arm([.1, .1, .1]), self.arm([.1, .1, .1], valid=False))
        self.assertFalse(result["exploratory_comparable"])
        self.assertEqual(result["status"], "not_demonstrated")

    def test_nonoverlapping_benefits_fail_margin(self):
        result = paired_result(self.arm([.2, .2, .2]), self.arm([0, 0, 0]))
        self.assertFalse(result["exploratory_comparable"])
        self.assertAlmostEqual(result["difference_ratio"], -.2)

    def test_incomplete_arm_is_not_an_equivalence_result(self):
        self.assertEqual(paired_result(self.arm([0, 0]), {"status": "failed"})["status"], "incomplete")

    def test_pressure_gate_still_rejects_changed_completed_pressure(self):
        def trial(tps, external):
            return {"business": {"steady_tps": tps}, "pressure": {"steady_tps": external},
                    "pressure_policy": {"pressure_offered_tps": 2500}}
        result = pressure_comparison(trial(1000, 1000), trial(1500, 2500))
        self.assertFalse(result["comparison_valid"])
        self.assertEqual(result["pressure_mode"], "open_loop_fixed_rate")


if __name__ == "__main__":
    unittest.main()
