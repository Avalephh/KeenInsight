#!/usr/bin/env python3
"""Real SQL/LLM E2E audit. Never forces alerts or manufactures gain records."""
import argparse
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parent))
from sysinsight_dream_bridge import Bridge, DatabaseClient, StateStore, _canonical_sql

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parent

def stamp():return dt.datetime.now(dt.timezone.utc).isoformat()

def request(path,body=None):
    req=Request('http://127.0.0.1:9108'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=30) as response:return json.load(response)

def read_rows(query,params=()):
    with sqlite3.connect(str(REPO/'monitoring/data/sysinsight_dream_bridge.sqlite3'),timeout=10) as conn:
        conn.row_factory=sqlite3.Row
        return [dict(row) for row in conn.execute(query,params)]

def run(args):
    tp=Path(args.tp_audit)
    print('Waiting for the six TP cases before running AP queries',flush=True)
    while True:
        data=json.loads(tp.read_text()) if tp.exists() else {}
        if data.get('finishedAt') and data.get('total')==6:break
        time.sleep(5)
    while read_rows("SELECT incident_id FROM incidents WHERE started_at>=? AND status='running'",(data['startedAt'],)):
        time.sleep(5)
    output=tp.parent/'dream_audit.json'
    report={'startedAt':stamp(),'realLLM':True,'offline':False,'tpAudit':str(tp),'cases':[]}
    def save():output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    save()
    Bridge._sync_dream_runtime('/tmp/dream-runtime-20260914')
    db=DatabaseClient('keeninsight','postgres','/var/run/postgresql',5432,'postgres',180)
    store=StateStore(REPO/'monitoring/data/sysinsight_dream_bridge.sqlite3')
    bridge=Bridge.__new__(Bridge);bridge.store=store;bridge.args=SimpleNamespace()

    def sql(number):return (REPO/'dream/data/slow_queries/TPC-DS'/f'{number}.sql').read_text()
    def jobs_for(query):
        return read_rows('SELECT j.* FROM dream_jobs j JOIN sql_observations o ON j.sql_key=o.sql_key WHERE o.canonical_sql=? ORDER BY j.requested_at DESC',(_canonical_sql(query),))
    def external_sql(query,app):
        # Submit a standalone business statement. Connection startup options
        # must not contaminate pg_stat_activity's replayable SQL sample.
        started=time.perf_counter()
        completed=subprocess.run(db._command(query,app,startup_settings={'search_path':'tpcds,public','statement_timeout':180000}),
            cwd='/',text=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=185,check=False)
        return {'status':'completed' if completed.returncode==0 else 'failed',
                'duration_ms':(time.perf_counter()-started)*1000,'error':completed.stderr[-2000:]}
    def wait_job(job_id):
        deadline=time.monotonic()+1200
        while time.monotonic()<deadline:
            item=request('/api/v1/jobs/'+job_id)
            if item['status'] not in {'queued','running'}:return item
            time.sleep(5)
        raise RuntimeError('actual DREAM job exceeded bounded wait: '+job_id)
    def settle():
        deadline=time.monotonic()+1800
        while time.monotonic()<deadline:
            if not read_rows("SELECT incident_id FROM incidents WHERE started_at>=? AND status='running'",(report['startedAt'],)):return
            time.sleep(5)
        raise RuntimeError('incident queue did not drain; no more AP load launched')
    def replay(case,query,key):
        action=bridge.resolve_dream_application(key,query)
        case['resolvedApplication']=action
        if not action.get('applied'):return
        case['replayStartedAt']=stamp()
        case['replay']=db.execute_timed(action['query'],application_name='keeninsight-e2e-managed-replay',
            search_path=['tpcds','public'],session_settings=action.get('session_settings') or {},timeout=180)
        case['replayFinishedAt']=stamp()
        if case['replay']['status']=='completed' and action.get('apply_scope')=='bridge_managed':
            store.record_improvement_hit(action['improvement_id'],'real E2E bridge-managed SQL replay')
        case['portal']=request('/api/v1/keeninsight/sql/'+key)

    # The real completed-statement collector, not a manual enqueue, must
    # schedule a new long Q31 fingerprint. Observe that no job starts early.
    case={'sqlId':'tpcds_q31','mode':'automatic_completed_long_sql','startedAt':stamp(),
          'variant':'Original TPC-DS Q31 with OFFSET 0 pagination; a fresh fingerprint without changing rows or adding artificial delay'}
    report['cases'].append(case);save()
    query=sql(31).rstrip().rstrip(';')+'\nOFFSET 0;'
    case['query']=query
    prior={row['job_id'] for row in jobs_for(query)}
    case['preexistingJobs']=sorted(prior)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(external_sql,query,'keeninsight-e2e-q31-original')
        case['earlyJobs']=[]
        while not future.done():
            found=[row['job_id'] for row in jobs_for(query) if row['job_id'] not in prior]
            if found and not future.done():
                case['earlyJobs']=found
                break
            time.sleep(1)
        case['baseline']=future.result()
    case['originalFinishedAt']=stamp();save()
    if case['baseline']['status']!='completed':raise RuntimeError('Q31 actual baseline failed')
    deadline=time.monotonic()+90
    new=[]
    while time.monotonic()<deadline:
        new=[row for row in jobs_for(query) if row['job_id'] not in prior]
        if new:break
        time.sleep(2)
    if not new:
        case['automaticTriggerPassed']=False
        case['reason']='No new automatic job; retained job gate or threshold may have prevented scheduling'
    else:
        case['jobId']=new[0]['job_id'];case['automaticTriggerPassed']=not case['earlyJobs'] and case['baseline']['duration_ms']>=10000
        print('AUTO Q31',case['jobId'],'baseline_ms',case['baseline']['duration_ms'],'earlyJobs',case['earlyJobs'],flush=True)
        case['job']=wait_job(case['jobId']);save();settle()
        replay(case,query,new[0]['sql_key'])
    save();settle()

    # Verify a real short query does not create an automatic DREAM job.
    case={'sqlId':'tpcds_q8','mode':'automatic_short_query_gate','startedAt':stamp()}
    report['cases'].append(case);query=sql(8);prior={row['job_id'] for row in jobs_for(query)}
    case['execution']=external_sql(query,'keeninsight-e2e-q8-original')
    time.sleep(12)
    case['newJobs']=[row['job_id'] for row in jobs_for(query) if row['job_id'] not in prior]
    case['shortGatePassed']=case['execution']['status']=='completed' and case['execution']['duration_ms']<10000 and not case['newJobs']
    print('SHORT Q8',case['execution']['duration_ms'],'newJobs',case['newJobs'],flush=True);save();settle()

    # Manually requested diagnosis is allowed below the automatic threshold.
    # Refresh the baseline from an actual execution before explicit retries.
    for number in (44,88):
        case={'sqlId':f'tpcds_q{number}','mode':'manual_fresh_baseline_real_llm_retry','startedAt':stamp()}
        report['cases'].append(case);query=sql(number)
        row=read_rows('SELECT * FROM sql_observations WHERE queryid=?',(f'lab-tpcds_q{number}',))[0]
        key=row['sql_key'];prior_job=read_rows('SELECT job_id FROM dream_jobs WHERE sql_key=? ORDER BY requested_at DESC LIMIT 1',(key,))[0]['job_id']
        active=read_rows("SELECT improvement_id FROM improvements WHERE sql_key=? AND status='active'",(key,))
        case['previousActiveIds']=[item['improvement_id'] for item in active]
        case['baseline']=db.execute_timed(query,application_name=f'keeninsight-e2e-q{number}-original',search_path=['tpcds','public'],timeout=180)
        case['originalFinishedAt']=stamp()
        if case['baseline']['status']!='completed':raise RuntimeError('real baseline failed '+case['sqlId'])
        milliseconds=case['baseline']['duration_ms']
        store.upsert_observations([{'database':'keeninsight','username':'postgres','queryid':f'lab-tpcds_q{number}',
            'query':query,'replay_sql':query,'calls':1,'total_time_ms':milliseconds,'min_time_ms':milliseconds,
            'max_time_ms':milliseconds,'mean_time_ms':milliseconds}],[])
        for item in active:request('/api/v1/actions/improvements/'+item['improvement_id']+'/rollback',{})
        try:
            result=request('/api/v1/actions/jobs/'+prior_job+'/retry',{});case['jobId']=result['job_id'];save()
            print('MANUAL',case['sqlId'],case['jobId'],'baseline_ms',milliseconds,flush=True)
            case['job']=wait_job(case['jobId']);save();settle()
            replay(case,query,key)
            if not case.get('resolvedApplication',{}).get('applied'):
                for item in active:request('/api/v1/actions/improvements/'+item['improvement_id']+'/activate',{})
                case['previousActionRestored']=bool(active)
        except BaseException:
            for item in active:
                try:request('/api/v1/actions/improvements/'+item['improvement_id']+'/activate',{})
                except Exception:pass
            raise
        case['portal']=request('/api/v1/keeninsight/sql/'+key)
        save();settle()
    report['finishedAt']=stamp();save()
    print('DREAM E2E FINISHED',output,flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tp-audit',required=True)
    run(parser.parse_args())
