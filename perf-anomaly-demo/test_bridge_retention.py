#!/usr/bin/env python3
"""Focused regression tests for the bridge's bounded 24-hour storage."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sysinsight_dream_bridge import StateStore, maintain_monitoring_logs, prune_output_artifacts


class BridgeRetentionTest(unittest.TestCase):
    def test_prune_preserves_active_state_and_removes_terminal_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "output"
            output.mkdir()
            store = StateStore(root / "state.sqlite3")
            old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
            recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()

            with sqlite3.connect(str(store.path)) as conn:
                for incident_id, status, timestamp in (
                    ("old-incident", "completed", old),
                    ("new-incident", "completed", recent),
                    ("active-incident", "running", old),
                ):
                    directory = output / "incidents" / incident_id
                    directory.mkdir(parents=True)
                    (directory / "evidence.json").write_text("{}", encoding="utf-8")
                    conn.execute(
                        "INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)",
                        (
                            incident_id,
                            "TestAlert",
                            "{}",
                            timestamp,
                            timestamp if status != "running" else None,
                            status,
                            str(directory),
                            "{}" if status != "running" else None,
                        ),
                    )
                for run_id, status, timestamp in (
                    ("old-run", "completed", old),
                    ("new-run", "completed", recent),
                    ("active-run", "running", old),
                ):
                    directory = output / "lab" / run_id
                    directory.mkdir(parents=True)
                    (directory / "result.json").write_text("{}", encoding="utf-8")
                    conn.execute(
                        """
                        INSERT INTO lab_runs(
                            run_id,kind,status,phase,requested_at,started_at,finished_at,
                            config_json,result_json
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run_id,
                            "sysinsight",
                            status,
                            status,
                            timestamp,
                            timestamp,
                            timestamp if status != "running" else None,
                            "{}",
                            "{}",
                        ),
                    )

            result = store.prune_history(1.0, 1000, 1000)
            expired = result.pop("_artifact_paths")
            prune_output_artifacts(output, result["cutoff"], expired, store.artifact_references())

            with sqlite3.connect(str(store.path)) as conn:
                incidents = {row[0] for row in conn.execute("SELECT incident_id FROM incidents")}
                runs = {row[0] for row in conn.execute("SELECT run_id FROM lab_runs")}
            self.assertEqual(incidents, {"new-incident", "active-incident"})
            self.assertEqual(runs, {"new-run", "active-run"})
            self.assertFalse((output / "incidents" / "old-incident").exists())
            self.assertFalse((output / "lab" / "old-run").exists())
            self.assertTrue((output / "incidents" / "active-incident").exists())

    def test_unchanged_statement_does_not_create_duplicate_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            statement = {
                "database": "demo",
                "username": "demo",
                "queryid": "1",
                "query": "select 1",
                "calls": 1,
                "total_time_ms": 10,
                "min_time_ms": 10,
                "max_time_ms": 10,
                "mean_time_ms": 10,
            }
            store.upsert_observations([statement], [])
            store.upsert_observations([statement], [])
            with sqlite3.connect(str(store.path)) as conn:
                count = int(conn.execute("SELECT COUNT(*) FROM sql_observation_samples").fetchone()[0])
            self.assertEqual(count, 1)

    def test_incremental_vacuum_reclaims_deleted_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            with sqlite3.connect(str(store.path)) as conn:
                conn.execute("CREATE TABLE extra(value BLOB)")
                conn.executemany("INSERT INTO extra VALUES(?)", [(b"x" * 4096,) for _ in range(200)])
                conn.execute("DELETE FROM extra")
            result = store.reclaim_space(8192)
            self.assertEqual(result["auto_vacuum"], 2)
            self.assertEqual(result["free_pages_after"], 0)
            self.assertGreater(result["reclaimed_bytes"], 0)

    def test_monitoring_logs_rotate_and_expire(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            active = root / "sysinsight_dream_bridge.log"
            active.write_bytes(b"x" * 10000)
            stale = root / "grafana.log.20200101"
            stale.write_bytes(b"old")
            old_epoch = time.time() - 30 * 3600
            os.utime(str(stale), (old_epoch, old_epoch))
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()

            result = maintain_monitoring_logs(root, cutoff, rotation_seconds=900)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(active.stat().st_size, 0)
            self.assertFalse(stale.exists())
            self.assertEqual(len(list(root.glob("*.retained-*.gz"))), 1)


if __name__ == "__main__":
    unittest.main()
