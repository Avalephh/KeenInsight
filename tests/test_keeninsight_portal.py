"""Contracts and persistence regression tests for the live English portal."""
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen
import json
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from keeninsight_portal import KeenInsightPortal, prune
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

    def observation(self, query="select 1", queryid="1"):
        return self.store.upsert_observations([{"database": "demo", "username": "tester", "queryid": queryid,
            "query": query, "calls": 2, "total_time_ms": 24000, "min_time_ms": 11000,
            "max_time_ms": 13000, "mean_time_ms": 12000}], [])[0]

    def test_sql_contract_persists_real_evidence_and_keeps_unknowns_null(self):
        key = self.observation()
        job = self.store.enqueue_job(key, None, cooldown=0, force=True)
        self.store.finish_job(job, "completed", {"query": "select 1", "plan_json": {"Plan": {"Node Type": "Result"}},
            "root_causes": ["test cause"], "api_key": "secret-value", "api_model": "fixture-model"})
        self.store.add_improvement({"sql_key": key, "status": "active", "old_time": 12, "new_time": 3,
                                    "improvement_ratio": .75, "rewrite_sql": "select 1"})
        # A fresh portal process reads the captured result without original files.
        record = KeenInsightPortal(self.bridge).get("sql/"+key, {})
        self.assertEqual(record["kind"], "observation")
        self.assertEqual(record["executionTime"],12000)
        self.assertEqual(record["tuning_results"]["new_time"],3)
        self.assertEqual(record["lifecycle"][0]['source'], 'dream_jobs')
        self.assertIsNone(record["lifecycle"][0]['durationMs'])
        self.assertIsNone(record["flameGraph"])
        self.assertEqual(record["plan"]["Plan"]["Node Type"], "Result")
        self.assertNotIn("secret-value",json.dumps(record))

    def test_failed_single_execution_is_separate_from_fingerprint(self):
        self.observation()
        run = "test-run"
        self.store.create_lab_run(run, "dream", "fixture", "Test", {})
        eid=self.store.add_lab_execution({"run_id":run,"sql_id":"fixture","query_text":"select broken",
                                         "status":"failed","duration_ms":15,"error":"db error"})
        record=self.portal.get("sql/"+eid,{})
        self.assertTrue(record["hasError"])
        self.assertEqual(record["executionTime"],15)
        result=self.portal.get("sql",{"q":["broken"]})
        self.assertEqual(result["total"],1)
        self.assertEqual(result["items"][0]["kind"],"execution")
        self.assertEqual(self.portal.get("sql",{})["items"][0]["kind"],"execution")
        self.assertEqual(self.portal.get("sql",{"kind":["observation"]})["total"],1)
        with self.assertRaises(ValueError):self.portal.get("sql",{"kind":["invalid"]})

    def test_original_page_filters_run_before_pagination(self):
        self.observation("select 1", "1")
        self.observation("update fixture set x=1", "2")
        record = self.portal.get("sql", {"type": ["SELECT"], "duration": ["slow"], "limit": ["1"]})
        self.assertEqual(record["total"], 1)
        self.assertEqual(record["items"][0]["type"], "SELECT")
        self.assertEqual(self.portal.get("sql", {"duration": ["fast"]})["total"], 0)
        self.assertEqual(self.portal.get("sql", {"result": ["failed"]})["total"], 0)
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

    def test_original_presentation_containers_without_demo_scripts(self):
        web = Path(__file__).resolve().parents[1] / "app/keeninsight_web"
        expected = {"host-list": ["search-section", "table-toolbar", 'id="hostTableBody"'],
                    "alert-list": ["filter-section", 'id="alertTableBody"'],
                    "sql-log": ["filter-section", 'id="logTableBody"'],
                    "manual": ['id="snippetsContainer"'], "tuning-knowledge": ['id="tuningContainer"'],
                    "alert-detail": ['id="detailContent"'], "sql-detail": ['id="detailContent"']}
        for name, markers in expected.items():
            source = (web / (name + ".html")).read_text()
            for marker in markers:
                self.assertIn(marker, source, name)
            self.assertIn('src="original-layouts.js"', source)
            self.assertNotIn("realtime-monitor", source)
            self.assertNotIn("const alertData", source)
            self.assertNotIn("const sqlLogData", source)
            self.assertNotIn("192.168.10.202", source)
        layout = (web / "original-layouts.js").read_text()
        self.assertIn('class="middle-section"', layout)
        self.assertIn("SHAP Model Analysis Results", layout)
        self.assertIn("SQL Lifecycle", layout)
        self.assertNotIn("Math.random", layout)
        self.assertNotIn("innodb_log_buffer_size", layout)

    def test_clear_archives_sql_evidence_and_preserves_alert_evidence(self):
        key=self.observation()
        job=self.store.enqueue_job(key,None,cooldown=0,force=True)
        self.store.finish_job(job,"failed",{"query":"select 1","plan_json":{"Plan":{}},"error":"fixture"})
        incident=self.store.start_incident({"labels":{"alertname":"Fixture"}},Path(self.tmp.name))
        self.store.finish_incident(incident,"failed",{"error":"fixture"})
        archive=Path(self.tmp.name)/"archive.sqlite3"
        result=self.store.clear_dream_records(archive)
        self.assertEqual(result["deleted"]["keeninsight_sql_evidence"],1)
        with self.store._connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM keeninsight_evidence WHERE entity_type='sql'").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT count(*) FROM keeninsight_evidence WHERE entity_type='alert'").fetchone()[0],1)
        with sqlite3.connect(archive) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM keeninsight_evidence WHERE entity_type='sql'").fetchone()[0],1)

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
        self.assertTrue(any(item['state']=='not_instrumented' for item in data['capabilities']))

    def test_job_selection_cannot_mix_old_active_win_with_new_no_action(self):
        key=self.observation()
        first=self.store.enqueue_job(key,None,cooldown=0,force=True)
        self.store.finish_job(first,'completed',{'api_model':'old-model','root_causes':['rewrite'],'old_time':12,'new_time':3})
        self.store.add_improvement({'sql_key':key,'status':'candidate','old_time':12,'new_time':3,'rewrite_sql':'select 1',
                                    'validation':{'job_id':first}})
        second=self.store.enqueue_job(key,None,cooldown=0,force=True)
        self.store.finish_job(second,'completed',{'api_model':'new-model','status':'no_action','old_time':11,
                                                'root_causes':['normal'],'explanation':'No justified action'})
        current=self.portal.get('sql/'+key,{})
        self.assertEqual(current['job']['model'],'new-model')
        self.assertIsNone(current['tuning_results']['new_time'])
        self.assertIsNone(current['tuning_results']['rewrite_sql'])
        self.assertEqual(current['tuning_results']['message'],'No justified action')
        previous=self.portal.get('sql/'+key,{'job_id':[first]})
        self.assertEqual(previous['job']['model'],'old-model')
        self.assertEqual(previous['tuning_results']['new_time'],3)
        with self.assertRaises(KeyError):self.portal.get('sql/'+key,{'job_id':['not-this-sql']})

    def test_active_evidence_remains_readable_after_job_history_expires(self):
        key=self.observation()
        job=self.store.enqueue_job(key,None,cooldown=0,force=True)
        self.store.finish_job(job,"completed",{
            "api_model":"retained-real-model","old_time":12,"new_time":3,
            "plan_json":{"Plan":{"Node Type":"Result"}},
            "diagnosis_trace":{"attempts":[{"raw_response":"retained response"}]},
        })
        self.store.add_improvement({"sql_key":key,"status":"active","old_time":12,"new_time":3,
                                    "rewrite_sql":"select 1","validation":{"job_id":job}})
        with self.store._connection() as conn:
            conn.execute("DELETE FROM dream_jobs WHERE job_id=?",(job,))
        for query in ({},{"job_id":[job]}):
            record=self.portal.get("sql/"+key,query)
            self.assertEqual(record["job"]["model"],"retained-real-model")
            self.assertEqual(record["job"]["id"],job)
            self.assertTrue(record["job"]["historyExpired"])
            self.assertEqual(record["jobStatus"],"history_expired")
            self.assertTrue(record["plan"])
            self.assertTrue(record["diagnosisTrace"])
            self.assertEqual(record["jobHistory"],[])
            self.assertFalse(any(row.get("jobId")==job for row in record["executionHistory"]))
        with self.assertRaises(KeyError):
            self.portal.get("sql/"+key,{"job_id":["foreign-job"]})
        other=self.observation(query="select 2",queryid="2")
        with self.assertRaises(KeyError):
            self.portal.get("sql/"+other,{"job_id":[job]})

    def test_execution_and_analysis_timestamps_are_real_lifecycle_not_cpu_samples(self):
        key=self.observation()
        self.store.create_lab_run('ap-run','dream','q88','Fixture',{})
        self.store.update_lab_run('ap-run',result_patch={'sql_key':key})
        eid=self.store.add_lab_execution({'run_id':'ap-run','sql_id':'q88','phase':'baseline',
            'query_text':'select 1','started_at':'2026-10-22T12:00:00+00:00',
            'finished_at':'2026-10-22T12:00:12+00:00','duration_ms':12000,'status':'completed','method':'original SQL'})
        record=self.portal.get('sql/'+eid,{})
        self.assertEqual(record['lifecycle'][0]['executionId'],eid)
        self.assertEqual(record['lifecycle'][0]['durationMs'],12000)
        self.assertIsNone(record['flameGraph'])
        self.assertEqual(record['availability']['lifecycle'],'recorded')

    def test_running_alert_reads_current_stage_journal(self):
        root=Path(self.tmp.name)
        incident=self.store.start_incident({'labels':{'alertname':'Fixture'}},root)
        (root/'workflow_progress.json').write_text(json.dumps({'stages':[{'name':'perf_capture','status':'completed','recordedAt':'2026-10-22T12:00:00Z'}]}))
        detail=self.portal.get('alerts/'+incident,{})
        self.assertEqual(detail['detailAnalysis']['stages'][0]['name'],'perf_capture')

    def test_references_are_real_assets_and_pagination_is_bounded(self):
        root=Path(__file__).resolve().parents[1]
        if not (root/"app/db_profiles/postgresql/common/parameter_metadata.json").exists() or not (
                root/"dream/dream/agent/action/rewrite_rules.jsonl").exists():
            self.skipTest("Optional generated profiles and external DREAM rules are not installed")
        for category in ["code","manual","diagnosisRules"]:
            data=self.portal.get(category,{"limit":["2"]})
            self.assertGreater(data["total"],2)
            self.assertEqual(len(data["items"]),2)
            self.assertTrue(data["items"][0]["source"])
        self.assertEqual(self.portal.get("hosts",{})["items"][0]["status"],"stale")
        self.assertEqual(len(self.portal.get("schema",{})["categories"]),11)
        with self.assertRaises(KeyError):self.portal.get("metrics",{})

    def test_evidence_obeys_retention_and_preserves_active_actions(self):
        keys=[self.observation("select "+str(i),str(i)) for i in [1,2]]
        jobs=[self.store.enqueue_job(key,None,cooldown=0,force=True) for key in keys]
        for job in jobs:self.store.finish_job(job,"completed",{"old_time":12,"new_time":3})
        self.store.add_improvement({"sql_key":keys[0],"status":"active","old_time":12,"new_time":3,
                                    "validation":{"job_id":jobs[0]}})
        with self.store._connection() as conn:
            conn.execute("UPDATE keeninsight_evidence SET captured_at='2000-01-01'")
            prune(conn,"2020-01-01")
            remaining=conn.execute("SELECT entity_id FROM keeninsight_evidence").fetchall()
        self.assertEqual([r[0] for r in remaining],[jobs[0]])

    def test_http_assets_unknown_ids_and_traversal(self):
        server=BridgeHTTPServer(("127.0.0.1",0),BridgeHTTPHandler,self.bridge)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base="http://127.0.0.1:"+str(server.server_port)
        try:
            for page in ["host-list","template","sql-log","alert-detail"]:
                with urlopen(base+"/keeninsight/"+page+".html") as response:
                    body=response.read().decode()
                    self.assertIn('id="ki-root"',body)
                    self.assertNotIn("const sqlLogData",body)
                    self.assertNotIn("realtime-monitor.html",body)
            with urlopen(base+"/api/v1/keeninsight/schema") as response:
                self.assertEqual(json.load(response)["schemaVersion"],"keeninsight.ui.v1")
            with urlopen(base+"/keeninsight/original-layouts.js") as response:
                self.assertIn("window.KeenInsightLayouts", response.read().decode())
            for path,code in [("/keeninsight/../sysinsight_dream_bridge.py",404),
                              ("/keeninsight/realtime-monitor.html",404),
                              ("/api/v1/keeninsight/metrics",404),
                              ("/api/v1/keeninsight/sql/missing",404),
                              ("/api/v1/keeninsight/sql?limit=invalid",400)]:
                with self.assertRaises(HTTPError) as raised:urlopen(base+path)
                self.assertEqual(raised.exception.code,code)
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == "__main__":
    unittest.main()
