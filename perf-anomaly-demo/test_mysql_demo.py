"""Regression tests for the MySQL demo stack and its shared evidence contracts."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from function_locations import describe_function, load_index, source_root
from keeninsight_portal import KeenInsightPortal
from mysql_lab_controller import MySQLLabController
from sysinsight_shap import MYSQL_MODEL, explain, read_rates
from sysinsight_prometheus import default_queries, mysql_queries


ROOT = Path(__file__).resolve().parent


class MySQLLabConfigTests(unittest.TestCase):
    def setUp(self):
        self.controller = object.__new__(MySQLLabController)
        self.case = {"scenario_id": "tp_payment_hot", "title": "Payment hot", "clients": 3,
                     "pressure_target_tps": 900}

    def test_default_windows_match_the_pg_demo_shape(self):
        config = self.controller._config(self.case, {})
        self.assertEqual(config["baseline_seconds"], 60)
        self.assertEqual(config["pressure_seconds"], 180)
        self.assertEqual(config["pressure_before_seconds"], 90)
        self.assertEqual(config["pressure_after_seconds"], 90)
        self.assertEqual(config["dbms"], "mysql")

    def test_pressure_continuous_mode_skips_baseline_and_auto_tunes(self):
        config = self.controller._config(
            self.case, {"continuous_seconds": 120, "auto_tune_seconds": 60},
            continuous_mode="pressure",
        )
        self.assertTrue(config["continuous"])
        self.assertTrue(config["skip_baseline"])
        self.assertEqual(config["pressure_before_seconds"], 60)
        self.assertEqual(config["pressure_after_seconds"], 120)


@unittest.skipUnless(source_root(dbms="mysql"), "MySQL source tree is not installed")
class MySQLFunctionLocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = source_root(dbms="mysql")
        cls.index = load_index(cls.root, dbms="mysql") if cls.root else {}

    @unittest.skipUnless(source_root(dbms="mysql"), "MySQL source tree is not installed")
    def test_innodb_symbol_resolves_to_a_real_definition(self):
        details = describe_function("btr_cur_search_to_nth_level", self.index, root=self.root)
        self.assertIsNotNone(details)
        self.assertEqual(details["file"], "storage/innobase/btr/btr0cur.cc")
        self.assertEqual(details["line"], 619)
        self.assertIn("Searches an index tree", details["description"] or "")

    def test_mysql_index_is_available_for_portal_enrichment(self):
        self.assertGreater(len(self.index), 1000)


class MySQLSourceEvidenceTests(unittest.TestCase):
    def test_manual_and_code_pages_use_mysql_assets(self):
        portal = object.__new__(KeenInsightPortal)
        portal.bridge = SimpleNamespace(args=SimpleNamespace(dbms="mysql", pg_version="8.0.36"))
        portal._cache = {}
        portal._cache_lock = __import__("threading").RLock()
        manual = portal.references("manual")
        code = portal.references("code")
        self.assertGreater(len(manual), 10)
        self.assertTrue(any(row["title"] == "innodb_buffer_pool_size" for row in manual))
        self.assertGreater(len(code), 10)
        self.assertTrue(all(row["db"] == "mysql" for row in code))
        if source_root(dbms="mysql"):
            self.assertTrue(any(row.get("file") and row.get("line") for row in code))


class MySQLPrometheusQueryTests(unittest.TestCase):
    def test_mysql_queries_use_the_mysql_job_and_schema(self):
        queries = mysql_queries("keeninsight_tpcc")
        self.assertEqual(queries["active_sessions"],
                         'mysql_global_status_threads_running{job="mysql"}')
        self.assertIn('job="mysql"', queries["transactions_rolled_back"])
        self.assertIn('schema="keeninsight_tpcc"', queries["database_size_bytes"])
        self.assertIn('operation="read"', queries["tuples_returned"])
        self.assertEqual(default_queries("keeninsight_tpcc", "mysql"), queries)
        self.assertIn('job="postgres"', default_queries("keeninsight", "postgresql")["transactions_committed"])


class MySQLShapTests(unittest.TestCase):
    def test_mysql_counts_profile_is_accepted(self):
        profile = ROOT / "results" / "mysql_normal_profile.csv"
        if not profile.is_file():
            self.skipTest("MySQL normal profile is not available")
        rates = read_rates(profile)
        self.assertGreater(len(rates), 100)
        self.assertIn("do_command", rates)

    @unittest.skipUnless(
        (ROOT / "models" / "sysinsight_mysql_tps" / "manifest.json").is_file(), "MySQL SHAP model is not trained")
    def test_mysql_model_explains_a_measured_pressure_profile(self):
        profiles = sorted((ROOT / "results" / "mysql_shap_windows").glob("**/anomaly_counts_tpcc.txt"))
        if not profiles:
            self.skipTest("no collected MySQL pressure profile is available")
        result = explain(profiles[0], MYSQL_MODEL, {"workload": "tpcc", "phase": "pressure_before"})
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["featureCount"], 10)
        self.assertGreater(len(result["features"]), 0)
        self.assertIn("reliability", result)


if __name__ == "__main__":
    unittest.main()
