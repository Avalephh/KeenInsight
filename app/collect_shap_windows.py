#!/usr/bin/env python3
"""Collect real, time-aligned foreground TPS/perf pairs for the six demo cases.

No model training, parameter tuning, global PG changes, synthetic data or LLM
calls are performed by this script. The existing online alert service remains
active. TPC-C transactions use the existing rollback-protected scripts.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from lab_controller import LabController, _case_catalog, TPCC_CASE_ROOT
from sysinsight_dream_bridge import DatabaseClient
from sysinsight_live_chain import _capture_perf, _source_environment
from sysinsight_shap import sha


def aligned_tps(path,start,end):
    """Use only complete pgbench progress intervals inside the perf window."""
    rows=[];previous=0.0
    for match in re.finditer(r'progress:\s*([\d.]+)\s+s,\s*([\d.]+)\s+tps',Path(path).read_text(errors='replace')):
        elapsed,tps=map(float,match.groups())
        if previous>=start and elapsed<=end:
            rows.append({'startSeconds':previous,'endSeconds':elapsed,'tps':tps})
        previous=elapsed
    seconds=sum(r['endSeconds']-r['startSeconds'] for r in rows)
    if seconds<4:raise ValueError('Insufficient fully overlapping progress intervals')
    return {'tps':sum((r['endSeconds']-r['startSeconds'])*r['tps'] for r in rows)/seconds,
            'coveredSeconds':seconds,'intervals':rows,'windowSeconds':[start,end]}


def sampled_window(script,control_origin):
    """Use actual monotonic perf event timestamps, not postprocessing duration.

    A 0.5 s boundary guard covers pgbench startup/connection delay. Full progress
    intervals only are accepted later. This does not align off-CPU wait time.
    """
    timestamps=[]
    with Path(script).open(errors='replace') as handle:
        for line in handle:
            match=re.match(r'^\S+\s+\d+(?:/\d+)?\s+(?:\[\d+\]\s+)?([\d]+\.[\d]+):\s+',line)
            if match:timestamps.append(float(match[1]))
    if not timestamps:raise ValueError('No actual monotonic perf timestamps')
    return (min(timestamps)-control_origin+.5,max(timestamps)-control_origin-.5)


def collect(args):
    cases=sorted((r for r in _case_catalog() if r['focus']),key=lambda r:r['focus_order'])
    destination=Path(args.output).resolve();destination.mkdir(parents=True,exist_ok=True)
    db=DatabaseClient('keeninsight','postgres','/var/run/postgresql',5432,'postgres',5)
    lab=LabController.__new__(LabController)
    lab.bridge=SimpleNamespace(args=SimpleNamespace(run_as='postgres',host='/var/run/postgresql',port=5432,db_user='postgres',db='keeninsight'))
    report={'schemaVersion':'keeninsight.shap-window-dataset.v1','startedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
            'scope':'Six existing TP cases; paired real foreground TPS windows; no tuning by collector',
            'rounds':args.rounds,'rows':[],'failures':[]}
    def save():
        (destination/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    save()
    for repeat in range(args.rounds):
        # Rotate order to reduce a fixed scenario/order confound. Batches remain grouped in evaluation.
        ordered=cases[repeat%len(cases):]+cases[:repeat%len(cases)]
        for case in ordered:
            rowdir=destination/('round-{}-{}'.format(repeat+1,case['scenario_id']));rowdir.mkdir(exist_ok=False)
            row={'scenarioId':case['scenario_id'],'batch':repeat+1,'startedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
                 'normalClients':2,'pressureClients':case['clients'],'normalTargetTps':1600,
                 'pressureTargetTps':min(4000,case['pressure_target_tps']),'directory':str(rowdir)}
            print('START round={} scenario={}'.format(repeat+1,case['scenario_id']),flush=True)
            processes=[]
            try:
                with tempfile.TemporaryDirectory(prefix='keen-shap-sql-') as tmp:
                    staging=Path(tmp);staging.chmod(0o755)
                    normal=lab._stage_sql(TPCC_CASE_ROOT/case['normal_sql'],staging)
                    pressure=lab._stage_sql(TPCC_CASE_ROOT/case['sql'],staging)
                    for phase in ('baseline','pressure'):
                        phase_dir=rowdir/phase;phase_dir.mkdir()
                        marker='lab-shap-{}-{}-'.format(repeat+1,case['scenario_id'])[:48]
                        control_origin=time.monotonic()
                        control=lab._start_pgbench(normal,phase_dir,marker+'control',2,18,'control',rate=1600)
                        processes=[control]
                        if phase=='pressure':processes.append(lab._start_pgbench(pressure,phase_dir,marker+'external',case['clients'],18,'external',rate=row['pressureTargetTps']))
                        row.setdefault('origins',{})[phase]={'controlLaunchMonotonic':control_origin,'workerStartedAt':control['started_at']}
                        time.sleep(4)
                        if phase=='pressure':
                            active=db.collect_active(100,include_recent_idle=True)
                            pids=sorted({int(r['pid']) for r in active if str(r.get('application_name','')).startswith(marker)})
                            if len(pids)<2:raise ValueError('No live foreground/pressure backend pair')
                            capture=_capture_perf(phase_dir,pids,8,49,'',_source_environment())
                            row['capture']=capture
                            if capture.get('postprocess',{}).get('status')!='sysinsight_counts_generated':raise ValueError('No complete measured counts file')
                            window=sampled_window(capture['postprocess']['script_path'],control_origin)
                        else:
                            window=(5.0,12.0)
                        for item in processes:
                            item['process'].wait(timeout=22)
                            item['log_handle'].close()
                            if item['process'].returncode:raise RuntimeError('pgbench {} failed'.format(item['role']))
                        value=aligned_tps(control['log_path'],*window)
                        row[phase]=value
                        row[phase]['logSha256']=sha(control['log_path'])
                        row[phase]['logPath']=str(control['log_path'])
                        row[phase]['workerApplication']=control['app_name']
                        processes=[]
                    row['target']=row['pressure']['tps']/row['baseline']['tps']
                    row['countsPath']=row['capture']['postprocess']['counts_path']
                    row['countsSha256']=sha(row['countsPath'])
                    row['status']='completed'
                    report['rows'].append(row)
                    (rowdir/'measurement.json').write_text(json.dumps(row,ensure_ascii=False,indent=2))
                    print('DONE round={} scenario={} baseline={:.1f} pressure={:.1f} retained={:.1%}'.format(repeat+1,case['scenario_id'],row['baseline']['tps'],row['pressure']['tps'],row['target']),flush=True)
            except Exception as exc:
                row.update(status='failed',error='{}: {}'.format(type(exc).__name__,exc));report['failures'].append(row)
                print('FAILED',row['error'],flush=True)
            finally:
                for item in processes:
                    if item['process'].poll() is None:
                        os.killpg(item['process'].pid,signal.SIGTERM)
                        item['process'].wait(timeout=4)
                    item['log_handle'].close()
                save()
    report['finishedAt']=dt.datetime.now(dt.timezone.utc).isoformat();save()
    print('DATASET',str(destination/'audit.json'),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--rounds',type=int,default=3);p.add_argument('--output',required=True)
    args=p.parse_args()
    if not 1<=args.rounds<=4:p.error('rounds must be between 1 and 4')
    collect(args)
