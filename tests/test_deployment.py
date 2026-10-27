import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitoring"))
from deployment import unit_text, checks
from prepare_runtime import copy_tree, relocate_config, prepare, make_public_directory
from process_control import identity, matches


class RuntimeTests(unittest.TestCase):
    def test_new_runtime_parents_traversable_under_private_operator_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = os.umask(0o077)
            try:
                make_public_directory(root / "persistent" / ".venv" / "lib")
            finally:
                os.umask(previous)
            for path in (root / "persistent", root / "persistent/.venv", root / "persistent/.venv/lib"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700, "Do not expose an existing private ancestor")

    def test_relocates_old_checkout_and_strips_api_keys(self):
        original = {"PLANNER_CONFIG": {"model_path": "/old/checkout/dream/dream/agent/plan/model.pt"},
                    "API_SETTINGS": {"openai": {"api_key": "secret", "model": "operator-model"}},
                    "MEMORY_MANAGER_CONFIG": {"db_path": "/tmp/old"}}
        with patch.dict(os.environ, {"SYSINSIGHT_DB": "newdb", "SYSINSIGHT_DB_SCHEMA": "ap"}):
            result = relocate_config(original, Path("/var/lib/example"), Path("/opt/new/dream"))
        self.assertEqual(result["PLANNER_CONFIG"]["model_path"], "/var/lib/example/dream/agent/plan/model.pt")
        self.assertNotIn("api_key", result["API_SETTINGS"]["openai"])
        self.assertEqual(original["API_SETTINGS"]["openai"]["api_key"], "secret")
        self.assertEqual(result["DATABASE_CONFIG"]["dbname"], "newdb")
        self.assertEqual(result["DATABASE_CONFIG"]["search_path"], ["ap", "public"])
        self.assertEqual(result["MEMORY_MANAGER_CONFIG"]["db_path"], "/var/lib/example/state/memory.json")

    def test_copy_is_incremental_and_not_a_hardlink_or_secret_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "source", Path(tmp) / "runtime"
            source.mkdir()
            (source / "model.pt").write_bytes(b"genuine local test asset")
            (source / ".env.local").write_text("private")
            (source / "auth.json").write_text("private")
            copy_tree(source, target)
            self.assertEqual((source / "model.pt").read_bytes(), (target / "model.pt").read_bytes())
            self.assertNotEqual((source / "model.pt").stat().st_ino, (target / "model.pt").stat().st_ino)
            self.assertFalse((target / ".env.local").exists())
            self.assertFalse((target / "auth.json").exists())
            before = (target / "model.pt").stat().st_mtime_ns
            copy_tree(source, target)
            self.assertEqual(before, (target / "model.pt").stat().st_mtime_ns)

    def test_refuses_unmarked_nonempty_runtime_without_touching_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "unrelated"; target.mkdir()
            item = target / "valuable"; item.write_text("preserve")
            with self.assertRaisesRegex(ValueError, "unmarked"):
                prepare(target, "/unused/source", "/unused/venv", "/unused/config", "postgres")
            self.assertEqual(item.read_text(), "preserve")

    def test_refuses_runtime_source_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"; source.mkdir()
            with self.assertRaisesRegex(ValueError, "disjoint"):
                prepare(source / "runtime", source, "/unused", "/unused", "postgres")


class ProcessTests(unittest.TestCase):
    def test_pid_alone_is_not_enough_and_birth_token_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "component.pid"; path.write_text(str(os.getpid()))
            self.assertIsNone(matches(path))
            real = identity(os.getpid())
            path.with_suffix(".identity.json").write_text(json.dumps(real))
            self.assertEqual(matches(path), real)
            real["birth"] = "reused"
            path.with_suffix(".identity.json").write_text(json.dumps(real))
            self.assertIsNone(matches(path))

    def test_changed_command_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "component.pid"; path.write_text(str(os.getpid()))
            real = identity(os.getpid()); real["commandSha256"] = hashlib.sha256(b"other").hexdigest()
            path.with_suffix(".identity.json").write_text(json.dumps(real))
            self.assertIsNone(matches(path))


class UnitTests(unittest.TestCase):
    def test_distinct_unit_no_secrets_and_paths_with_spaces(self):
        with patch.dict(os.environ, {}, clear=True):
            text = unit_text("/opt/demo with spaces", "/usr/bin/python3")
        self.assertIn('ExecStart=/usr/bin/bash "/opt/demo with spaces/scripts/deploy.sh" supervise', text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("Requires=postgresql@12-main.service", text)
        self.assertNotIn("WorkingDirectory=", text)
        self.assertNotIn("api_key", text)
        self.assertIn('Environment="KEENINSIGHT_DEPLOY_PYTHON=/usr/bin/python3"', text)

    def test_rejects_unit_injection(self):
        with self.assertRaises(ValueError):
            unit_text("/opt/demo\nExecStart=/bad", "/usr/bin/python3")
        with patch.dict(os.environ, {"SYSINSIGHT_PG_SERVICE": "bad\n.service"}):
            with self.assertRaises(ValueError):
                unit_text("/opt/demo", "/usr/bin/python3")

    def test_missing_process_cannot_report_healthy_even_if_port_is_open(self):
        with patch("process_control.matches", return_value=None), patch("deployment.urlopen") as request:
            result = checks()
        self.assertTrue(all(not r["healthy"] for r in result.values()))
        request.assert_not_called()

    def test_exporter_pg_up_after_two_megabytes_does_not_restart_ui(self):
        metrics=(b"sample_metric 1\n"*150000)+b'pg_up{server="local"} 1\n'
        with patch("deployment.endpoints",return_value={"postgres_exporter":"http://test/metrics"}), \
             patch("process_control.matches",return_value={"pid":123}), \
             patch("deployment.urlopen",side_effect=lambda *a,**kw:io.BytesIO(metrics)):
            self.assertTrue(checks()["postgres_exporter"]["healthy"])

    def test_exporter_actual_down_is_not_hidden(self):
        with patch("deployment.endpoints",return_value={"postgres_exporter":"http://test/metrics"}), \
             patch("process_control.matches",return_value={"pid":123}), \
             patch("deployment.urlopen",side_effect=lambda *a,**kw:io.BytesIO(b"pg_up 0\n")):
            result=checks()["postgres_exporter"]
        self.assertFalse(result["healthy"])
        self.assertIn("pg_up != 1",result["error"])


if __name__ == "__main__":
    unittest.main()
