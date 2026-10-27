"""Real attribution contracts: strict SQL windows, sample mass and model SHAP."""
from pathlib import Path
from types import SimpleNamespace
import copy
import json
import math
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "app"))
from sql_profiling import SQLProfiler, parse_script, stack_tree, digest
from sysinsight_dream_bridge import StateStore, _canonical_sql
from keeninsight_portal import KeenInsightPortal
from sysinsight_shap import DEFAULT_MODEL, explain, explain_subprocess, compact_evidence, reliability, llm_evidence
from collect_shap_windows import aligned_tps, sampled_window


class SQLProfilesTest(unittest.TestCase):
    def test_parser_discards_other_pid_and_queries_outside_validated_window(self):
        script='''  41 100.100000: cpu-clock:
                7ff123 leaf (postgres)
                7ff124 root (postgres)

  42 100.200000: cpu-clock:
                7ff123 other_connection (postgres)

  41 101.200000: cpu-clock:
                7ff123 next_query_same_connection (postgres)

  41 100.300000: cpu-clock:
                7ff123 leaf (postgres)
                7ff124 root (postgres)
'''
        stacks,stats=parse_script(script.splitlines(),41,100,101)
        self.assertEqual(stacks,{('root','leaf'):2})
        self.assertEqual(stats['sampleCount'],2)
        self.assertEqual(stats['discardedEvents'],2)

    def test_pid_reuse_or_same_text_next_execution_never_matches(self):
        original={'pid':42,'backend_start':'connection1','query_start':'query1','query':'select 1','state':'active'}
        self.assertTrue(SQLProfiler.same_query(original,dict(original)))
        for key,value in [('pid',43),('backend_start','connection2'),('query_start','query2'),('query','select 2'),('state','idle')]:
            changed={**original,key:value}
            self.assertFalse(SQLProfiler.same_query(original,changed),key)
        self.assertFalse(SQLProfiler.same_query(original,None))

    def test_parallel_worker_requires_leader_title_query_and_kernel_identity(self):
        leader={'pid':41,'backend_start':'leaderbirth','query_start':'q1','query':'select 1','datname':'db','state':'active'}
        kernel={'uid':101,'parentPid':5,'executable':'/usr/lib/postgresql/12/bin/postgres','startTicks':'900','title':'leader'}
        worker={**leader,'pid':42,'backend_start':'workerbirth','backend_type':'parallel worker'}
        worker_os={**kernel,'startTicks':'901','title':'postgres: 12/main: parallel worker for PID 41'}
        self.assertTrue(SQLProfiler.verified_worker(leader,kernel,worker,worker_os))
        for key,value in [('query_start','q2'),('query','select 2'),('datname','other'),('state','idle'),('backend_type','client backend'),('backend_start',None)]:
            self.assertFalse(SQLProfiler.verified_worker(leader,kernel,{**worker,key:value},worker_os),key)
        for key,value in [('title','postgres: 12/main: parallel worker for PID 414'),('uid',102),('parentPid',6),('executable','/other'),('startTicks',None)]:
            self.assertFalse(SQLProfiler.verified_worker(leader,kernel,worker,{**worker_os,key:value}),key)
        self.assertFalse(SQLProfiler.verified_worker(leader,None,worker,worker_os))

    def test_participant_birth_token_prevents_pid_reuse(self):
        row={'pid':42,'backend_start':'connection','query_start':'query','query':'select 1','state':'active','_os':{'startTicks':'birth1'}}
        self.assertTrue(SQLProfiler.same_participant(row,copy.deepcopy(row)))
        self.assertFalse(SQLProfiler.same_participant(row,{**row,'_os':{'startTicks':'birth2'}}))
        self.assertFalse(SQLProfiler.same_participant({**row,'_os':None},{**row,'_os':None}))

    def test_each_pid_has_its_own_window_and_role_in_parallel_flamegraph(self):
        script='''41 100.100000: cpu-clock:
            aa leader_fn (postgres)

42 100.100000: cpu-clock:
            aa too_early_worker (postgres)

42 100.400000: cpu-clock:
            aa worker_fn (postgres)

42 100.900000: cpu-clock:
            aa reused_after_worker_ended (postgres)

43 100.400000: cpu-clock:
            aa unrelated_identical_sql (postgres)
'''
        stacks,counts=parse_script(script.splitlines(),41,100,101,windows={41:(100,101),42:(100.2,100.8)},labels={41:'leader PID 41',42:'parallel worker PID 42'})
        self.assertEqual(counts['samplesByPid'],{'41':1,'42':1})
        self.assertEqual(counts['sampleCount'],2)
        self.assertEqual(counts['discardedEvents'],3)
        self.assertEqual(stacks,{('leader PID 41','leader_fn'):1,('parallel worker PID 42','worker_fn'):1})

    def test_group_snapshot_never_adds_an_unrelated_worker(self):
        leader={'pid':41,'backend_start':'leader','query_start':'q1','query':'select 1','datname':'db','state':'active','backend_type':'client backend'}
        worker={**leader,'pid':42,'backend_start':'worker','backend_type':'parallel worker'}
        unrelated={**worker,'pid':43}
        kernel={'uid':101,'parentPid':5,'executable':'postgres','startTicks':'900','title':'postgres'}
        identities={41:kernel,42:{**kernel,'title':'postgres: parallel worker for PID 41'},43:{**kernel,'title':'postgres: parallel worker for PID 99'}}
        profiler=SQLProfiler(SimpleNamespace(_rows=lambda *a:[leader,worker,unrelated]),None,'/unused',_canonical_sql)
        with patch.object(profiler,'process_identity',side_effect=lambda pid:identities[pid]):
            self.assertEqual(set(profiler._group_snapshot(leader)),{41,42})
        with patch.object(profiler,'process_identity',return_value=None):
            self.assertEqual(profiler._group_snapshot(leader),{})

    def test_recorder_rotates_when_workers_join_and_leave_and_discards_worker_tail(self):
        leader={'pid':41,'backend_start':'leader','query_start':'q1','query':'select 1','state':'active','role':'leader','_os':{'startTicks':'birth1'}}
        worker={**leader,'pid':42,'backend_start':'worker','role':'parallel worker','_os':{'startTicks':'birth2'}}
        solo={41:leader};parallel={41:leader,42:worker}
        clock=[100.0]
        def wait(seconds):clock[0]+=seconds;return False
        stop=SimpleNamespace(wait=wait,is_set=lambda:False)
        recorder=SimpleNamespace(poll=lambda:None)
        def launch(command,**kw):Path(command[-1]).write_bytes(b'actual-test-fixture');return recorder
        scripts={
            'cpu-0.perf.data':'41 100.100000: cpu-clock:\n aa leader_fn (postgres)\n',
            'cpu-1.perf.data':'41 100.500000: cpu-clock:\n aa leader_fn (postgres)\n\n42 100.500000: cpu-clock:\n aa worker_fn (postgres)\n\n42 100.800000: cpu-clock:\n aa discarded_tail (postgres)\n',
            'cpu-2.perf.data':'41 100.950000: cpu-clock:\n aa discarded_unvalidated_tail (postgres)\n'}
        def decode(command,**kw):
            kw['stdout'].write(scripts[Path(command[3]).name]);return SimpleNamespace(returncode=0)
        profiler=SQLProfiler(None,None,'/unused',_canonical_sql,seconds=2)
        with tempfile.TemporaryDirectory() as tmp, patch.object(profiler,'_group_snapshot',side_effect=[solo,parallel,parallel,solo,{}]), \
             patch('sql_profiling.time.monotonic',side_effect=lambda:clock[0]), \
             patch('sql_profiling.subprocess.Popen',side_effect=launch),patch('sql_profiling.subprocess.run',side_effect=decode), \
             patch('sql_profiling.threading.Timer'),patch.object(profiler,'_stop_process'):
            record={'startedAt':'test'}
            stacks,metadata=profiler._capture_group(leader,Path(tmp),'perf',stop,record)
        self.assertEqual([s['pids'] for s in metadata['segments']],[[41],[41,42],[41]])
        self.assertEqual(metadata['samplesByPid'],{'41':2,'42':1})
        self.assertEqual(metadata['sampleCount'],3)
        self.assertEqual(metadata['discardedEvents'],2)
        self.assertFalse(any('discarded' in frame for stack in stacks for frame in stack))
        self.assertEqual(metadata['endReason'],'query_ended_or_changed')

    def test_tree_preserves_real_mass_and_bounds_size(self):
        stacks={('root',str(i),'leaf'):i+1 for i in range(3000)}
        tree=stack_tree(stacks,80)
        def walk(node):
            self.assertLessEqual(sum(c['value'] for c in node['children']),node['value'])
            return 1+sum(walk(c) for c in node['children'])
        self.assertLessEqual(walk(tree),80)
        self.assertEqual(tree['value'],sum(stacks.values()))
        self.assertEqual(sum(c['value'] for c in tree['children']),tree['value'])

    def test_profile_failure_does_not_fail_or_repeat_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp)/'state.db')
            db=SimpleNamespace(db='test',_rows=lambda *a:(_ for _ in ()).throw(RuntimeError('permission denied')))
            profiler=SQLProfiler(db,store,tmp,_canonical_sql)
            called=[]
            with profiler.managed('unique','select 1','execution1','run1','baseline'):
                called.append('one actual SQL execution')
            self.assertEqual(len(called),1)
            with store._connection() as conn:
                row=conn.execute('SELECT * FROM sql_profiles').fetchone()
                self.assertEqual(row['status'],'failed')
                self.assertIsNone(json.loads(row['payload_json'])['flameGraph'])
            self.assertTrue(profiler._slot.acquire(False))
            profiler._slot.release();profiler.close()

    def test_disabled_profiling_does_not_start_observer(self):
        profiler=SQLProfiler(None,None,'/unused',_canonical_sql,enabled=False)
        with profiler.managed('app','select 1','id','run','baseline'):
            pass
        self.assertEqual(profiler._tasks,{})

    def test_sampling_probe_has_own_timeout_without_modifying_database_client(self):
        db=SimpleNamespace(timeout=90)
        profiler=SQLProfiler(db,None,'/unused',_canonical_sql,enabled=False)
        self.assertEqual(profiler._probe.timeout,2)
        self.assertEqual(db.timeout,90)

    def test_busy_sampler_does_not_queue_old_queries(self):
        profiler=SQLProfiler(None,None,'/unused',_canonical_sql)
        profiler._slot.acquire()
        try:self.assertIsNone(profiler._submit('app','select 1','id','run','baseline'))
        finally:profiler._slot.release();profiler.close()

    def test_clear_pause_prevents_sampling_and_resumes(self):
        profiler=SQLProfiler(None,None,'/unused',_canonical_sql)
        with profiler.pause():
            self.assertIsNone(profiler._submit('app','select 1','id','run','baseline'))
            self.assertTrue(profiler._paused.is_set())
        self.assertFalse(profiler._paused.is_set())
        profiler.close()

    def test_exact_execution_profile_does_not_fall_back_to_another_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp)/'state.db')
            bridge=SimpleNamespace(store=store,args=SimpleNamespace(db='test',port=5432),last_collection_error='')
            portal=KeenInsightPortal(bridge)
            store.create_lab_run('run','dream','fixture','Test',{})
            for eid in ['execution1','execution2']:
                store.add_lab_execution({'execution_id':eid,'run_id':'run','sql_id':'fixture','query_text':'select 1','phase':'baseline','status':'completed'})
            profile={'profileId':'profile1','executionId':'execution1','status':'completed','requestedAt':'2026-10-23','flameGraph':{'name':'root','value':2,'children':[]}}
            profiler=SQLProfiler(SimpleNamespace(db='test'),store,tmp,_canonical_sql)
            profile['sqlSha256']=digest(_canonical_sql('select 1'))
            profiler._save(profile)
            record=portal.get('sql/execution1',{})
            self.assertEqual(record['flameGraph']['value'],2)
            self.assertEqual(record['sqlProfile']['executionId'],'execution1')
            self.assertIsNone(portal.get('sql/execution2',{})['flameGraph'])

    def test_profile_retention_and_clear_are_bounded_to_sql_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp)/'state.db')
            profiler=SQLProfiler(SimpleNamespace(db='test'),store,tmp,_canonical_sql)
            profiler._save({'profileId':'old','executionId':'exec','sqlSha256':'sha','requestedAt':'2000-01-01','finishedAt':'2000-01-01','status':'completed','flameGraph':None})
            store.prune_history(1,1000,1000)
            with store._connection() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM sql_profiles').fetchone()[0],0)


@unittest.skipUnless((DEFAULT_MODEL/'manifest.json').exists(),'local trained SHAP model not built')
class RealShapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest=json.loads((DEFAULT_MODEL/'manifest.json').read_text())
        cls.dataset=json.loads((DEFAULT_MODEL/'training_provenance.json').read_text())

    def profile_fixture(self,tmp,record=None):
        record=record or self.dataset['records'][0]
        path=Path(tmp)/'counts.txt'
        text='Function\tSampling Rate (%)\n'
        text+='\n'.join('{}\t{}%'.format(n,v) for n,v in zip(self.manifest['featureNames'],record['values']))
        path.write_text(text)
        return path

    def test_real_shap_values_sum_to_actual_forest_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=explain(self.profile_fixture(tmp))
        self.assertEqual(result['status'],'completed')
        self.assertIn('tree_path_dependent',result['method'])
        self.assertAlmostEqual(result['baseValue']+sum(x['shapValue'] for x in result['allFeatures']),result['prediction'],places=8)
        self.assertAlmostEqual(sum(x['shapValue'] for x in result['features']),result['sumShapValues'],places=8)
        self.assertTrue(any(x['shapValue']<0 for x in result['allFeatures']))
        self.assertTrue(any(x['shapValue']>0 for x in result['allFeatures']))
        self.assertIn('not causation',str(result['limitations']))
        self.assertFalse(result['reliability']['decisionEligible'])
        self.assertNotIn('prediction',llm_evidence(result))
        compact=compact_evidence(result)
        self.assertNotIn('allFeatures',compact)
        self.assertNotIn('folds',compact['validation'])
        self.assertEqual(compact['features'],result['features'])
        self.assertIn('allFeatures',result,'Compaction must not destroy the full retained attribution')

    def test_missing_or_tampered_model_has_no_attributions(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=explain_subprocess(self.profile_fixture(tmp),Path(tmp)/'missing')
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(result['features'],[])
            target=Path(tmp)/'model';target.mkdir()
            for name in ['manifest.json','forest.joblib','training_provenance.json']:
                shutil.copyfile(DEFAULT_MODEL/name,target/name)
            with (target/'forest.joblib').open('ab') as handle:handle.write(b'tampered')
            with self.assertRaisesRegex(ValueError,'digest mismatch'):explain(self.profile_fixture(tmp),target)

    def test_training_groups_do_not_leak_across_validation_folds(self):
        for fold in self.manifest['validation']['folds']:
            self.assertFalse(set(fold['trainingGroups'])&set(fold['heldOutGroups']))
        profiles=[r['profileSha256'] for r in self.dataset['records']]
        self.assertEqual(len(profiles),len(set(profiles)))
        for row in self.dataset['records']:
            self.assertAlmostEqual(row['target'],row['pressureControlTps']/row['baselineControlTps'])
        self.assertGreater(self.manifest['validation']['maePercentagePoints'],0)


class ShapReliabilityTest(unittest.TestCase):
    def test_tps_labels_include_only_whole_foreground_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'control.log'
            path.write_text('\n'.join('progress: {} s, {} tps, lat 1 ms'.format(i,100+i) for i in range(1,10)))
            result=aligned_tps(path,1.5,8.5)
            self.assertEqual(result['coveredSeconds'],6)
            self.assertEqual(result['intervals'][0]['startSeconds'],2)
            self.assertEqual(result['intervals'][-1]['endSeconds'],8)
            self.assertAlmostEqual(result['tps'],105.5)
            with self.assertRaisesRegex(ValueError,'Insufficient'):aligned_tps(path,2.5,4.5)

    def test_perf_alignment_uses_actual_event_times_not_decode_elapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'perf.script'
            path.write_text('postgres 42 100.200000: 1 cycles:\n\taa fn (postgres)\npostgres 43 108.200000: 1 cycles:\n')
            self.assertEqual(sampled_window(path,96),(4.700000000000003,11.700000000000003))
            path.write_text('decode took 99 seconds, but no actual sample\n')
            with self.assertRaisesRegex(ValueError,'No actual'):sampled_window(path,96)

    def test_accuracy_gate_is_not_the_additivity_residual(self):
        good={'maePercentagePoints':4,'rows':120,'scenarioGroups':20}
        context={'workload':'tpcc','phase':'pressure_before'}
        self.assertTrue(reliability(good,{},context)['decisionEligible'])
        for bad in ({**good,'maePercentagePoints':11},{**good,'rows':6},{**good,'scenarioGroups':6}):
            self.assertFalse(reliability(bad,{},context)['decisionEligible'])
        self.assertFalse(reliability(good,{'outsideTrainingRangeFeatures':['LockAcquire']},context)['decisionEligible'])
        self.assertFalse(reliability(good,{},None)['decisionEligible'])
        self.assertFalse(reliability(good,{},dict(context,workload='tpcds'))['decisionEligible'])
        self.assertFalse(reliability(good,{},dict(context,phase='baseline'))['decisionEligible'])

    def test_low_trust_and_historical_shap_numerics_are_withheld_from_llm(self):
        for trust in ({},{'decisionEligible':False}):
            evidence=llm_evidence({'status':'completed','modelVersion':'x','prediction':.9,'features':[{'name':'LockAcquire'}],'reliability':trust})
            self.assertNotIn('prediction',evidence)
            self.assertNotIn('features',evidence)
            self.assertEqual(evidence['status'],'excluded_low_confidence')
        self.assertIn('prediction',llm_evidence({'reliability':{'decisionEligible':True},'prediction':.5}))


if __name__=='__main__':unittest.main()
