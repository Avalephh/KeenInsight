"""Regression tests for the tuning-action rationale builder."""
import unittest

from tuning_rationale import format_section, merge_explanation, same_value


class TuningRationaleTests(unittest.TestCase):
    def test_same_value_normalizes_quotes_and_numbers(self):
        self.assertTrue(same_value("0", 0))
        self.assertTrue(same_value("''", ""))
        self.assertTrue(same_value("16384", 16384.0))
        self.assertFalse(same_value("off", "on"))
        self.assertFalse(same_value(None, "off"))

    def test_merge_keeps_authoritative_after_values_and_llm_reason(self):
        bundle = {
            "changed": ["work_mem"],
            "actions": [{
                "parameter": "work_mem",
                "before": "4096 kB",
                "after": "6143 kB",
                "applied": True,
                "skipped_reason": None,
                "evidence": ["fixture_function (src/backend/x.c:1)"],
            }],
            "skipped": {},
            "hot_functions": [{"Function": "fixture_function"}],
            "selected_reasoning": "",
            "comparison": {},
        }
        generated = {
            "root_cause": {"结论": "测试根因"},
            "actions": [{"parameter": "work_mem", "reason": "排序内存不足",
                         "evidence": ["llm-only"]}],
            "not_applied": [],
            "validation": "复测通过",
        }
        value = merge_explanation(bundle, generated, "gpt-6.1-sol")
        self.assertEqual(value["actions"][0]["after"], "6143 kB")
        self.assertEqual(value["actions"][0]["reason"], "排序内存不足")
        self.assertEqual(value["actions"][0]["evidence"],
                         ["fixture_function (src/backend/x.c:1)"])
        self.assertIn("结论：测试根因", value["root_cause"])
        self.assertEqual(value["validation"], "复测通过")

    def test_format_section_translates_known_labels_and_booleans(self):
        text = format_section({"baseline_ms": 12.345, "configuration_status": "applied_and_restored",
                               "restored_within_20pct_of_baseline": True})
        self.assertIn("基线耗时：12.35", text)
        self.assertIn("配置状态：已应用并恢复", text)
        self.assertIn("恢复在基线 ±20% 内：是", text)


if __name__ == "__main__":
    unittest.main()
