"""Regression tests for the continuous baseline/pressure lab modes."""
import unittest

import lab_controller


class ContinuousTpccConfigTests(unittest.TestCase):
    def setUp(self):
        self.controller = object.__new__(lab_controller.LabController)
        self.case = {"scenario_id": "tp_payment_hot", "title": "Payment hot", "clients": 3}

    def test_baseline_mode_runs_until_switched(self):
        config = self.controller._tpcc_config(
            self.case,
            {"continuous_seconds": 120, "auto_tune_seconds": 60},
            continuous_mode="baseline",
        )
        self.assertTrue(config["continuous"])
        self.assertEqual(config["continuous_mode"], "baseline")
        self.assertFalse(config["skip_baseline"])
        self.assertEqual(config["baseline_seconds"], 120)
        self.assertEqual(config["pressure_after_seconds"], 0)

    def test_pressure_mode_auto_tunes_then_keeps_running(self):
        config = self.controller._tpcc_config(
            self.case,
            {"continuous_seconds": 120, "auto_tune_seconds": 60},
            continuous_mode="pressure",
        )
        self.assertTrue(config["continuous"])
        self.assertEqual(config["continuous_mode"], "pressure")
        self.assertTrue(config["skip_baseline"])
        self.assertEqual(config["pressure_before_seconds"], 60)
        self.assertEqual(config["pressure_after_seconds"], 120)
        self.assertEqual(config["auto_tune_seconds"], 60)
        self.assertIn("keep it active", config["tuning_observation"])

    def test_one_shot_config_keeps_the_bounded_three_phase_shape(self):
        config = self.controller._tpcc_config(self.case, {})
        self.assertFalse(config.get("continuous", False))
        self.assertEqual(config["baseline_seconds"], 60)
        self.assertEqual(config["pressure_before_seconds"], 90)
        self.assertEqual(config["pressure_after_seconds"], 90)


if __name__ == "__main__":
    unittest.main()
