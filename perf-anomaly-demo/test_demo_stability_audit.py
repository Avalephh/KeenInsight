"""Audit classification must distinguish execution, causality and fresh LLM."""
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from validate_demo_stability import AuditStatementClient, bounded_lab_context, other_lab_runs, summarize_tp
from lab_controller import LabController
from guard_audit_reentry import derived_jobs
from summarize_demo_stability import render


class StabilityAuditTests(unittest.TestCase):
    def record(self, gain=.30, pressure_delta=.01, drop=.40, source='verified_sysinsight_gpt5.6-sol_selection'):
        return {'scenarioId':'tp_payment_hot','status':'completed','candidateSourceType':source,
                'comparison':{'tuning_gain_ratio':gain,'comparison_valid':True,
                              'external_pressure_delta_ratio':pressure_delta,'pressure_drop_ratio':drop}}

    def test_three_historical_runs_do_not_become_three_fresh_llm_runs(self):
        result=summarize_tp([self.record() for _ in range(3)])[0]
        self.assertTrue(result['stableGainAtLeast10Percent'])
        self.assertFalse(result['freshCandidateUsedEveryRun'])

    def test_single_success_is_not_stability(self):
        self.assertFalse(summarize_tp([self.record()])[0]['stableGainAtLeast10Percent'])

    def test_one_regression_prevents_stable_claim(self):
        self.assertFalse(summarize_tp([self.record(),self.record(),self.record(-.02)])[0]['stableGainAtLeast10Percent'])

    def test_report_pressure_drop_and_cold_baseline_are_separate_requirements(self):
        result=summarize_tp([self.record(pressure_delta=-.17,drop=-.5) for _ in range(3)])[0]
        self.assertFalse(result['allExternalThroughputWithin5Percent'])
        self.assertFalse(result['allBaselineToPressureDropsAtLeast20Percent'])

    def test_actual_lab_timeout_is_90_seconds_without_changing_worker(self):
        bridge=SimpleNamespace(args=SimpleNamespace(db_timeout=30,db_schema='public'))
        context=bounded_lab_context(bridge,None)
        received={}
        def execute(query,**kwargs):
            received.update(kwargs)
            return {'status':'completed','duration_ms':1}
        lab=LabController.__new__(LabController)
        lab.bridge=context
        lab.db=SimpleNamespace(execute_timed=execute)
        lab.store=SimpleNamespace(add_lab_execution=lambda record:record['execution_id'])
        lab._application_prefix=lambda *_:'audit-'
        lab._execute_lab_sql('run','sql','baseline','SELECT 1','test')
        self.assertEqual(received['timeout'],90)
        self.assertEqual(bridge.args.db_timeout,30)

    def test_audit_client_exposes_original_single_statement_and_caps_timeout(self):
        client=AuditStatementClient('keeninsight','postgres','/var/run/postgresql',5432,'postgres',90)
        with patch('validate_demo_stability.subprocess.run',
                   return_value=SimpleNamespace(returncode=0,stderr='')) as run:
            result=client.execute_timed('SELECT 1',search_path=['tpcds','public'],
                                       session_settings={'jit':'off'},timeout=180)
        argv=run.call_args.args[0]
        self.assertEqual(argv[-1],'SELECT 1')
        self.assertTrue(any('search_path=tpcds,public' in item for item in argv))
        self.assertTrue(any('statement_timeout=90000' in item for item in argv))
        self.assertEqual(run.call_args.kwargs['timeout'],95)
        self.assertEqual(result['status'],'completed')

    def test_guard_only_follows_retained_rewrite_identity(self):
        import json
        jobs=[
            {'job_id':'original','canonical_sql':'select a','result_json':json.dumps({'rewrite_sql':'SELECT b'})},
            {'job_id':'child','canonical_sql':'select b','result_json':json.dumps({'rewrite_sql':'SELECT c'})},
            {'job_id':'grandchild','canonical_sql':'select c','result_json':'{}'},
            {'job_id':'unrelated','canonical_sql':'select d','result_json':'{}'},
        ]
        for row in jobs:row['sql_key']=row['job_id']
        descendants,edges=derived_jobs(jobs,{'original'})
        self.assertEqual(descendants,{'child','grandchild'})
        self.assertEqual(len(edges),2)

    def test_replay_wait_excludes_own_audit_but_not_other_work(self):
        own={'run_id':'own','status':'running'}
        other={'run_id':'other','status':'running'}
        self.assertEqual(other_lab_runs([own],('own',)),[])
        self.assertEqual(other_lab_runs([own,other],('own',)),[other])

    def test_report_rejects_reused_job_and_one_good_round(self):
        records=[{'sqlId':'tpcds_q1','round':i,'status':'passed','speedup':3,
                  'jobId':'same-job'} for i in (1,2,3)]
        text=render({},[{'ap':records}])
        self.assertNotIn('- tpcds_q1：3 次',text)
        records[1]['jobId']='second'
        records[2]['jobId']='third'
        text=render({},[{'ap':records}])
        self.assertIn('- tpcds_q1：3 次',text)
        records[2]['speedup']=8
        text=render({},[{'ap':records}])
        self.assertNotIn('- tpcds_q1：3 次',text)
        self.assertIn('重复均有收益，但增益幅度不相近',text)


if __name__=='__main__':unittest.main()
