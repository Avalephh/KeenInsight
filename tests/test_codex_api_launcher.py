"""Model/auth reuse checks without accessing the operator's real credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/"monitoring"))
sys.path.insert(0,str(ROOT.parent/"app"))
from codex_api_launcher import load_settings, environment
from sysinsight_dream_bridge import Bridge


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

    def test_all_dream_agents_follow_shared_selection_without_saving_key(self):
        config=self.root/"dream.json"
        config.write_text(json.dumps({"AGENT_CONFIG":{"root_cause_llm_model":"old","fix_agent_model":"old","diagnostic_agent_model":"old"},"API_SETTINGS":{"openai":{"api_key":"fixture-secret-value","model":"old"}}}))
        bridge=Bridge.__new__(Bridge)
        bridge.args=SimpleNamespace(dream_config=str(config),dream_runtime_root=str(ROOT.parent/"dream"),
            api_base="https://provider.invalid/v1",model="gpt-6.1-sol",db="demo",db_user="demo",
            host="/tmp",port=5432,db_schema="public")
        output=bridge._job_config(self.root)
        data=json.loads(output.read_text())
        for value in data["AGENT_CONFIG"].values():self.assertEqual(value,"gpt-6.1-sol")
        self.assertNotIn("fixture-secret-value",output.read_text())
        self.assertEqual(data["API_SETTINGS"]["openai"]["model"],"gpt-6.1-sol")


if __name__=="__main__":unittest.main()
