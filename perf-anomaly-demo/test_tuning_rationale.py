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


class RationaleTranslationTests(unittest.TestCase):
    def test_merge_translation_keeps_chinese_and_adds_english(self):
        from tuning_rationale import merge_translation
        value = {
            "root_cause": "结论：中文根因",
            "validation": "复测通过",
            "actions": [{"parameter": "work_mem", "reason": "中文原因", "evidence": ["x"]}],
            "not_applied": [{"parameter": "max_connections", "reason": "需要重启"}],
        }
        translated = {
            "root_cause": "Conclusion: English root cause",
            "validation": "Replay passed",
            "actions": [{"parameter": "work_mem", "reason": "English reason"}],
            "not_applied": [{"parameter": "max_connections", "reason": "Requires restart"}],
        }
        merged = merge_translation(value, translated)
        self.assertEqual(merged["root_cause"], "结论：中文根因")
        self.assertEqual(merged["root_cause_en"], "Conclusion: English root cause")
        self.assertEqual(merged["validation_en"], "Replay passed")
        self.assertEqual(merged["actions"][0]["reason"], "中文原因")
        self.assertEqual(merged["actions"][0]["reason_en"], "English reason")
        self.assertEqual(merged["not_applied"][0]["reason_en"], "Requires restart")
        self.assertEqual(merged["actions"][0]["evidence"], ["x"])

    def test_merge_translation_without_result_is_a_noop(self):
        from tuning_rationale import merge_translation
        value = {"root_cause": "中文", "actions": [], "not_applied": []}
        self.assertEqual(merge_translation(value, None), value)
