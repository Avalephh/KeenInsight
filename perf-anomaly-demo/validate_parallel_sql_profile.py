#!/usr/bin/env python3
"""Real PG12 parallel query with an identical, unrelated concurrent query.

Session-only planner settings. Saves actual lab execution/profile evidence;
does not invoke DREAM, tune parameters globally or modify table contents.
"""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from lab_controller import LabController
from sql_profiling import SQLProfiler
from sysinsight_dream_bridge import DatabaseClient, StateStore, _canonical_sql

ROOT=Path(__file__).resolve().parents[1]


def main():
    db=DatabaseClient('keeninsight','postgres','/var/run/postgresql',5432,'postgres',25)
    store=StateStore(ROOT/'monitoring/data/sysinsight_dream_bridge.sqlite3')
    output=ROOT/'monitoring/data/sysinsight_dream_bridge'
    profiler=SQLProfiler(db,store,output,_canonical_sql,seconds=8)
    lab=LabController.__new__(LabController);lab.db=db;lab.store=store
    lab.bridge=SimpleNamespace(sql_profiler=profiler,args=SimpleNamespace(db_schema='tpcds',db_timeout=15))
    stamp=dt.datetime.now().strftime('%Y%m%d%H%M%S')
    run='lab-parallel-isolation-'+stamp;app='lab-parallel-unrelated-'+stamp
    query='SELECT sum(sqrt(ss_quantity::float8)+sqrt(abs(ss_net_profit)::float8)+sqrt(ss_sales_price::float8)) FROM tpcds.store_sales'
    settings={'max_parallel_workers_per_gather':2,'min_parallel_table_scan_size':0,
              'parallel_setup_cost':0,'parallel_tuple_cost':0,'statement_timeout':20000}
    store.create_lab_run(run,'dream','sql_parallel_isolation','Parallel CPU attribution validation',{'sessionSettings':settings})
    stop=threading.Event();observed=set()
    def observe():
        while not stop.wait(.2):
            try:
                rows=db._rows("SELECT pid,backend_type FROM pg_stat_activity WHERE application_name='{}' AND state='active'".format(app),'lab-parallel-isolation-observer')
                observed.update(int(r['pid']) for r in rows)
            except Exception:pass
    observer=threading.Thread(target=observe,daemon=True);observer.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            other=pool.submit(db.execute_timed,query,application_name=app,session_settings=settings,search_path=['tpcds','public'],timeout=25)
            execution,result=lab._execute_lab_sql(run,'sql_parallel_isolation','baseline',query,'real parallel-query isolation validation',settings)
            unrelated=other.result(timeout=30)
        stop.set();observer.join(timeout=3);profiler.close()
        with store._connection() as conn:
            row=conn.execute('SELECT payload_json FROM sql_profiles WHERE execution_id=?',(execution,)).fetchone()
        profile=json.loads(row[0]) if row else {}
        pids={p['pid'] for p in profile.get('participants',[])}
        evidence={'executionId':execution,'queryResult':result,'unrelatedQueryResult':unrelated,
                  'unrelatedActivePids':sorted(observed),'profile':profile,'passed':False}
        assert result.get('status')=='completed' and unrelated.get('status')=='completed','Actual SQL failed'
        assert observed,'No concurrent unrelated query was observed'
        assert not pids.intersection(observed),'Unrelated query samples contaminated the managed profile'
        assert profile.get('sampledWorkerCount',0)>0,'No verified parallel worker CPU sample'
        assert profile['sampleCount']==sum(p['sampleCount'] for p in profile['participants'])
        assert profile['sampleCount']==profile['flameGraph']['value']
        evidence['passed']=True
        directory=Path(profile['artifactDirectory']);(directory/'parallel_isolation_validation.json').write_text(json.dumps(evidence,indent=2))
        store.update_lab_run(run,status='completed',phase='profile_validated',result_patch={'executionId':execution,'profileId':profile['profileId'],'isolationPassed':True})
        print(json.dumps({'executionId':execution,'profileId':profile['profileId'],'sampleCount':profile['sampleCount'],
                          'participants':profile['participants'],'unrelatedActivePids':sorted(observed),'overlap':sorted(pids.intersection(observed)),
                          'evidence':str(directory/'parallel_isolation_validation.json')}))
    except Exception as exc:
        store.update_lab_run(run,status='failed',phase='validation_failed',error=str(exc));raise
    finally:
        stop.set();observer.join(timeout=3);profiler.close()


if __name__=='__main__':main()
