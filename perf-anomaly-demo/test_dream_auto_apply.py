#!/usr/bin/env python3
"""Regression tests for measured DREAM automatic application routing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dream_auto_apply import is_read_only_statement, parse_dream_candidate
from sysinsight_dream_bridge import Bridge, StateStore, _hint_inner


class FakeDatabase:
    def __init__(self) -> None:
        self.published = []

    @staticmethod
    def normalize_session_settings(settings):
        return {str(name): str(value) for name, value in settings.items()}

    @staticmethod
    def extension_status():
        return {"hint_table_exists": True, "hint_table_enabled": "on"}

    def upsert_hint(self, norm_query, hints, application_name=""):
        row = {
            "norm_query_string": norm_query,
            "hints": hints,
            "application_name": application_name,
        }
        self.published.append(row)
        return row

    @staticmethod
    def explain(sql):
        return {"statement": sql}


class DreamAutoApplyTest(unittest.TestCase):
    def _bridge(self, root: Path):
        bridge = Bridge.__new__(Bridge)
        bridge.store = StateStore(root / "state.sqlite3")
        bridge.db = FakeDatabase()
        bridge.args = SimpleNamespace(
            min_improvement=0.10,
            auto_apply=True,
            hint_application_name="",
        )
        return bridge

    @staticmethod
    def _observation(store: StateStore, query: str = "select * from demo"):
        key = store.upsert_observations(
            [{
                "database": "demo",
                "username": "demo",
                "queryid": "101",
                "query": query,
                "calls": 1,
                "total_time_ms": 10000,
                "min_time_ms": 10000,
                "max_time_ms": 10000,
                "mean_time_ms": 10000,
            }],
            [],
        )[0]
        return store.get_observation(key)

    def test_parser_routes_executor_setting_through_managed_path(self):
        work_mem = parse_dream_candidate(
            "SET work_mem = '64MB';", "", "select * from demo order by value"
        )
        planner_cost = parse_dream_candidate(
            "SET random_page_cost = 10;", "", "select * from demo"
        )
        self.assertEqual(work_mem["apply_scope"], "bridge_managed")
        self.assertEqual(work_mem["execution_sql"], "select * from demo order by value")
        self.assertEqual(planner_cost["apply_scope"], "postgresql_direct")

    def test_read_only_and_nested_hint_validation(self):
        self.assertTrue(is_read_only_statement("WITH x AS (SELECT 1) SELECT * FROM x;"))
        self.assertFalse(is_read_only_statement("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x"))
        self.assertFalse(is_read_only_statement("SELECT nextval('s')"))
        self.assertEqual(
            _hint_inner("Leading((a b)) HashJoin(a b) Set(random_page_cost 10)"),
            "Leading((a b)) HashJoin(a b) Set(random_page_cost 10)",
        )
        with self.assertRaises(ValueError):
            _hint_inner("SeqScan(a); DROP TABLE a")

    def test_validated_rewrite_becomes_active_and_is_resolved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bridge = self._bridge(Path(temporary_directory))
            observation = self._observation(bridge.store)
            publication = bridge._validate_and_publish(
                "job-rewrite",
                observation,
                {
                    "evaluation_status": 1,
                    "old_time": 10.0,
                    "new_time": 2.0,
                    "read_only": True,
                    "rewrite_sql": "select count(*) from demo",
                    "fix_action": "",
                    "root_causes": ["poorly written queries"],
                },
            )
            self.assertEqual(publication["status"], "active")
            self.assertEqual(publication["application_scope"], "bridge_managed")
            resolved = bridge.resolve_dream_application(
                str(observation["sql_key"]), "select * from demo"
            )
            self.assertTrue(resolved["applied"])
            self.assertEqual(resolved["query"], "select count(*) from demo")
            self.assertEqual(resolved["apply_kind"], "rewrite_sql")

    def test_same_canonical_sql_with_new_statistics_key_still_resolves(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bridge = self._bridge(Path(temporary_directory))
            observation = self._observation(bridge.store)
            bridge._validate_and_publish(
                "job-canonical-fallback",
                observation,
                {
                    "evaluation_status": 1,
                    "old_time": 10.0,
                    "new_time": 2.0,
                    "read_only": True,
                    "rewrite_sql": "select count(*) from demo",
                },
            )
            resolved = bridge.resolve_dream_application(
                "different-client-query-key", "select * from demo"
            )
            self.assertTrue(resolved["applied"])
            self.assertEqual(resolved["query"], "select count(*) from demo")

    def test_rewrite_does_not_cross_apply_to_different_literal_values(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bridge = self._bridge(Path(temporary_directory))
            observation = self._observation(bridge.store, "select * from demo where id = 1")
            bridge._validate_and_publish(
                "job-literal-guard",
                observation,
                {
                    "evaluation_status": 1,
                    "old_time": 10.0,
                    "new_time": 2.0,
                    "read_only": True,
                    "rewrite_sql": "select count(*) from demo where id = 1",
                },
            )
            resolved = bridge.resolve_dream_application(
                "different-client-query-key", "select * from demo where id = 2"
            )
            self.assertFalse(resolved["applied"])
            self.assertIn("literal", resolved["method"])

    def test_executor_setting_is_applied_to_original_managed_query(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bridge = self._bridge(Path(temporary_directory))
            observation = self._observation(bridge.store, "select * from demo order by value")
            publication = bridge._validate_and_publish(
                "job-work-mem",
                observation,
                {
                    "evaluation_status": 1,
                    "old_time": 10.0,
                    "new_time": 5.0,
                    "read_only": True,
                    "rewrite_sql": "",
                    "fix_action": "SET work_mem = '64MB';",
                },
            )
            self.assertEqual(publication["status"], "active")
            self.assertEqual(publication["application_scope"], "bridge_managed")
            resolved = bridge.resolve_dream_application(
                str(observation["sql_key"]), "select * from demo order by value"
            )
            self.assertEqual(resolved["query"], "select * from demo order by value")
            self.assertEqual(resolved["session_settings"], {"work_mem": "64MB"})

    def test_planner_setting_is_published_for_direct_postgresql_use(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bridge = self._bridge(Path(temporary_directory))
            observation = self._observation(bridge.store)
            publication = bridge._validate_and_publish(
                "job-planner-setting",
                observation,
                {
                    "evaluation_status": 1,
                    "old_time": 10.0,
                    "new_time": 5.0,
                    "read_only": True,
                    "rewrite_sql": "",
                    "fix_action": "SET random_page_cost = 10;",
                },
            )
            self.assertEqual(publication["status"], "active")
            self.assertEqual(publication["application_scope"], "postgresql_direct")
            self.assertEqual(len(bridge.db.published), 1)
            self.assertEqual(bridge.db.published[0]["hints"], "Set(random_page_cost 10)")

    def test_missing_optimized_runtime_can_never_become_active(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bridge = self._bridge(Path(temporary_directory))
            observation = self._observation(bridge.store)
            publication = bridge._validate_and_publish(
                "job-no-measurement",
                observation,
                {
                    "evaluation_status": 1,
                    "old_time": 10.0,
                    "new_time": None,
                    "read_only": True,
                    "rewrite_sql": "select count(*) from demo",
                    "fix_action": "",
                },
            )
            self.assertEqual(publication["status"], "candidate")
            self.assertIn("missing", publication["reason"])
            improvement = bridge.store.latest_improvement(str(observation["sql_key"]))
            self.assertIsNone(improvement["improvement_ratio"])


if __name__ == "__main__":
    unittest.main()
