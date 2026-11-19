"""Bounded, SQL-identity-scoped CPU sampling. Never execute/replay a query.

Sampling is opportunistic, not on the SQL's critical execution path. A fresh
managed connection is identified by its unique application name; external
connections additionally require an unchanged backend_start and query_start.
Only samples inside a positively revalidated identity window are published.
PostgreSQL 12 workers are associated using their PostgreSQL-generated process
title plus database query identity and OS process birth identity. No global
perf recording or guessed association based on SQL text alone is used.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

LOG = logging.getLogger(__name__)
HEADER = re.compile(r"^\s*(\d+)\s+(\d+\.\d+):\s+.*(?:cpu-clock|cycles)")
FRAME = re.compile(r"^\s+[0-9a-fA-F]+\s+(.+?)\s+\(([^)]*)\)\s*$")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(sql):
    return hashlib.sha256(str(sql).encode()).hexdigest()


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sql_profiles (
            profile_id TEXT PRIMARY KEY, execution_id TEXT, sql_sha256 TEXT NOT NULL,
            database_name TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sql_profiles_execution ON sql_profiles(execution_id,started_at);
        CREATE INDEX IF NOT EXISTS sql_profiles_fingerprint ON sql_profiles(database_name,sql_sha256,started_at);
    """)


def parse_script(lines, pid, start, end, windows=None, labels=None):
    """Count actual sampled events, not perf period/cycle weights as samples."""
    stacks, frames, valid = {}, [], False
    seen, discarded, event_pid = 0, 0, None
    per_pid = {}

    def flush():
        nonlocal frames, seen, discarded
        if valid and frames:
            stack = tuple(reversed(frames[:128]))
            if labels:
                stack = (labels[event_pid],) + stack
            stacks[stack] = stacks.get(stack, 0) + 1
            seen += 1
            per_pid[str(event_pid)] = per_pid.get(str(event_pid), 0) + 1
        elif frames:
            discarded += 1
        frames = []

    for line in lines:
        header = HEADER.match(line)
        if header:
            flush()
            event_pid, timestamp = int(header[1]), float(header[2])
            bounds = (windows or {int(pid): (start, end)}).get(event_pid)
            valid = bool(bounds and bounds[0] <= timestamp <= bounds[1])
        elif not line.strip():
            flush()
            valid = False
        else:
            frame = FRAME.match(line)
            if frame:
                name = re.sub(r"\+0x[0-9a-f]+$", "", frame[1])[:250]
                if name in {"[unknown]", "unknown"}:
                    name = "[unknown @ " + Path(frame[2]).name[:80] + "]"
                frames.append(name)
    flush()
    return stacks, {"sampleCount": seen, "discardedEvents": discarded,
                    "uniqueStacks": len(stacks), "samplesByPid": per_pid}


def stack_tree(stacks, max_nodes=2500):
    """Mass-conserving trie; summarize small stacks instead of overfull bars."""
    root = {"name": "SQL CPU samples", "value": 0, "_children": {}}
    nodes = 1
    for stack, count in sorted(stacks.items(), key=lambda item: (-item[1], item[0])):
        root["value"] += count
        if nodes + len(stack) >= max_nodes:
            children = root['_children']
            if '[other sampled stacks]' not in children:
                children['[other sampled stacks]'] = {'name':'[other sampled stacks]','value':0,'_children':{}}
                nodes += 1
            children['[other sampled stacks]']['value'] += count
            continue
        node = root
        for frame in stack:
            children = node["_children"]
            if frame not in children:
                name = frame
                if name not in children:
                    children[name] = {"name": name, "value": 0, "_children": {}}
                    nodes += 1
                child = children[name]
                child["value"] += count
                if name != frame:
                    break
            else:
                child = children[frame]
                child["value"] += count
            node = child

    def finish(node):
        children = node.pop("_children")
        node["children"] = [finish(child) for child in sorted(children.values(), key=lambda x: (-x["value"], x["name"]))]
        return node
    return finish(root)


class SQLProfiler:
    def __init__(self, db, store, output_root, canonical, enabled=True, frequency=49, seconds=10):
        self.db, self.store, self.canonical = db, store, canonical
        # A slow database must not keep a perf recorder alive behind a 30 s
        # normal query timeout. Probes have their own short psql deadline.
        self._probe = copy.copy(db)
        if hasattr(self._probe, 'timeout'):
            self._probe.timeout = min(2.0, max(.25, float(self._probe.timeout)))
        self.root = Path(output_root) / "sql_profiles"
        self.enabled = bool(enabled)
        self.frequency = max(19, min(int(frequency), 99))
        self.seconds = max(2.0, min(float(seconds), 15.0))
        self._slot = threading.Lock()
        self._lock = threading.Lock()
        self._tasks = {}
        self._seen = {}
        self._closed = threading.Event()
        self._paused = threading.Event()

    def _snapshot(self, pid=None, application=None):
        # Values are internal and always escaped; callers cannot submit paths/PIDs via HTTP.
        where = "pid = {}".format(int(pid)) if pid else "application_name = '{}'".format(str(application).replace("'", "''"))
        rows = self._probe._rows("""SELECT pid, backend_start::text AS backend_start,
            query_start::text AS query_start, state, query, application_name,
            extract(epoch FROM (clock_timestamp()-query_start))*1000 AS duration_ms
            FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend'
            AND {}""".format(where), "keeninsight-sql-profiler")
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def same_query(first, current):
        return bool(current and current.get("state") == "active" and
                    all(str(first.get(key)) == str(current.get(key)) for key in ("pid", "backend_start", "query_start", "query")))

    @staticmethod
    def process_identity(pid):
        """Read the kernel process birth token, executable and PG-generated title."""
        base = Path('/proc') / str(int(pid))
        try:
            stat = (base/'stat').read_text()
            # comm may contain spaces/parentheses; fields after final ')' begin at state.
            fields = stat[stat.rfind(')')+2:].split()
            return {'pid': int(pid), 'parentPid': int(fields[1]), 'startTicks': fields[19],
                    'uid': base.stat().st_uid, 'executable': os.readlink(str(base/'exe')),
                    'title': (base/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()}
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def verified_worker(leader, leader_os, worker, worker_os):
        if not leader_os or not worker_os or worker.get('backend_type') != 'parallel worker':
            return False
        match = re.search(r'(?:^|: )parallel worker for PID (\d+)(?:\s|$)', worker_os.get('title',''))
        return bool(match and int(match[1]) == int(leader['pid']) and
            worker.get('state') == 'active' and
            all(worker.get(k) == leader.get(k) for k in ('query', 'query_start', 'datname')) and
            all(worker_os.get(k) == leader_os.get(k) for k in ('uid', 'executable', 'parentPid')) and
            worker.get('backend_start') and worker_os.get('startTicks'))

    def _group_snapshot(self, first):
        rows = self._probe._rows("""SELECT pid,backend_type,datname,backend_start::text AS backend_start,
            query_start::text AS query_start,state,query,application_name FROM pg_stat_activity
            WHERE datname=current_database() AND (pid={} OR backend_type='parallel worker')""".format(int(first['pid'])),
            'keeninsight-sql-profiler')
        leader = next((row for row in rows if int(row['pid']) == int(first['pid'])), None)
        if not self.same_query(first, leader):
            return {}
        leader_os = self.process_identity(first['pid'])
        if not leader_os:
            # A remote server or unreadable /proc is not verified local identity.
            return {}
        group = {int(first['pid']): {**leader, '_os': leader_os, 'role': 'leader'}}
        omitted=[]
        for worker in rows:
            worker_os = self.process_identity(worker['pid']) if worker.get('backend_type') == 'parallel worker' else None
            if self.verified_worker(leader, leader_os, worker, worker_os):
                if len(group) < 9:
                    group[int(worker['pid'])] = {**worker, '_os': worker_os, 'role': 'parallel worker'}
                else:
                    omitted.append(int(worker['pid']))
        group[int(first['pid'])]['_omittedWorkerPids']=omitted
        return group

    @staticmethod
    def same_participant(first, current):
        return (SQLProfiler.same_query(first, current) and
                bool((first.get('_os') or {}).get('startTicks')) and
                (first.get('_os') or {}).get('startTicks') == (current.get('_os') or {}).get('startTicks'))

    def _capture_group(self, first, directory, perf, stop, record):
        """One recorder at a time; rotate bounded segments when the worker set changes."""
        began = time.monotonic()
        deadline = began + self.seconds
        group = self._group_snapshot(first)
        if not group:
            return {}, {'status':'identity_unavailable','reason':'SQL ended or local process identity could not be verified before group capture'}
        all_stacks, participants, segments = {}, {}, []
        totals = {'sampleCount':0,'discardedEvents':0,'samplesByPid':{}}
        end_reason = 'capture_limit'
        artifact_bytes = 0
        for index in range(8):
            if artifact_bytes>=16*1024*1024:
                end_reason='artifact_size_limit';break
            if not group or time.monotonic() >= deadline or stop.is_set() or self._closed.is_set() or self._paused.is_set():
                break
            end_reason = 'capture_limit'
            pids = sorted(group)
            for pid, item in group.items():
                participants.setdefault(pid, {'pid':pid,'role':item['role'],'backendStart':item['backend_start'],
                    'queryStart':item['query_start'],'processStartTicks':(item.get('_os') or {}).get('startTicks'),
                    'association':'query leader' if item['role']=='leader' else 'PG process-title leader PID + query_start + SQL + database + OS executable/parent/uid/birth token'})
            data = directory/('cpu-{}.perf.data'.format(index))
            command = [perf,'record','-e','cpu-clock','-F',str(self.frequency),'--clockid','mono',
                       '--call-graph','fp','--no-inherit','-m','64','-p',','.join(map(str,pids)),'-o',str(data)]
            start = time.monotonic()
            windows = {pid:[start,start] for pid in pids}
            next_group = group
            record.setdefault('commands',[]).append(command)
            with (directory/('perf-{}.log'.format(index))).open('w') as log:
                proc = subprocess.Popen(command,cwd='/',stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                watchdog = threading.Timer(max(.01,deadline-time.monotonic()),self._stop_process,args=(proc,))
                watchdog.daemon=True;watchdog.start()
                try:
                    while time.monotonic() < deadline and not self._closed.is_set() and not self._paused.is_set():
                        if stop.wait(.3):
                            end_reason='managed_execution_finished';next_group={};break
                        checked=time.monotonic()
                        current=self._group_snapshot(first)
                        if not current or not self.same_participant(group[int(first['pid'])],current.get(int(first['pid']))):
                            end_reason='query_ended_or_changed';next_group={};break
                        for pid in pids:
                            if self.same_participant(group[pid],current.get(pid)):
                                windows[pid][1]=min(checked,deadline)
                        next_group=current
                        if set(current)!=set(pids) or any(not self.same_participant(group[pid],current.get(pid)) for pid in pids):
                            end_reason='worker_set_changed';break
                        if proc.poll() is not None:
                            end_reason='recorder_exited';next_group={};break
                        if data.exists() and artifact_bytes+data.stat().st_size>16*1024*1024:
                            end_reason='artifact_size_limit';next_group={};break
                finally:
                    watchdog.cancel();self._stop_process(proc)
            if self._closed.is_set() or self._paused.is_set():
                end_reason='sampler_closed_or_paused';next_group={}
            segment={'pids':pids,'windows':{str(k):v for k,v in windows.items()},'artifact':str(data),
                     'omittedWorkerPids':group[int(first['pid'])].get('_omittedWorkerPids',[])}
            if data.is_file() and data.stat().st_size:
                artifact_bytes += data.stat().st_size
                script=directory/('cpu-{}.perf.script'.format(index))
                with script.open('w') as handle:
                    result=subprocess.run([perf,'script','-i',str(data),'-F','pid,time,event,ip,sym,dso'],cwd='/',text=True,stdout=handle,stderr=subprocess.PIPE,timeout=8)
                if result.returncode==0:
                    labels={pid:'{} PID {}'.format(group[pid]['role'],pid) for pid in pids}
                    with script.open(errors='replace') as handle:
                        stacks,counts=parse_script(handle,first['pid'],start,deadline,windows=windows,labels=labels)
                    for stack,count in stacks.items():all_stacks[stack]=all_stacks.get(stack,0)+count
                    for key in ('sampleCount','discardedEvents'):totals[key]+=counts[key]
                    for pid,count in counts['samplesByPid'].items():totals['samplesByPid'][pid]=totals['samplesByPid'].get(pid,0)+count
                    segment['sampleCount']=counts['sampleCount']
                else:
                    segment['decodeError']=result.stderr[-1000:]
            segments.append(segment)
            group=next_group
            if end_reason!='worker_set_changed':break
        if len(segments)==8 and end_reason=='worker_set_changed':end_reason='segment_limit'
        for pid, item in participants.items():item['sampleCount']=totals['samplesByPid'].get(str(pid),0)
        workers=[p for p in participants.values() if p['role']=='parallel worker']
        sampled=sum(p['sampleCount']>0 for p in workers)
        omitted=sorted({pid for s in segments for pid in s['omittedWorkerPids']})
        valid_seconds=sum(max(0,s['windows'].get(str(first['pid']),[0,0])[1]-s['windows'].get(str(first['pid']),[0,0])[0]) for s in segments)
        return all_stacks, {**totals,'uniqueStacks':len(all_stacks),'participants':list(participants.values()),'segments':segments,
            'startedAt':record['startedAt'],'finishedAt':now(),'captureSeconds':round(valid_seconds,6),'endReason':end_reason,
            'scope':'SQL leader and identity-verified PostgreSQL parallel workers',
            'parallelCoverage':'{} of {} attached parallel workers produced CPU samples; {} verified workers omitted by the 8-worker cap; partial time window, short-lived workers may be missed'.format(sampled,len(workers),len(omitted)),
            'coverageStatus':'partial_worker_cap' if omitted else 'observed_workers_sampled' if sampled==len(workers) and workers else 'leader_only_no_verified_worker' if not workers else 'partial',
            'workerCount':len(workers),'sampledWorkerCount':sampled,'omittedWorkerPids':omitted,
            'attribution':'Each PID has an independently revalidated time window. Process birth tokens prevent PID reuse; tail events and unrelated worker PIDs are excluded.'}

    @contextlib.contextmanager
    def managed(self, application, query, execution_id, run_id, phase):
        stop = threading.Event()
        task = self._submit(application, query, execution_id, run_id, phase, stop=stop)
        try:
            yield
        finally:
            stop.set()
            # Postprocessing is bounded and runs after the measured SQL, never in its timer.
            if task:
                task.join(timeout=12)

    def observe(self, rows):
        """Called by the normal collector; DREAM still starts only AFTER completion."""
        if not self.enabled or self._closed.is_set() or self._paused.is_set():
            return
        for row in rows:
            app = str(row.get("application_name") or "")
            if re.search(r"lab|sysinsight|profiler|exporter|grafana|dream", app, re.I):
                continue
            query = str(row.get("query") or "")
            if float(row.get("duration_ms") or 0) < 3000 or not re.match(r"^\s*(?:/\*.*?\*/\s*)*(?:select|with)\b", query, re.I | re.S):
                continue
            identity = "{}:{}:{}".format(row.get("pid"), row.get("backend_start"), row.get("query_start"))
            with self._lock:
                if identity in self._seen:
                    continue
                if len(self._seen) > 1000:
                    self._seen = dict(list(self._seen.items())[-500:])
                self._seen[identity] = time.monotonic()
            task = self._submit(app, query, None, None, "external_active", initial=row)
            if task:
                break

    def _submit(self, application, query, execution_id, run_id, phase, initial=None, stop=None):
        with self._lock:
            if not self.enabled or self._closed.is_set() or self._paused.is_set():
                return None
            # One perf recorder globally for this bridge. Never queue stale captures.
            if not self._slot.acquire(blocking=False):
                return None
            profile_id = "sql-profile-" + uuid.uuid4().hex[:20]
            task = threading.Thread(target=self._run, args=(profile_id, application, query, execution_id, run_id, phase, initial, stop or threading.Event()), daemon=True, name=profile_id)
            self._tasks[profile_id] = task
            task.start()
        return task

    def _save(self, record):
        with self.store._lock, self.store._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO sql_profiles VALUES(?,?,?,?,?,?,?,?)", (
                record["profileId"], record.get("executionId"), record["sqlSha256"], self.db.db,
                record["requestedAt"], record.get("finishedAt"), record["status"], json.dumps(record, ensure_ascii=False)))

    def _run(self, profile_id, application, query, execution_id, run_id, phase, initial, stop):
        record = {"schemaVersion": "keeninsight.sql-profile.v1", "profileId": profile_id,
                  "executionId": execution_id, "runId": run_id, "phase": phase,
                  "sqlSha256": digest(self.canonical(query)), "requestedAt": now(), "status": "waiting",
                  "statementSha256": digest(query), "sampledStatement": query,
                  "frequencyHz": self.frequency, "maxCaptureSeconds": self.seconds,
                  "source": "Linux perf cpu-clock samples", "scope": "SQL leader and verified parallel workers, when available",
                  "parallelCoverage": "not sampled; worker coverage unavailable",
                  "unit": "sample count", "flameGraph": None}
        try:
            # Leave sub-second statements alone. Setup + observer overhead isn't included in SQL timing.
            deadline = time.monotonic() + 5
            first = initial
            while not stop.is_set() and not self._closed.is_set() and not self._paused.is_set():
                current = self._snapshot(pid=initial.get("pid") if initial else None, application=application)
                if current and current.get("state") == "active" and float(current.get("duration_ms") or 0) >= 750:
                    if initial and not self.same_query(initial, current):
                        record.update(status="identity_changed", reason="SQL changed before capture")
                        return
                    first = current
                    break
                if time.monotonic() >= deadline:
                    break
                stop.wait(.2)
            if not first or stop.is_set() or self._closed.is_set() or self._paused.is_set():
                record.update(status="not_sampled", reason="SQL ended before the bounded sampling threshold")
                return
            if not first.get("backend_start") or not first.get("query_start"):
                record.update(status="identity_unavailable", reason="Backend/query start identity unavailable")
                return
            record.update(pid=int(first["pid"]), backendStart=first["backend_start"], queryStart=first["query_start"], applicationName=application)
            perf = shutil.which("perf")
            if not perf:
                record.update(status="unavailable", reason="perf executable not installed")
                return
            self.root.mkdir(parents=True, exist_ok=True)
            directory = self.root / profile_id
            directory.mkdir(mode=0o700)
            if shutil.disk_usage(str(directory)).free < 512 * 1024 * 1024:
                record.update(status="resource_limited", reason="Less than 512 MiB free; sampling skipped")
                return
            record.update(startedAt=now(), artifactDirectory=str(directory))
            stacks, metadata = self._capture_group(first, directory, perf, stop, record)
            record.update(metadata)
            if not stacks:
                if not metadata.get('status'):
                    record.update(status="no_samples", reason="No on-CPU stack events inside this SQL's validated window (may be waiting or too short)")
                return
            tree = stack_tree(stacks)
            with (directory / "cpu.folded").open("w") as handle:
                for stack, count in sorted(stacks.items()):
                    handle.write("{} {}\n".format(";".join(frame.replace(";", ":") for frame in stack), count))
            record.update(status="completed", flameGraph=tree,
                          limitations=["Partial on-CPU window, not whole SQL latency or off-CPU waits.",
                                       "Frame-pointer unwinding; unresolved frames remain explicit.",
                                       record["parallelCoverage"]])
        except Exception as exc:
            record.update(status="failed", reason="{}: {}".format(type(exc).__name__, exc)[:1500])
            LOG.warning("SQL profile unavailable: %s", record["reason"])
        finally:
            record.setdefault("finishedAt", now())
            try:
                self._save(record)
            except Exception:
                LOG.exception("could not persist SQL profile")
            with self._lock:
                self._tasks.pop(profile_id, None)
            self._slot.release()

    @staticmethod
    def _stop_process(proc):
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    def close(self):
        self._closed.set()
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            task.join(timeout=12)

    @contextlib.contextmanager
    def pause(self):
        """Drain only sampler tasks before a workspace archive/clear, never cancel SQL."""
        self._paused.set()
        try:
            with self._lock:
                tasks = list(self._tasks.values())
            for task in tasks:
                task.join(timeout=15)
                if task.is_alive():
                    raise RuntimeError('SQL sampler is still finalizing; retry clear after it finishes')
            yield
        finally:
            self._paused.clear()
