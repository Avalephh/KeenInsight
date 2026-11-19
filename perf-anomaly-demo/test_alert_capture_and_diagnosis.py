#!/usr/bin/env python3
"""Regression coverage for early perf evidence and honest DREAM diagnosis."""
import ast
import asyncio
import concurrent.futures
import json
import math
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sysinsight_dream_bridge import Bridge, DatabaseClient, StateStore, _canonical_sql


def planner_class():
    path = Path(__file__).resolve().parents[1] / 'dream/dream/agent/plan/planner.py'
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Planner')
    cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {'parse_and_validate', 'llm_predict'}]
    module = ast.Module(body=[cls], type_ignores=[])
    namespace = {'json': json, 'math': math, 'build_root_cause_diagnosis_prompt': lambda *a, **kw: 'real prompt'}
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace['Planner']


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.planner = planner_class()()
        self.planner.root_cause_model = 'test-model'
        self.valid = {'predicted_root_causes': [], 'root_causes': [
            {'label': label, 'confidence': 0.2} for label in (
                'missing indexes', 'suboptimal plan optimizer', 'inappropriate query knobs', 'poorly written queries')]}

    def test_empty_object_not_normal(self):
        for value in ({}, {'root_causes': []}, {'predicted_root_causes': [], 'root_causes': []}):
            self.assertIsNone(self.planner.parse_and_validate(json.dumps(value)))

    def test_valid_normal_retains_four_actual_confidences(self):
        labels, confidences, _ = self.planner.parse_and_validate(json.dumps(self.valid))
        self.assertEqual(labels, ['normal'])
        self.assertEqual(len(confidences), 4)
        self.assertNotIn('normal', confidences)

    def test_bad_labels_ranges_and_duplicates_rejected(self):
        for label, confidence in [('unknown', .2), ('missing indexes', 1.1), ('missing indexes', True), ('missing indexes', float('nan'))]:
            value = json.loads(json.dumps(self.valid))
            value['root_causes'][0] = {'label': label, 'confidence': confidence}
            self.assertIsNone(self.planner.parse_and_validate(json.dumps(value)))
        self.valid['root_causes'][1] = self.valid['root_causes'][0]
        self.assertIsNone(self.planner.parse_and_validate(json.dumps(self.valid)))

    def test_invalid_responses_fail_and_preserve_raw_evidence(self):
        async def respond(prompt):
            return '{}'
        self.planner.diagnosis_agent = respond
        with self.assertRaisesRegex(ValueError, 'not a normal/no_action'):
            asyncio.run(self.planner.llm_predict(None, 'normal', {}))
        self.assertEqual(self.planner.last_diagnosis['status'], 'invalid_response')
        self.assertEqual(len(self.planner.last_diagnosis['attempts']), 6)

    def test_success_records_raw_response(self):
        response = json.dumps(self.valid)
        async def respond(prompt):
            return response
        self.planner.diagnosis_agent = respond
        result = asyncio.run(self.planner.llm_predict(None, 'normal', {'a': .1}))
        self.assertEqual(result['root_causes'], ['normal'])
        self.assertIn('<= 0.5', result['explanation'])
        self.assertEqual(self.planner.last_diagnosis['attempts'][0]['raw_response'], response)

    def test_prompt_does_not_cap_final_confidence_below_execution_threshold(self):
        path = Path(__file__).resolve().parents[1] / 'dream/dream/agent/prompt.py'
        tree = ast.parse(path.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'build_root_cause_diagnosis_prompt')
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        query = SimpleNamespace(execution_time=10, query='select 1', plan_json={},
                                internal_metrics=[0]*12, external_metrics=[[0]]*7,
                                metric_provenance='unavailable, not zero')
        prompt = namespace[function.name](query, 'normal', {'missing indexes': .001})
        self.assertIn('Assign each FINAL confidence independently in [0,1]', prompt)
        self.assertIn('unavailable, not zero', prompt)
        self.assertNotIn('MUST** be restricted to the range', prompt)


class CaptureTests(unittest.TestCase):
    def test_empty_capture_finishes_without_waiting_for_busy_llm_worker(self):
        gate=threading.Event();finished=threading.Event()
        bridge=Bridge.__new__(Bridge)
        bridge.args=SimpleNamespace(sysinsight_full_chain=True,no_api=False)
        bridge.capture_executor=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        bridge.sysinsight_executor=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        bridge.futures=[]
        bridge.store=SimpleNamespace(finish_incident=lambda *args:finished.set())
        bridge._capture_alert_evidence=lambda directory:{'active_rows':[],'prometheus':{}}
        bridge.sysinsight_executor.submit(gate.wait,3)
        try:
            with tempfile.TemporaryDirectory() as directory:
                bridge._launch_sysinsight('empty',Path(directory),{},{},[])
                self.assertTrue(finished.wait(1),'empty capture waited behind the LLM')
                self.assertTrue(bridge.futures[0].cancelled())
                result=json.loads((Path(directory)/'sysinsight_analysis.json').read_text())
                self.assertEqual(result['status'],'skipped')
        finally:
            gate.set();bridge.capture_executor.shutdown();bridge.sysinsight_executor.shutdown()

    def test_recent_idle_only_enabled_for_perf_not_slow_sql_tracking(self):
        db = DatabaseClient.__new__(DatabaseClient)
        queries = []
        db._rows = lambda query, name: queries.append(query) or []
        db.collect_active()
        db.collect_active(include_recent_idle=True)
        self.assertNotIn('state_change >', queries[0])
        self.assertIn("interval '5 seconds'", queries[1])
        self.assertIn("state = 'active'", queries[1])
        self.assertIn("application_name <> 'keeninsight-api'", queries[1])
        self.assertIn('sysinsight-live-', queries[1])

    def test_evidence_acquired_even_while_analysis_worker_is_busy(self):
        gate = threading.Event()
        captured = threading.Event()
        bridge = Bridge.__new__(Bridge)
        bridge.args = SimpleNamespace(sysinsight_full_chain=True, no_api=False)
        bridge.capture_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        bridge.sysinsight_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        bridge.futures = []
        blocker = bridge.sysinsight_executor.submit(gate.wait, 3)
        bridge._capture_alert_evidence = lambda directory: captured.set() or {'actual_evidence': True, 'active_rows':[{'pid':'123'}]}
        bridge._run_sysinsight = lambda *args: args[-1].result()
        try:
            bridge._launch_sysinsight('incident', Path('/tmp/unused-test-path'), {}, {}, [])
            self.assertTrue(captured.wait(1), 'capture was incorrectly queued behind LLM analysis')
            self.assertFalse(bridge.futures[0].done())
            gate.set()
            self.assertTrue(bridge.futures[0].result(timeout=2)['actual_evidence'])
        finally:
            gate.set()
            bridge.capture_executor.shutdown()
            bridge.sysinsight_executor.shutdown()

    def test_recent_idle_pid_is_really_passed_to_perf_and_saved(self):
        bridge = Bridge.__new__(Bridge)
        bridge.args = SimpleNamespace(active_limit=100, sysinsight_perf_seconds=1,
                                      sysinsight_perf_frequency=100, sysinsight_stackcollapse='', sysinsight_window=30)
        bridge.db = SimpleNamespace(collect_active=lambda *a, **kw: [{'pid': '123', 'state': 'idle'}])
        bridge.store = SimpleNamespace(list_lab_runs=lambda *a, **kw: [])
        bridge._collect_prometheus = lambda seconds: {'queries': {}}
        with tempfile.TemporaryDirectory() as directory, patch('sysinsight_live_chain._capture_perf') as perf:
            perf.return_value = {'perf': {'pids': [123]}, 'postprocess': {'status': 'sysinsight_counts_generated'}}
            result = bridge._capture_alert_evidence(Path(directory))
            self.assertEqual(perf.call_args.args[1], [123])
            snapshot = json.loads((Path(directory) / 'backend_snapshot.json').read_text())
            self.assertEqual(snapshot['rows'][0]['state'], 'idle')
            self.assertTrue((Path(directory) / 'perf_capture.json').is_file())

    def test_truly_absent_workload_is_skipped_not_fabricated_or_failed(self):
        bridge = Bridge.__new__(Bridge)
        finished = []
        bridge.store = SimpleNamespace(finish_incident=lambda *args: finished.append(args))
        evidence = concurrent.futures.Future()
        evidence.set_result({'active_rows': [], 'prometheus': {}})
        with tempfile.TemporaryDirectory() as directory:
            result = bridge._run_sysinsight('empty-alert', Path(directory), {}, {}, [], evidence)
            self.assertEqual(result['status'], 'skipped')
            self.assertEqual(result['reason'], 'no_workload_backend_at_alert')
            self.assertEqual(finished[0][1], 'skipped')


class LiveCandidateTests(unittest.TestCase):
    def test_manual_lab_reserves_once_and_exposes_job_before_llm_returns(self):
        from lab_controller import LabController
        with tempfile.TemporaryDirectory() as directory, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            store=StateStore(Path(directory)/'state.sqlite3')
            store.create_lab_run('manual','dream','fixture','Fixture',{})
            controller=LabController.__new__(LabController)
            controller.store=store;controller.root=Path(directory)
            seen=[]
            def worker(job_id):
                seen.append(store.get_lab_run('manual')['result'].get('job_id'))
                store.start_job(job_id)
                store.finish_job(job_id,'completed',{'status':'no_action','old_time':12})
                return {'status':'no_action'}
            controller.bridge=SimpleNamespace(args=SimpleNamespace(db='demo',db_user='tester'),
                executor=executor,futures=[],_run_dream_job=worker)
            controller._execute_lab_sql=lambda *args:('baseline',{'status':'completed','duration_ms':12000})
            original=store.enqueue_job
            def reserve(*args,**kwargs):
                self.assertTrue(store._lock._is_owned())
                return original(*args,**kwargs)
            with patch.object(store,'enqueue_job',side_effect=reserve):
                controller._run_dream('manual',{'sql_id':'fixture','query':'select 1','title':'Fixture'},threading.Event())
            run=store.get_lab_run('manual')
            self.assertEqual(run['status'],'completed')
            self.assertEqual(seen,[run['result']['job_id']])
            self.assertEqual(store.get_job(seen[0])['trigger_source'],'manual')

    def test_explicit_read_only_validation_probe_has_priority(self):
        from sysinsight_live_chain import _sql_from_case
        sql,source=_sql_from_case({'active_queries':[{'query':'select slow from big_table'}]},'SELECT count(*) FROM pg_catalog.pg_class')
        self.assertEqual(source,'configured_validation_sql')
        self.assertEqual(sql,'SELECT count(*) FROM pg_catalog.pg_class')
        with self.assertRaises(ValueError):_sql_from_case({},'DELETE FROM important_table')

    def test_completed_long_sql_survives_active_to_idle_poll_race(self):
        with tempfile.TemporaryDirectory() as directory:
            store=StateStore(Path(directory)/'state.sqlite3')
            query='SELECT count(*) FROM demo WHERE x > 1'
            row={'database':'demo','username':'tester','queryid':'99','query':query,'calls':1,
                 'total_time_ms':12000,'mean_time_ms':12000,'max_time_ms':12000}
            key=store.upsert_observations([row],[{'query':query,'username':'tester'}])[0]
            self.assertEqual(store.slow_observations(10000,10000,1,10,exclude_canonical_sql=[_canonical_sql(query)],completed_only=True),[])
            # Another poll has no call/time delta, but the previously observed
            # completed call must remain eligible when its backend turns idle.
            store.upsert_observations([row],[])
            slow=store.slow_observations(10000,10000,1,10,completed_only=True)
            self.assertEqual([item['sql_key'] for item in slow],[key])
            job=store.enqueue_job(key,None,0)
            self.assertEqual(store.get_job(job)['trigger_source'],'automatic_completed_sql')
            self.assertEqual(store.get_job(job)['source_completion_ms'],12000)
            self.assertEqual(store.slow_observations(10000,10000,1,10,completed_only=True),[])

    def test_short_sql_completion_never_passes_ten_second_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            store=StateStore(Path(directory)/'state.sqlite3')
            store.upsert_observations([{'database':'demo','username':'tester','queryid':'1','query':'select 1',
                'calls':1,'total_time_ms':50,'mean_time_ms':50,'max_time_ms':50}],[])
            self.assertEqual(store.slow_observations(10000,10000,1,10,completed_only=True),[])

    def test_null_and_boolean_literals_match_postgresql_without_erasing_predicates(self):
        self.assertEqual(_canonical_sql('SELECT CASE WHEN x>0 THEN true ELSE NULL END FROM t'),
                         _canonical_sql('SELECT CASE WHEN x>$1 THEN $2 ELSE $3 END FROM t'))
        self.assertNotEqual(_canonical_sql('SELECT x IS NULL FROM t'),_canonical_sql('SELECT x IS TRUE FROM t'))
        self.assertNotEqual(_canonical_sql('SELECT "null" FROM t'),_canonical_sql('SELECT NULL FROM t'))
        self.assertIn('is not null',_canonical_sql('SELECT x IS NOT NULL FROM t'))

    def test_scalar_literal_migration_preserves_job_links_and_replay_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite3'
            store=StateStore(path)
            query='SELECT CASE WHEN x>0 THEN x ELSE NULL END FROM t'
            values={'database':'demo','username':'tester','queryid':'42','query':query,'calls':1,'total_time_ms':12000,'max_time_ms':12000,'mean_time_ms':12000}
            key=store.upsert_observations([values],[{'query':query,'username':'tester'}])[0]
            job=store.enqueue_job(key,None,0,force=True)
            old_canonical='select case when x>? then x else null end from t'
            with store._connection() as conn:
                conn.execute("UPDATE meta SET value='1' WHERE key='canonical_sql_version'")
                conn.execute('UPDATE sql_observations SET canonical_sql=? WHERE sql_key=?',(old_canonical,key))
                conn.execute('UPDATE live_sql_samples SET canonical_sql=?',(old_canonical,))
            migrated=StateStore(path)
            normalized={**values,'query':'SELECT CASE WHEN x>$1 THEN x ELSE $2 END FROM t'}
            self.assertEqual(migrated.upsert_observations([normalized],[])[0],key)
            self.assertEqual(migrated.get_job(job)['sql_key'],key)
            self.assertEqual(migrated.get_observation(key)['replay_sql'],query)
            with migrated._connection() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM sql_observations').fetchone()[0],1)

    def test_real_rule_candidates_are_preserved_without_claiming_application(self):
        repo=Path(__file__).resolve().parents[1]
        path=repo/'dream/dream/agent/action/rule_matcher.py'
        spec=importlib.util.spec_from_file_location('real_rule_matcher_test',path)
        rules=importlib.util.module_from_spec(spec);spec.loader.exec_module(rules)
        tree=ast.parse((repo/'dream/dream/agent/prompt.py').read_text())
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='build_action_space_prompt')
        namespace={}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'prompt.py','exec'),namespace)
        query=SimpleNamespace(query='SELECT (SELECT sum(x) FROM t WHERE x > 0) FROM t GROUP BY x')
        with patch.dict(sys.modules,{'dream.agent.action.rule_matcher':rules}):
            prompt=namespace[function.name](['poorly written queries'],None,None,None,query)
        self.assertGreater(len(query.rewrite_rule_candidates),0)
        self.assertTrue(all(rule['name'] in prompt for rule in query.rewrite_rule_candidates))
        self.assertTrue(all('applied' not in rule for rule in query.rewrite_rule_candidates))

    def test_result_comparison_fallback_is_never_reported_as_formal_proof(self):
        repo=Path(__file__).resolve().parents[1]
        tree=ast.parse((repo/'dream/dream/database/pg_env.py').read_text())
        method=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='check_sql_equivalence')
        namespace={'shutil':SimpleNamespace(which=lambda value:None)}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'pg_env.py','exec'),namespace)
        db=SimpleNamespace(compare_sql_results=lambda *args:True)
        db.check_sql_equivalence=lambda *args:namespace['check_sql_equivalence'](db,*args)
        tree=ast.parse((repo/'dream/dream/agent/action/action_evaluation.py').read_text())
        function=next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='check_sql_rewrite')
        exec(compile(ast.Module(body=[function],type_ignores=[]),'action_evaluation.py','exec'),namespace)
        result=asyncio.run(namespace['check_sql_rewrite']('select 1','select 1',db=db,fix_agent=None))
        self.assertEqual(result,'select 1')
        self.assertFalse(db.last_equivalence_check['formal_proof'])
        self.assertEqual(db.last_rewrite_validation['status'],'result_set_comparison_passed')
        self.assertIsNone(db.last_rewrite_validation['qed_equivalence'])

    def test_live_prompt_does_not_invent_upstream_scale_or_threads(self):
        from sysinsight_original_llm import live_workload_template
        legacy='- Benchmark tool: tpcc;\n - Data scale: 100 w;\n - Concurrent threads: 16 threads;'
        prompt=live_workload_template(legacy,{'mode':'live_alert','workload':'tpcc','active_queries':[{},{}]})
        self.assertNotIn('16 threads',prompt)
        self.assertIn('"backend_sessions_in_alert_snapshot": 2',prompt)
        self.assertIn('not measured',prompt)
        self.assertEqual(live_workload_template(legacy,{'mode':'benchmark'}),legacy)
    def test_invalid_first_response_does_not_discard_valid_source_selected_candidate(self):
        from sysinsight_live_chain import _candidate_and_validation
        api = {'api_generated_configurations': [
            {'phase':'acquisition','parse_status':'parsed','call_index':i,
             'parsed_by_source_LLM_ACQ__convert_to_json': config}
            for i,config in enumerate([{'work_mem':-999}, {'work_mem':8192}, {'work_mem':16384}])],
            'source_selector': {'selected_row_index': 2}}
        args = SimpleNamespace(dbms='postgresql',pg_version='12',candidate_count=1)
        with patch('sysinsight_candidate_benchmark.validate_candidate_live',return_value={'status':'completed'}):
            result = _candidate_and_validation(api,args,None)
        self.assertEqual(result['candidate_count'],3)
        self.assertFalse(result['candidates'][0]['validation']['valid'])
        self.assertEqual(result['selected_candidate']['configuration']['work_mem'],16384)
        self.assertEqual(result['selected_candidate']['candidate_id'],'candidate-002')

    def test_stage_journal_is_available_before_final_result(self):
        from sysinsight_live_chain import StageJournal, _stage
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'workflow_progress.json'
            journal=StageJournal(path)
            _stage(journal,'source_evidence','completed',{'functions':5})
            saved=json.loads(path.read_text())
            self.assertEqual(saved['stages'][0]['name'],'source_evidence')
            self.assertIn('recordedAt',saved['stages'][0])

    def test_running_stage_is_replaced_by_its_real_completion(self):
        from sysinsight_live_chain import StageJournal, _stage
        with tempfile.TemporaryDirectory() as directory:
            journal=StageJournal(Path(directory)/'workflow_progress.json')
            _stage(journal,'llm','running')
            _stage(journal,'llm','completed',{'model':'actual-test-model'})
            self.assertEqual(len(journal),1)
            self.assertEqual(journal[0]['status'],'completed')
            self.assertGreaterEqual(journal[0]['durationMs'],0)
            self.assertIn('finishedAt',journal[0])


if __name__ == '__main__':
    unittest.main()
