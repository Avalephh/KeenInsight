"""Contracts and persistence regression tests for the live system-tuning portal."""
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen
import json
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from function_locations import build_index, describe_function
from keeninsight_portal import KeenInsightPortal, hot_functions_from_source
from sysinsight_dream_bridge import StateStore, BridgeHTTPServer, BridgeHTTPHandler


class PrometheusFixture:
    def query(self, expression):
        return {"data": {"result": [{"metric": {"ki_metric": name}, "value": [1, value]}
                                     for name, value in [("cpuPercent", "12.5"), ("memoryPercent", "30"), ("diskPercent", "40")]]}}

    def query_range(self, expression, start, end, step):
        return {"data": {"result": [{"metric": {}, "values": [[end-15, "3"], [end, "4"]]}]}}

    def _get(self, path, params):
        return {"data": {"groups": [{"name": "fixture", "file": "test.yml", "rules": [{
            "name": "TestAlert", "query": "fixture > 3", "duration": 10, "state": "inactive", "health": "ok"}]}]}}


class PortalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.store.set_meta("latest_collection", {"collected_at": "2020-01-01T00:00:00+00:00"})
        self.bridge = SimpleNamespace(store=self.store, args=SimpleNamespace(db="demo",port=5432),
                                      last_collection_error="", prometheus=PrometheusFixture())
        self.portal = KeenInsightPortal(self.bridge)
        self.bridge.portal = self.portal

    def tearDown(self):
        self.tmp.cleanup()

    def test_host_usage_uses_real_snapshot_and_unknown_is_not_zero(self):
        record = self.portal.hosts()[0]["utilization"]
        self.assertEqual(record["cpuPercent"], 12.5)
        self.assertEqual(record["status"], "recorded")
        self.bridge.prometheus = SimpleNamespace(query=lambda expr: {"data": {"result": []}})
        self.portal._host_metrics_cache = None
        record = self.portal.hosts()[0]["utilization"]
        self.assertIsNone(record["cpuPercent"])
        self.assertIsNone(record["diskPercent"])
        self.assertEqual(record["status"], "partial")

    def test_function_analysis_falls_back_to_retained_evidence(self):
        source = {
            "runtime_call_chains": [
                {"samples": 10, "matched_key_functions": ["alpha", "beta"]},
                {"samples": 5, "matched_key_functions": ["beta"]},
            ],
            "static_parameter_evidence": [{"function": "gamma"}, {"function": "alpha"}],
        }
        values = hot_functions_from_source(source)
        self.assertEqual([item["Function"] for item in values], ["alpha", "beta", "gamma"])
        self.assertEqual(values[0]["Samples"], 10)
        self.assertEqual(values[1]["Samples"], 15)
        self.assertEqual(values[0]["Evidence"], "runtime_call_chain")
        self.assertEqual(values[2]["Evidence"], "source_association")
        self.assertEqual(hot_functions_from_source(None), [])

    def test_function_location_index_finds_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "backend"
            source.mkdir(parents=True)
            (source / "x.c").write_text(
                "/* comment */\nvoid\nfixture_function(int value)\n{\n}\n", encoding="utf-8")
            (source / "x.h").write_text(
                "extern void fixture_function(int value);\n", encoding="utf-8")
            index = build_index(root)
            self.assertEqual(index["fixture_function"],
                             {"file": "src/backend/x.c", "line": 3})

    def test_function_description_extracts_comment_and_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "backend"
            source.mkdir(parents=True)
            (source / "x.c").write_text(
                "/*\n * Compute fixture values for a tuple.\n */\n"
                "void\nfixture_function(int value)\n{\n}\n", encoding="utf-8")
            index = build_index(root)
            detail = describe_function("fixture_function", index=index, root=root)
            self.assertEqual(detail["description"], "Compute fixture values for a tuple.")
            self.assertEqual(detail["signature"], "void fixture_function(int value)")
            self.assertEqual(detail["excerptStartLine"], 1)
            self.assertIn("fixture_function", detail["excerpt"])
            self.assertEqual(describe_function("missing_function", index=index, root=root), None)

    def test_hot_functions_get_source_locations(self):
        self.portal._function_locations = {
            "fixture_function": {"file": "src/backend/x.c", "line": 3},
        }
        values = self.portal._enrich_hot_functions([
            {"Function": "fixture_function"},
            {"Function": "missing_function"},
        ])
        self.assertEqual(values[0]["SourceFile"], "src/backend/x.c")
        self.assertEqual(values[0]["SourceLine"], 3)
        self.assertNotIn("SourceFile", values[1])

    def test_interrupted_alert_uses_backfilled_function_analysis(self):
        incident_dir = Path(self.tmp.name) / "backfill-incident"
        incident_dir.mkdir()
        (incident_dir / "backfill_function_analysis.json").write_text(json.dumps({
            "schema_version": "keeninsight.backfill.v1",
            "generated_at": "2026-11-20T00:00:00+00:00",
            "provenance": {"source": "retained perf counts"},
            "source_detection": {"source_compare": {
                "key_function_count": 1,
                "key_functions": [{"Function": "fixture_function", "Sample Rate": 1,
                                   "Diff From Mean": 2, "Change": 1}],
            }},
            "source_evidence": {"runtime_call_chains": [], "static_parameter_evidence": []},
            "shap_analysis": {"status": "completed"},
        }), encoding="utf-8")
        incident = self.store.start_incident({"labels": {"alertname": "Fixture"}}, incident_dir)
        self.store.finish_incident(incident, "failed", {"error": "interrupted"})
        detail = self.portal.get("alerts/" + incident, {})
        analysis = detail["detailAnalysis"]
        self.assertTrue(analysis["backfillGenerated"])
        self.assertEqual(analysis["hotFunctions"][0]["Function"], "fixture_function")
        self.assertEqual(analysis["differentialProfiling"]["key_function_count"], 1)
        self.assertEqual(analysis["stages"][-1]["name"], "post_hoc_function_analysis")

    def test_alert_filters_run_before_pagination(self):
        for level in ["info", "critical", "critical"]:
            incident = self.store.start_incident({"labels": {"alertname": "Fixture", "instance": "test-host", "severity": level}}, Path(self.tmp.name))
            self.store.finish_incident(incident, "failed", {"error": "fixture"})
        result = self.portal.get("alerts", {"q": ["test-host"], "severity": ["critical"], "status": ["failed"], "limit": ["1"]})
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["severity"], "critical")
        self.assertEqual(self.portal.get("hosts", {"dbType": ["mysql"]})["total"], 0)
        self.assertEqual(self.portal.get("hosts", {"dbType": ["postgresql"]})["total"], 1)
        self.assertEqual(self.portal.get("code", {"db": ["mysql"]})["total"], 0)

    def test_restart_marks_only_unfinished_alerts_interrupted_and_retains_evidence(self):
        unfinished=self.store.start_incident({"labels":{"alertname":"Interrupted"}},Path(self.tmp.name))
        finished=self.store.start_incident({"labels":{"alertname":"Completed"}},Path(self.tmp.name))
        self.store.finish_incident(finished,"completed",{"originalEvidence":"preserved"})
        recovered=self.store.recover_incidents()
        self.assertEqual(recovered,[unfinished])
        self.assertEqual(self.portal.get("alerts/"+finished,{})["rawStatus"],"completed")
        detail=self.portal.get("alerts/"+unfinished,{})
        self.assertEqual(detail["rawStatus"],"failed")
        self.assertIn("restarted",detail["message"])
        with self.store._connection() as conn:
            row=conn.execute("SELECT sysinsight_json,finished_at FROM incidents WHERE incident_id=?",(unfinished,)).fetchone()
            payload=json.loads(row[0])
            self.assertFalse(payload["recovery"]["automatically_replayed"])
            self.assertTrue(row[1])
            self.assertTrue(conn.execute("SELECT 1 FROM keeninsight_evidence WHERE entity_id=?",(unfinished,)).fetchone())
        self.assertEqual(self.store.recover_incidents(),[])

    def test_alert_wrapper_unwraps_call_chain_and_application(self):
        incident=self.store.start_incident({"labels":{"alertname":"Fixture"}},Path(self.tmp.name))
        self.store.finish_incident(incident,"completed",{"live_chain":{"status":"completed","result":{
            "source_evidence":{"runtime_call_chains":[{"frames":["a","b"],"samples":3}]},
            "comparison":{"baseline_ms":40,"tuned_ms":20}, "temporary_application":{"status":"applied_and_restored"}}}})
        detail=self.portal.get("alerts/"+incident,{})
        self.assertEqual(detail["detailAnalysis"]["comparison"]["tuned_ms"],20)
        self.assertEqual(detail["detailAnalysis"]["runtimeCallChains"][0]["samples"],3)
        self.assertIsNone(detail["detailAnalysis"]["shapAnalysis"])

    def test_alert_summary_and_skip_reason_are_visible_without_claiming_recovery(self):
        incident=self.store.start_incident({'state':'firing','labels':{'alertname':'Fixture'}},Path(self.tmp.name))
        self.store.finish_incident(incident,'skipped',{'reason':'no_workload_backend_at_alert','explanation':'No live workload remained'})
        record=self.portal.get('alerts/'+incident,{})
        self.assertEqual(record['message'],'No live workload remained')
        self.assertEqual(record['triggerState'],'firing')
        self.assertIsNone(record['error'])
        self.assertEqual(record['applicationStatus'],None)

    def test_overview_has_measured_tps_and_truthful_candidate_provenance(self):
        self.store.create_lab_run('tp-run','sysinsight','case','Fixture',{})
        self.store.update_lab_run('tp-run',status='completed',phase='completed',result_patch={
            'comparison':{'baseline_tps':100,'pressure_before_tps':20,'pressure_after_tps':80},
            'tuning':{'source_type':'historical_llm_candidate','status':'applied_and_restored'}})
        data=self.portal.get('overview',{})
        self.assertEqual(data['recentRuns'][0]['comparison']['pressure_after_tps'],80)
        self.assertEqual(data['recentRuns'][0]['candidateSourceType'],'historical_llm_candidate')
        self.assertIn('different measurements',data['recentRuns'][0]['evidenceNote'])
        self.assertTrue(any(item['name']=='SHAP attribution' for item in data['capabilities']))

    def test_running_alert_reads_current_stage_journal(self):
        root=Path(self.tmp.name)
        incident=self.store.start_incident({'labels':{'alertname':'Fixture'}},root)
        (root/'workflow_progress.json').write_text(json.dumps({'stages':[{'name':'perf_capture','status':'completed','recordedAt':'2026-10-22T12:00:00Z'}]}))
        detail=self.portal.get('alerts/'+incident,{})
        self.assertEqual(detail['detailAnalysis']['stages'][0]['name'],'perf_capture')

    def test_references_are_real_assets_and_pagination_is_bounded(self):
        for category in ["code","manual"]:
            data=self.portal.get(category,{"limit":["2"]})
            self.assertGreater(data["total"],2)
            self.assertEqual(len(data["items"]),2)
            self.assertTrue(data["items"][0]["source"])
        self.assertEqual(self.portal.get("hosts",{})["items"][0]["status"],"stale")
        self.assertEqual(len(self.portal.get("schema",{})["categories"]),6)
        with self.assertRaises(KeyError):self.portal.get("sql",{})
        with self.assertRaises(KeyError):self.portal.get("diagnosisRules",{})
        with self.assertRaises(KeyError):self.portal.get("knowledge",{})

    def test_retained_pages_have_no_sql_navigation(self):
        web = Path(__file__).resolve().parent / "keeninsight_web"
        for page in ("template","host-list","alert-list","alert-detail","alert-rules","manual","code-source"):
            source = (web / (page + ".html")).read_text()
            for removed in ("sql-log.html", "sql-detail.html", "dba-rules.html", "tuning-knowledge.html"):
                self.assertNotIn(removed, source, page)

    def test_http_assets_unknown_ids_and_traversal(self):
        server=BridgeHTTPServer(("127.0.0.1",0),BridgeHTTPHandler,self.bridge)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base="http://127.0.0.1:"+str(server.server_port)
        try:
            for page in ["host-list","template","alert-list","alert-detail"]:
                with urlopen(base+"/keeninsight/"+page+".html") as response:
                    body=response.read().decode()
                    self.assertIn('id="ki-root"',body)
                    self.assertNotIn("realtime-monitor.html",body)
            with urlopen(base+"/api/v1/keeninsight/schema") as response:
                self.assertEqual(json.load(response)["schemaVersion"],"keeninsight.ui.v1")
            with urlopen(base+"/keeninsight/original-layouts.js") as response:
                self.assertIn("window.KeenInsightLayouts", response.read().decode())
            for path,code in [("/keeninsight/../sysinsight_dream_bridge.py",404),
                              ("/keeninsight/realtime-monitor.html",404),
                              ("/keeninsight/sql-log.html",404),
                              ("/keeninsight/sql-detail.html",404),
                              ("/api/v1/keeninsight/metrics",404),
                              ("/api/v1/keeninsight/sql/missing",404),
                              ("/api/v1/keeninsight/sql?limit=1",404),
                              ("/api/v1/keeninsight/sql?limit=invalid",400)]:
                with self.assertRaises(HTTPError) as raised:urlopen(base+path)
                self.assertEqual(raised.exception.code,code)
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == "__main__":
    unittest.main()
