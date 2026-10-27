"""Release-specific contracts; no real DB, model, API call or source download."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"app"))
sys.path.insert(0,str(ROOT/"monitoring"))
from lab_controller import LabController
from prepare_runtime import relocate_config


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    loaded=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class ReleaseContracts(unittest.TestCase):
    def test_release_guard_audits_integration_patches_not_only_application(self):
        checker=module("release_guard",ROOT/"scripts/release_check.py")
        audited={p.relative_to(ROOT).as_posix() for p in checker.files()}
        for name in ("integrations/dream/assets.json","integrations/dream/local-integration.patch",
                     "integrations/dream/rcrank-compatibility.patch"):
            self.assertIn(name,audited)

    def test_missing_tp_candidate_is_unavailable_not_a_fabricated_tuning(self):
        with tempfile.TemporaryDirectory() as tmp,patch("lab_controller.LAB_CANDIDATE_DIRECTORY",Path(tmp)):
            result=LabController.__new__(LabController)._load_lab_tuning("tp_stock_level_hot")
        self.assertEqual(result["status"],"unavailable")
        self.assertEqual(result["requested_configuration"],{})
        self.assertEqual(result["source_type"],"operator_supplied_candidate_replay")

    def test_tp_candidate_is_explicitly_labelled_replay_and_ids_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp,patch("lab_controller.LAB_CANDIDATE_DIRECTORY",Path(tmp)):
            (Path(tmp)/"tp_stock_level_hot.json").write_text(json.dumps({"jit":"off"}))
            lab=LabController.__new__(LabController)
            result=lab._load_lab_tuning("tp_stock_level_hot")
            self.assertEqual(result["requested_configuration"],{"jit":"off"})
            self.assertEqual(result["source_type"],"operator_supplied_candidate_replay")
            with self.assertRaises(ValueError):lab._load_lab_tuning("../../other")

    def test_portable_template_resolves_inside_runtime_without_old_workstation(self):
        config=json.loads((ROOT/"config/dream.example.json").read_text())
        result=relocate_config(config,Path("/var/lib/example"),Path("/opt/source/dream"))
        self.assertTrue(result["PLANNER_CONFIG"]["model_path"].startswith("/var/lib/example/dream/"))
        self.assertEqual(result["AGENT_CONFIG"]["action_server_config"],
                         "/var/lib/example/dream/agent/action/mcp_tools/action_server_config.yaml")
        self.assertNotIn("${DREAM_ROOT}",json.dumps(result))
        self.assertNotIn("api_key",json.dumps(result))

    def test_asset_verifier_rejects_missing_and_tampered_data(self):
        assets=module("release_assets",ROOT/"scripts/check_dream_assets.py")
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"model"
            fixture=b"unit-test-only, not a model"
            manifest={"files":[{"path":"model","bytes":len(fixture),"sha256":hashlib.sha256(fixture).hexdigest()}]}
            self.assertTrue(assets.verify(Path(tmp),manifest))
            path.write_bytes(fixture)
            self.assertEqual(assets.verify(Path(tmp),manifest),[])
            path.write_bytes(b"changed")
            self.assertTrue(assets.verify(Path(tmp),manifest))

    def test_worker_portability_has_no_private_defaults_and_only_local_tool(self):
        fetch=module("release_fetch",ROOT/"scripts/fetch_dream_source.py")
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)
            (target/"dream/agent/action/mcp_tools").mkdir(parents=True)
            settings=target/"dream/runtime_config.py"
            settings.write_text('DEFAULT_API_BASE = "https://old.example.invalid"\nDEFAULT_MODEL = "legacy"\n')
            fetch.portable_worker_defaults(target)
            self.assertIn("https://api.example.com/v1",settings.read_text())
            services=json.loads((target/"dream/agent/action/mcp_tools/action_server_config.yaml").read_text())
            self.assertEqual(len(services["services"]),1)
            self.assertEqual(services["services"][0]["params"]["command"],str(target/".venv/bin/python"))


if __name__=="__main__":unittest.main()
