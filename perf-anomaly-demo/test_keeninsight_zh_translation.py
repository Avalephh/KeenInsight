"""Regression tests for the Chinese portal's runtime translation layer."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "keeninsight_web_zh" / "portal-zh.js"

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
let src = fs.readFileSync(process.argv[1], 'utf8');
src = src.replace(
  'const entries = Object.entries(map).sort((a,b)=>b[0].length-a[0].length);',
  'globalThis.__map=map;\n  const entries = Object.entries(map).sort((a,b)=>b[0].length-a[0].length);'
);
src = src.replace('const replace = value => {', 'const replace = globalThis.__replace = value => {');
const sandbox = {
  document: {
    documentElement: {}, readyState: 'loading', addEventListener() {},
    createTreeWalker() { return {nextNode() { return null; }}; },
    body: {}, querySelectorAll() { return []; }
  },
  location: {pathname: ''}, NodeFilter: {SHOW_TEXT: 4},
  MutationObserver: class { observe() {} disconnect() {} },
  window: {setTimeout() {}}, console
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const value of cases) out[value] = sandbox.__replace(value);
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class ChineseTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = [
            "SHAP Model Analysis Results",
            "Alerts",
            "Results",
            "Loading live host records…",
            "Time-series monitoring remains in Grafana. Differential profiling and model SHAP attribution are distinct evidence sources.",
            "Cluster:",
            "Background 12.5% → prediction 34.2%",
            "Total 5 records · 1–5",
        ]
        result = subprocess.run(
            ["node", "-e", HARNESS, str(SCRIPT), json.dumps(cls.cases)],
            check=True, capture_output=True, text=True,
        )
        cls.values = json.loads(result.stdout)

    def test_known_mixed_labels_are_fully_translated(self):
        self.assertEqual(self.values["SHAP Model Analysis Results"], "SHAP 模型分析结果")
        self.assertEqual(self.values["Alerts"], "告警")
        self.assertEqual(self.values["Loading live host records…"], "正在加载主机记录…")
        self.assertTrue(self.values["Time-series monitoring remains in Grafana. Differential profiling and model SHAP attribution are distinct evidence sources."].startswith("时序监控仍在 Grafana"))
        self.assertEqual(self.values["Cluster:"], "实例：")
        self.assertEqual(self.values["Background 12.5% → prediction 34.2%"], "背景值 12.5% → 预测值 34.2%")

    def test_plural_labels_do_not_leave_a_stray_suffix(self):
        self.assertEqual(self.values["Results"], "Results")
        self.assertNotIn("告警s", self.values["Alerts"])
        self.assertEqual(self.values["Total 5 records · 1–5"], "共 5 条记录 · 1–5")

    def test_translation_does_not_mix_cjk_and_latin_inside_a_word(self):
        for source in ("SHAP Model Analysis Results", "Alerts", "Cluster:"):
            value = self.values[source]
            self.assertNotRegex(value, r"[一-鿿][A-Za-z]|[A-Za-z][一-鿿]", source)


if __name__ == "__main__":
    unittest.main()
