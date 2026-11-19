"""Model/auth reuse checks without accessing the operator's real credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/"monitoring"))
sys.path.insert(0,str(ROOT))
from codex_api_launcher import load_settings, environment


class CodexAPIConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/"config.toml").write_text('model_provider="custom"\nmodel="gpt-6.1-sol"\n[model_providers.custom]\nbase_url="https://provider.invalid/v1"\nwire_api="responses"\n')
        (self.root/"auth.json").write_text(json.dumps({"OPENAI_API_KEY":"fixture-secret-value"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_selected_model_and_auth_override_stale_environment(self):
        settings=load_settings(self.root)
        env=environment(settings,{"SYSINSIGHT_GPT_MODEL":"old-model","SYSINSIGHT_API_KEY":"old-key","OPENAI_BASE_URL":"http://old.invalid"})
        self.assertEqual(env["SYSINSIGHT_GPT_MODEL"],"gpt-6.1-sol")
        self.assertEqual(env["SYSINSIGHT_API_KEY"],"fixture-secret-value")
        self.assertEqual(env["OPENAI_BASE_URL"],"https://provider.invalid/v1")

    def test_check_does_not_print_auth(self):
        r=subprocess.run([sys.executable,str(ROOT.parent/"monitoring/codex_api_launcher.py"),"--config-dir",str(self.root),"--check"],capture_output=True,text=True,check=True)
        self.assertNotIn("fixture-secret-value",r.stdout+r.stderr)
        self.assertNotIn("api_key",json.loads(r.stdout))

    def test_oauth_tokens_are_not_reused_as_api_keys(self):
        (self.root/"auth.json").write_text(json.dumps({"tokens":{"access_token":"oauth-fixture"}}))
        with self.assertRaises(ValueError):load_settings(self.root)

if __name__=="__main__":unittest.main()
