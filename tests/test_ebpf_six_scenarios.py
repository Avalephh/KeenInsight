"""Original six-case coverage and sampling contracts; no BPF/PG/API required."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.ebpf.catalog import BY_NAME, PROBES, choose_probes
from experiments.ebpf.collector import source
from experiments.ebpf.cpu_sample import validate_window
from experiments.ebpf.runtime import CollectorProcess
from experiments.ebpf.six_scenarios import (PLANS, association_matches, audit,
                                            counts_summary, original_cases)


class SixScenarioContracts(unittest.TestCase):
    def test_uses_exact_original_six_focus_cases_not_six_spill_substitutes(self):
        cases = original_cases()
        self.assertEqual(len(cases), 6)
        self.assertEqual({c["id"] for c in cases}, set(PLANS))
        self.assertEqual(cases[0]["id"], "tp_payment_hot")
        self.assertTrue(all(c["mode"] == "pgbench" for c in cases))
        self.assertTrue(all(c["repair"] == {} for c in cases))

    def test_resident_set_does_not_trap_high_fanout_read_lock_or_WAL_paths(self):
        names, _ = choose_probes("six-resident")
        self.assertEqual({BY_NAME[n].symbol for n in names},
                         {"ProcSleep", "CreateCheckPoint", "BufFileCreateTemp"})
        self.assertTrue(set(names).isdisjoint(
            {"vfs_read", "allocation", "lock_acquire", "wal_flush"}))

    def test_catalog_ABI_matches_BPF_bound_and_keeps_metric_mask_safe(self):
        self.assertEqual([p.id for p in PROBES], list(range(len(PROBES))))
        self.assertEqual(int(re.search(r"#define METRICS (\d+)", source()).group(1)), len(PROBES))
        self.assertLessEqual(len(PROBES), 16)

    def test_CPU_sampler_rejects_unbounded_duration_rate_and_multi_task_FD_overwrite(self):
        validate_window([42], 2, 49)
        for pids, seconds, freq in (([], 2, 49), ([42, 43], 2, 49), ([1], 2, 49),
                                   ([42], 6, 49), ([42], float("nan"), 49),
                                   ([42], 2, 1000), ([42], 2, 0)):
            with self.assertRaises(ValueError):
                validate_window(pids, seconds, freq)

    def test_audit_does_not_claim_automatic_CPU_trigger_or_TPS_tuning_acceptance(self):
        report = audit({"candidate_symbols": {p.name: True for p in PROBES}})
        self.assertEqual(len(report["cases"]), 6)
        for case in report["cases"]:
            self.assertIn("not_migrated", case["current_tuning_acceptance"])
            self.assertIn("CPU-only", case["automatic_trigger_limit"])
        self.assertEqual(sum(c["bounded_CPU_sampler"] for c in report["cases"]), 4)

    def test_backend_snapshots_are_cumulative_not_independent_event_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            events = [
                {"type": "backend_snapshot", "pid": 42, "process_start_ticks": 99,
                 "metrics": {"lock_wait": {"calls": n}}} for n in (1, 2, 3)]
            events.append({**events[-1], "type": "backend_final"})
            events.append({**events[-1], "pid": 43})
            (path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            self.assertEqual(counts_summary(path), {"lock_wait": {"calls": 6}})

    def test_missing_source_association_is_unknown_not_no_matching_knobs(self):
        self.assertEqual(association_matches(["ProcSleep"], None)["status"], "unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "association.json"
            path.write_text(json.dumps([{"knob_name": "lock_timeout", "data_flow_functions": ["ProcSleep"]}]))
            result = association_matches(["ProcSleep"], path)
            self.assertEqual(result["matches"], [{"knob": "lock_timeout", "functions": ["ProcSleep"]}])
            self.assertIn("not a proven cause", result["semantics"])

    def test_multi_process_collector_uses_repeated_PID_options_not_positional_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "capture"
            directory.mkdir()
            (directory / "manifest.json").write_text('{"status":"ready"}')
            process = Mock()
            process.poll.return_value = 0
            with patch("experiments.ebpf.runtime.subprocess.Popen", return_value=process) as spawn:
                child = CollectorProcess(directory, [42, 43], "pg_config", "system",
                                         "six-resident", 2)
                args = spawn.call_args.args[0]
                self.assertEqual(args.count("--pid"), 2)
                self.assertIn(["--pid", "42", "--pid", "43"],
                              [args[i:i + 4] for i in range(len(args) - 3)])
                child.close(check=False)


if __name__ == "__main__":
    unittest.main()
