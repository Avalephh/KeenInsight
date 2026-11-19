#!/usr/bin/env python3
"""Real, sequential TP/AP demonstration audit. Never overwrite old results.

TP uses the supported console HTTP path as-is and records whether its actual
candidate comes from history; fresh alert LLM output is NOT relabeled as the
candidate used for TPS. AP repeats explicitly invoke a new real DREAM worker.
No artificial delays, fake TPS, forced alerts, global stats reset or wholesale
history deletion. Only matching active AP actions are temporarily retired.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from sysinsight_dream_bridge import Bridge, DatabaseClient, StateStore, _canonical_sql, parse_args
from lab_controller import LabController
from sql_profiling import SQLProfiler

BASE='http://127.0.0.1:9108'
STATE=ROOT.parent/'monitoring/data/sysinsight_dream_bridge.sqlite3'


def stamp():return dt.datetime.now(dt.timezone.utc).isoformat()


def http(path,body=None):
    request=Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json'})
    try:
        with urlopen(request,timeout=30) as response:return json.load(response)
    except HTTPError as exc:raise RuntimeError('HTTP {}: {}'.format(exc.code,exc.read().decode()[:800]))


def rows(sql,values=()):
    with sqlite3.connect(STATE.as_uri()+'?mode=ro',uri=True,timeout=10) as conn:
        conn.row_factory=sqlite3.Row
        return [dict(r) for r in conn.execute(sql,values)]


def other_lab_runs(current,ignore_run_ids=()):
    return [r for r in (current or []) if r.get('run_id') not in ignore_run_ids]


def idle(timeout=1800,ignore_run_ids=()):
    deadline=time.monotonic()+timeout
    while True:
        active=rows("SELECT incident_id,status FROM incidents WHERE status IN ('running','queued')")
        active+=rows("SELECT job_id,status FROM dream_jobs WHERE status IN ('running','queued')")
        lab=other_lab_runs(http('/api/v1/lab/status').get('current'),ignore_run_ids)
        if not active and not lab:return
        if time.monotonic()>deadline:raise RuntimeError('Existing work did not drain; no overlapping experiment launched')
        time.sleep(3)


def wait_run(run_id,timeout=1800):
    deadline=time.monotonic()+timeout;next_log=0
    while time.monotonic()<deadline:
        run=http('/api/v1/lab/runs/'+run_id)
        if time.monotonic()>next_log:
            print('RUN',run_id,run['phase'],run['status'],flush=True);next_log=time.monotonic()+30
        if run['status'] not in ('running','queued'):return run
        time.sleep(2)
    raise RuntimeError('Bounded wait expired for '+run_id+'; do not launch another load')


def wait_job(job_id,timeout=1800):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        job=http('/api/v1/jobs/'+job_id)
        if job['status'] not in ('running','queued'):return job
        time.sleep(3)
    raise RuntimeError('DREAM job wait expired: '+job_id)


def summarize_tp(records):
    summaries=[]
    for sid in dict.fromkeys(r['scenarioId'] for r in records):
        group=[r for r in records if r['scenarioId']==sid]
        complete=[r for r in group if r.get('status')=='completed']
        gains=[r['comparison'].get('tuning_gain_ratio') for r in complete]
        valid=[v for v in gains if isinstance(v,(int,float))]
        summaries.append({'scenarioId':sid,'attempts':len(group),'completed':len(complete),
            'gainsPercent':[v*100 if isinstance(v,(int,float)) else None for v in gains],
            'allComparisonsValid':bool(complete) and all(r['comparison'].get('comparison_valid') for r in complete),
            'allExternalThroughputWithin5Percent':bool(complete) and all(abs(r['comparison'].get('external_pressure_delta_ratio',999))<=.05 for r in complete),
            'allBaselineToPressureDropsAtLeast20Percent':bool(complete) and all(
                r['comparison'].get('pressure_drop_ratio',-999)>=.20 for r in complete),
            'stableGainAtLeast10Percent':len(complete)>=3 and len(valid)==len(complete)
                and all(r['comparison'].get('comparison_valid') for r in complete) and min(valid)>=.10,
            'freshCandidateUsedEveryRun':bool(complete) and all(r.get('candidateSourceType')=='fresh_online_llm' for r in complete)})
    return summaries


def run_tp(args,report,save):
    if args.wait_for_ap_audit:
        report['status']='waiting_for_ap';save()
        print('TP waiting for AP audit to finish; no overlapping database load',flush=True)
        while True:
            other=json.loads(Path(args.wait_for_ap_audit).read_text())
            if other.get('finishedAt'):
                if other.get('status')!='completed':
                    raise RuntimeError('AP did not complete successfully; do not add TP load')
                break
            time.sleep(5)
        report['status']='running';save()
    catalog=http('/api/v1/lab/catalog')
    cases=catalog['tpcc_focus_scenarios']
    for repeat in range(args.tp_rounds):
        for case in cases[repeat%len(cases):]+cases[:repeat%len(cases)]:
            previous=next((r for r in report['tp'] if r['scenarioId']==case['scenario_id']
                           and r['round']==repeat+1),None)
            if previous:
                if previous.get('status')!='completed':
                    raise RuntimeError('Refuse to skip an unsuccessful TP measurement')
                continue
            idle()
            entry={'scenarioId':case['scenario_id'],'round':repeat+1,'startedAt':stamp(),'status':'starting'}
            report['tp'].append(entry);save()
            body={'scenario_id':case['scenario_id'],'normal_clients':2,'pressure_clients':case['clients'],
                  'baseline_seconds':args.baseline_seconds,'pressure_seconds':args.pressure_seconds}
            started=http('/api/v1/actions/lab/tpcc/start',body)
            run_id=started['run']['run_id'];entry['runId']=run_id;save()
            print('TP_START',repeat+1,case['scenario_id'],run_id,flush=True)
            run=wait_run(run_id)
            directory=Path(report['directory'])/(case['scenario_id']+'-round-'+str(repeat+1));directory.mkdir()
            (directory/'lab.json').write_text(json.dumps(run,ensure_ascii=False,indent=2))
            result=run.get('result')or{};tuning=result.get('tuning')or{}
            entry.update(status=run['status'],finishedAt=stamp(),comparison=result.get('comparison')or{},
                         candidateSourceType=tuning.get('source_type'),candidateSource=tuning.get('source'),
                         requestedConfiguration=tuning.get('requested_configuration'),
                         application=tuning.get('application'),error=run.get('error'))
            save()
            # A fresh alert chain may still be applying global settings. Drain
            # it before the next case, and preserve its distinct metrics.
            idle()
            incidents=rows('SELECT * FROM incidents WHERE started_at>=? AND started_at<=? ORDER BY started_at',
                           (entry['startedAt'],entry['finishedAt']))
            for incident in incidents:
                j=json.loads(incident.get('sysinsight_json')or'{}');chain=j.get('live_chain')or{};chain=chain.get('result')or chain
                incident['workflowSummary']={'status':chain.get('status'),'apiStatus':(chain.get('api')or{}).get('status'),
                    'comparison':chain.get('comparison'),'stages':[(s.get('name'),s.get('status')) for s in chain.get('stages',[])],
                    'configuration':((chain.get('candidate_validation')or{}).get('selected_candidate')or{}).get('configuration')}
                incident.pop('sysinsight_json',None);incident.pop('alert_json',None)
            entry['alertIncidents']=incidents
            entry['newLLMCandidateWasUsedForTps']=False
            (directory/'incidents.json').write_text(json.dumps(incidents,ensure_ascii=False,indent=2))
            report['tpSummary']=summarize_tp(report['tp']);save()
            cmp=entry['comparison'];print('TP_DONE',repeat+1,case['scenario_id'],'TPS',cmp.get('pressure_before_tps'),'->',cmp.get('pressure_after_tps'),
                'gain',cmp.get('tuning_gain_ratio'),'source',entry.get('candidateSourceType'),flush=True)
            if run['status']!='completed':raise RuntimeError('TP failed; stop instead of masking it: '+str(run.get('error')))


def retire_matching(sql_key):
    previous=rows("SELECT improvement_id FROM improvements WHERE sql_key=? AND status='active'",(sql_key,))
    for item in previous:http('/api/v1/actions/improvements/'+item['improvement_id']+'/rollback',{})
    return [r['improvement_id'] for r in previous]


def bounded_lab_context(bridge,profiler):
    """Bound audit replays without changing the production worker settings.

    LabController multiplies db_timeout by six and its execute_timed call
    overrides a session-level statement_timeout. Set the isolated lab context
    to 15 seconds so the actual statement deadline is 90 seconds.
    """
    settings=SimpleNamespace(**vars(bridge.args))
    settings.db_timeout=15.0
    return SimpleNamespace(args=settings,sql_profiler=profiler)


class AuditStatementClient(DatabaseClient):
    """A real psql client that exposes one SQL statement to the collector.

    Keep setup in PGOPTIONS instead of batching SET + SELECT into pg_stat_activity.
    This tests automatic literal capture, not a manually seeded replay SQL.
    Production collection, diagnosis and publication remain unchanged.
    """
    def execute_timed(self,sql,application_name='sysinsight-dream-worker-audit',
                      search_path=None,session_settings=None,startup_settings=None,timeout=None):
        options=dict(startup_settings or {})
        if search_path:options['search_path']=','.join(search_path)
        options.update(session_settings or {})
        effective=min(90.0,float(timeout if timeout is not None else self.timeout))
        if effective<=0:raise ValueError('Audit SQL timeout must be positive')
        options['statement_timeout']=int(effective*1000)
        started=time.perf_counter()
        try:
            result=subprocess.run(self._command(sql,application_name,startup_settings=options),
                cwd='/',text=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                timeout=effective+5,check=False)
        except subprocess.TimeoutExpired:
            return {'status':'failed','duration_ms':(time.perf_counter()-started)*1000,
                    'error':'Audit psql subprocess exceeded statement deadline','returncode':None}
        measured={'status':'completed' if result.returncode==0 else 'failed',
                  'duration_ms':(time.perf_counter()-started)*1000,'returncode':result.returncode}
        if result.returncode:measured['error']=result.stderr.strip()[:2000]
        return measured


def ap_round(number,repeat,report,save):
    idle()
    query=(ROOT.parent/'dream/data/slow_queries/TPC-DS'/str(number)).with_suffix('.sql').read_text()
    db=DatabaseClient('keeninsight','postgres','/var/run/postgresql',5432,'postgres',90)
    store=StateStore(STATE)
    # Reuse the actual service's settings and private child environment.
    # Secrets stay in memory and never enter the audit report or argv.
    pid=http('/healthz')['pid']
    command=[v.decode() for v in (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0') if v]
    environment=dict(v.decode().split('=',1) for v in (Path('/proc')/str(pid)/'environ').read_bytes().split(b'\0') if b'=' in v)
    for name,value in environment.items():
        if any(word in name for word in ('SYSINSIGHT','OPENAI','DREAM')):os.environ[name]=value
    previous_argv=sys.argv
    try:
        sys.argv=command[1:]
        settings=parse_args()
    finally:sys.argv=previous_argv
    bridge=Bridge.__new__(Bridge);bridge.store=store;bridge.args=settings;bridge.db=db
    bridge.output_root=Path(settings.output);bridge.current_incident_dir=None
    entry={'sqlId':'tpcds_q'+str(number),'round':repeat,'startedAt':stamp(),'status':'baseline',
           'triggerMode':'explicit_audit_diagnosis_not_automatic_threshold','realLLMRequired':True}
    report['ap'].append(entry);save()
    # Baseline is always original SQL, not a previously rewritten query.
    run_id='lab-dream-audit-{}-q{}-r{}'.format(report['runTag'],number,repeat)
    entry['runId']=run_id
    store.create_lab_run(run_id,'dream','tpcds_q'+str(number),'Fresh LLM stability audit Q'+str(number),
        {'audit':True,'freshLLM':True,'round':repeat,'manual':True,'sql_id':'tpcds_q'+str(number)})
    store.update_lab_run(run_id,status='running',phase='baseline_execution')
    profiler=SQLProfiler(db,store,bridge.output_root,_canonical_sql,seconds=5)
    bridge.sql_profiler=profiler
    lab=LabController.__new__(LabController)
    lab.db=AuditStatementClient('keeninsight','postgres','/var/run/postgresql',5432,'postgres',90)
    lab.store=store
    lab.bridge=bounded_lab_context(bridge,profiler)
    # This is an explicit internal DREAM benchmark, not a new user business
    # request. Use the same existing internal-worker exclusion as DREAM's
    # own evaluation connections; do not make its canary recursively launch
    # SysInsight and contaminate the next SQL measurement.
    lab._application_prefix=lambda *_:'sysinsight-dream-worker-audit-'
    entry['measurementApplication']='sysinsight-dream-worker-audit'
    entry['actualMeasurementTimeoutSeconds']=90
    entry['measurementClient']='psql_single_statement_startup_options'
    execution,baseline=lab._execute_lab_sql(run_id,'tpcds_q'+str(number),'baseline',query,
        'original SQL fresh baseline',{'statement_timeout':90000})
    profiler.close()
    entry['baselineExecutionId']=execution
    entry['baseline']=baseline;save()
    if baseline.get('status')!='completed':
        store.update_lab_run(run_id,status='failed',phase='baseline_failed',error=baseline.get('error','Baseline failed'))
        entry.update(status='baseline_failed',finishedAt=stamp());save();return entry
    if baseline['duration_ms']<10000:
        store.update_lab_run(run_id,status='completed',phase='below_automatic_threshold',
            result_patch={'baseline':baseline,'baseline_execution_id':execution,'auditOutcome':'below_10s_threshold'})
        entry.update(status='below_automatic_threshold',finishedAt=stamp());save();return entry
    old=rows('SELECT * FROM sql_observations WHERE canonical_sql=? ORDER BY last_seen DESC',(_canonical_sql(query),))
    # Keep prior jobs and evidence intact. A new audit key forces a new actual
    # diagnosis rather than reusing the prior successful job/candidate.
    milliseconds=baseline['duration_ms']
    automatic=[]
    deadline=time.monotonic()+15
    while repeat==1 and time.monotonic()<deadline:
        automatic=rows("""SELECT j.* FROM dream_jobs j JOIN sql_observations o ON o.sql_key=j.sql_key
            WHERE o.canonical_sql=? AND j.requested_at>=? AND j.trigger_source='automatic_completed_sql'
            ORDER BY j.requested_at LIMIT 1""",(_canonical_sql(query),entry['startedAt']))
        if automatic:break
        time.sleep(1)
    if automatic:
        key=automatic[0]['sql_key'];job_id=automatic[0]['job_id']
        entry.update(triggerMode='automatic_completed_sql',sourceCompletionMs=automatic[0]['source_completion_ms'])
        entry['retiredActions']=[]
    else:
        identifier='stability-{}-q{}-r{}'.format(report['runTag'],number,repeat)
        keys=store.upsert_observations([{'database':'keeninsight','username':'postgres','queryid':identifier,
            'query':query,'replay_sql':query,'calls':1,'total_time_ms':milliseconds,'min_time_ms':milliseconds,
            'max_time_ms':milliseconds,'mean_time_ms':milliseconds,'rows':0}],[])
        key=keys[0]
        entry['retiredActions']=retire_matching(key)
        job_id=store.enqueue_job(key,None,cooldown=0,force=True)
    entry['sqlKey']=key
    entry['matchingHistoricalKeys']=[r['sql_key'] for r in old]
    # retire ONLY this audit key's action; other user demo improvements remain.
    if not job_id:raise RuntimeError('A genuine fresh job was not created')
    entry.update(jobId=job_id,status='analysis');save()
    store.update_lab_run(run_id,status='running',phase='dream_analysis',
        result_patch={'job_id':job_id,'sql_key':key,'baseline':baseline,'baseline_execution_id':execution,
                      'trigger_source':'automatic_completed_sql' if automatic else 'manual',
                      'auditPurpose':'fresh real LLM stability'})
    print('AP_START',number,repeat,'baseline_s',milliseconds/1000,'job',job_id,flush=True)
    # The exact existing production worker is invoked, not a reimplementation.
    # It loads real RCRank, calls the configured LLM, checks/executes actions
    # and publishes through the same validation policy.
    result=(wait_job(job_id).get('result')or{}) if automatic else bridge._run_dream_job(job_id)
    job=store.get_job(job_id)
    entry['jobStatus']=job['status'];entry['analysisOutcome']=result.get('status')
    publication=result.get('bridge_publication')or{}
    entry['publication']=publication;entry['model']=result.get('api_model')
    trace=result.get('diagnosis_trace')or{}
    action_trace=result.get('action_trace')or{}
    entry['llmEvidence']={
        'diagnosisResponses':sum(bool(a.get('raw_response')) for a in trace.get('attempts',[])),
        'actionResponseRecorded':bool(action_trace.get('raw_response')),
        'semanticValidation':(result.get('evaluation')or{}).get('semantic_validation'),
    }
    if entry['model'] is None and job['status']=='completed':
        entry['evidenceWarning']='Worker completed without a retained API model identifier'
    directory=Path(report['directory'])/('q{}-round-{}'.format(number,repeat));directory.mkdir()
    (directory/'job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2,default=str))
    store.update_lab_run(run_id,phase='analysis_completed',result_patch={'job':lab._job_summary(job),'job_result':result})
    replays=[]
    if publication.get('status')=='active':
        # A collector-created job can run alongside an explicit exploration.
        # Drain it before timing replays instead of comparing under unequal
        # background evaluation load.
        idle(ignore_run_ids=(run_id,))
        for index in range(2):
            action=bridge.resolve_dream_application(key,query)
            if not action.get('applied') or action.get('improvement_id')!=publication.get('improvement_id'):
                raise RuntimeError('Replay selected an old action rather than this fresh job')
            eid,replay=lab._execute_lab_sql(run_id,'tpcds_q'+str(number),'optimized_'+str(index+1),
                action['query'],'DREAM automatic fresh-job application',action.get('session_settings')or{})
            replays.append({'executionId':eid,**replay,'improvementId':action['improvement_id']})
            if replay.get('status')=='completed':store.record_improvement_hit(action['improvement_id'],'fresh stability audit replay')
        # A warm original is a control against crediting cold-cache warmup as
        # tuning benefit. Original SQL is executed explicitly, never resolved.
        eid,warm=lab._execute_lab_sql(run_id,'tpcds_q'+str(number),'original_control',query,
            'warm original SQL control',{'statement_timeout':90000})
        entry['warmOriginal']={'executionId':eid,**warm}
    entry.update(replays=replays,finishedAt=stamp())
    good=[r['duration_ms'] for r in replays if r.get('status')=='completed']
    if len(good)==2 and entry.get('warmOriginal',{}).get('status')=='completed':
        conservative=min(milliseconds,entry['warmOriginal']['duration_ms'])
        entry['speedup']=conservative/statistics.median(good)
        entry['reductionRatio']=1-statistics.median(good)/conservative
        entry['status']='passed' if entry['speedup']>=2 else 'weak_gain'
    else:entry['status']='no_published_gain'
    store.update_lab_run(run_id,status='completed' if job['status']=='completed' else 'failed',phase=entry['status'],
        result_patch={'audit':entry,'job':lab._job_summary(job),'job_id':job_id,'sql_key':key,
                      'baseline':baseline,'baseline_execution_id':execution,
                      'comparison':{'baseline_ms':milliseconds,'optimized_ms':statistics.median(good) if good else None,
                                    'improvement_ratio':entry.get('reductionRatio')}})
    (directory/'measurement.json').write_text(json.dumps(entry,ensure_ascii=False,indent=2,default=str))
    save()
    print('AP_DONE',number,repeat,entry['status'],'speedup',entry.get('speedup'),'publication',publication.get('status'),flush=True)
    idle()
    return entry


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--mode',choices=['tp','ap'],required=True)
    parser.add_argument('--tp-rounds',type=int,default=3)
    parser.add_argument('--baseline-seconds',type=int,default=30)
    parser.add_argument('--pressure-seconds',type=int,default=120)
    parser.add_argument('--ap-queries',default='31,88,9,4,14,23,24,39,47,64,67,72,77,78,80,95')
    parser.add_argument('--wait-for-tp-audit')
    parser.add_argument('--wait-for-ap-audit',help='TP waits for this AP audit before starting any load')
    parser.add_argument('--shortlist',type=int,default=3)
    parser.add_argument('--resume-empty',action='store_true',help='Reuse an unstarted waiting audit only; never overwrite measurements')
    parser.add_argument('--resume',action='store_true',help='Continue an audit only after its last measurement/worker finished')
    args=parser.parse_args()
    destination=Path(args.output).resolve()
    previous_wait=None
    if args.resume:
        previous_wait=json.loads((destination/'audit.json').read_text())
        if (previous_wait.get('finishedAt')
                or any(r.get('status') in ('baseline','analysis') for r in previous_wait.get('ap',[]))
                or any(r.get('status') in ('starting','running','queued') for r in previous_wait.get('tp',[]))):
            raise ValueError('Cannot resume a finished audit or an unfinished measurement')
    elif args.resume_empty:
        previous_wait=json.loads((destination/'audit.json').read_text())
        if previous_wait.get('ap') or previous_wait.get('tp') or previous_wait.get('finishedAt'):
            raise ValueError('Only an unstarted waiting audit can be resumed')
        if sorted(p.name for p in destination.iterdir())!=['audit.json']:
            raise ValueError('Directory has other measurements; choose a new output')
    else:destination.mkdir(parents=True,exist_ok=False)
    report={'schema':'keeninsight.demo-stability.v1','startedAt':stamp(),'directory':str(destination),
        'runTag':dt.datetime.now().strftime('%Y%m%d%H%M%S'),'mode':args.mode,'tp':[],'ap':[],
        'policy':{'fakeData':False,'forceAlerts':False,'clearAllHistory':False,'tpsCandidateMayBeHistorical':True}}
    if previous_wait:report['resumedWaitingAuditStartedAt']=previous_wait['startedAt']
    if args.resume:
        report=previous_wait
        report.setdefault('resumeTimes',[]).append(stamp())
    def save():(destination/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    save()
    try:
        if args.mode=='tp':run_tp(args,report,save)
        else:
            if args.wait_for_tp_audit:
                print('AP waiting for all TP runs, to prevent mixed-load measurements',flush=True)
                while True:
                    path=Path(args.wait_for_tp_audit)
                    tp=json.loads(path.read_text()) if path.exists() else {}
                    if tp.get('finishedAt'):
                        if tp.get('status')!='completed':raise RuntimeError('TP stopped unsuccessfully; inspect before adding AP load')
                        break
                    time.sleep(10)
            for number in map(int,args.ap_queries.split(',')):
                if not any(r['sqlId']=='tpcds_q'+str(number) and r['round']==1 for r in report['ap']):
                    ap_round(number,1,report,save)
            winners=sorted([r for r in report['ap'] if r.get('status')=='passed' and r['round']==1],
                           key=lambda r:r['speedup'],reverse=True)[:args.shortlist]
            for winner in winners:
                number=int(winner['sqlId'].split('q')[1])
                for repeat in (2,3):
                    if not any(r['sqlId']==winner['sqlId'] and r['round']==repeat for r in report['ap']):
                        ap_round(number,repeat,report,save)
            report['apSummary']=[]
            for sid in dict.fromkeys(r['sqlId'] for r in report['ap']):
                measured=[r for r in report['ap'] if r['sqlId']==sid]
                speedups=[r['speedup'] for r in measured if isinstance(r.get('speedup'),(int,float))]
                report['apSummary'].append({'sqlId':sid,'attempts':len(measured),'statuses':[r['status'] for r in measured],
                    'speedups':[r.get('speedup') for r in measured],
                    'stable':len(measured)>=3 and all(r.get('status')=='passed' for r in measured),
                    'predictableSpeedupWithin25Percent':len(speedups)>=3 and min(speedups)>0
                        and max(speedups)/min(speedups)<=1.25,
                    'freshJobIds':[r.get('jobId') for r in measured]})
        report['status']='completed'
    except BaseException as exc:
        report.update(status='failed',error='{}: {}'.format(type(exc).__name__,exc));raise
    finally:
        report['finishedAt']=stamp();save()


if __name__=='__main__':main()
