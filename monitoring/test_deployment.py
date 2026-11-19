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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment import unit_text, checks
from process_control import identity, matches


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
