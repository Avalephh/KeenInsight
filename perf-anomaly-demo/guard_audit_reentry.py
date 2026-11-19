#!/usr/bin/env python3
"""Stop only test-derived DREAM workers; never change product scheduling.

This is a benchmark cleanup guard, NOT a fix or proof of a healthy automatic
product path. It preserves the job records, the parent/rewrite identity match,
and every terminated worker. Original audit jobs must be allowed to finish.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import sqlite3
import time

from sysinsight_dream_bridge import DatabaseClient, _canonical_sql


def derived_jobs(jobs, roots):
    known=set(roots)
    edges=[]
    while True:
        canonical={}
        for job in jobs:
            if job['job_id'] not in known:continue
            result=json.loads(job.get('result_json')or'{}')
            rewrite=result.get('rewrite_sql')or(result.get('evaluation')or{}).get('rewrite_sql')
            if rewrite:canonical[_canonical_sql(rewrite)]=job['job_id']
        additions=set()
        for job in jobs:
            if job['job_id'] not in known and job['canonical_sql'] in canonical:
                additions.add(job['job_id'])
                edges.append({'parentJobId':canonical[job['canonical_sql']],
                              'derivedJobId':job['job_id'],'sqlKey':job['sql_key']})
        if not additions:break
        known.update(additions)
    return known-set(roots),edges


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--audit',required=True)
    p.add_argument('--state',required=True)
    args=p.parse_args()
    audit_path=Path(args.audit).resolve()
    output=audit_path.parent/'internal_reentry_guard.json'
    report={'kind':'test_cleanup_not_product_fix','startedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
            'events':[],'edges':[]}
    killed=set()
    db=DatabaseClient('keeninsight','postgres','/var/run/postgresql',5432,'postgres',30)
    while True:
        try:audit=json.loads(audit_path.read_text())
        except (OSError,ValueError):
            time.sleep(2);continue
        if audit.get('finishedAt'):break
        roots={r['jobId'] for r in audit.get('ap',[]) if r.get('jobId')}
        with sqlite3.connect(Path(args.state).resolve().as_uri()+'?mode=ro',uri=True) as conn:
            conn.row_factory=sqlite3.Row
            jobs=[dict(r) for r in conn.execute(
                """SELECT j.job_id,j.sql_key,j.status,j.result_json,o.canonical_sql
                   FROM dream_jobs j JOIN sql_observations o ON o.sql_key=j.sql_key
                   WHERE j.requested_at>=?""",(audit['startedAt'],))]
        descendants,edges=derived_jobs(jobs,roots)
        report['edges']=edges
        targets={j['sql_key']:j for j in jobs
                 if j['job_id'] in descendants and j['status'] in ('running','queued')}
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name) in killed:continue
            try:argv=[v.decode() for v in (proc/'cmdline').read_bytes().split(b'\0') if v]
            except (OSError,UnicodeError):continue
            if not argv or not argv[0].endswith('/bin/python') or '--query-id' not in argv:continue
            key=argv[argv.index('--query-id')+1]
            if key not in targets:continue
            # Only a worker whose SQL is an exact canonical match to a
            # completed audit job's retained rewrite can be stopped.
            target=targets[key]
            backends=db._rows("""SELECT pid,query FROM pg_stat_activity
                WHERE backend_type='client backend' AND state='active'
                  AND application_name='sysinsight-dream-worker'""")
            pids=[int(r['pid']) for r in backends
                  if _canonical_sql(str(r['query']))==target['canonical_sql']]
            try:os.kill(int(proc.name),signal.SIGTERM)
            except ProcessLookupError:continue
            killed.add(int(proc.name))
            event={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'pid':int(proc.name),
                   'jobId':target['job_id'],'sqlKey':key,'canceledBackendPids':pids,
                   'reason':'Internal audit rewrite was rescheduled as a business SQL'}
            report['events'].append(event)
            print('TEST_REENTRY_CLEANUP',json.dumps(event),flush=True)
            if pids:
                db.execute("SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name='sysinsight-dream-worker' AND pid IN ("+
                    ','.join(map(str,pids))+')')
        report['updatedAt']=dt.datetime.now(dt.timezone.utc).isoformat()
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        time.sleep(2)
    report['finishedAt']=dt.datetime.now(dt.timezone.utc).isoformat()
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
