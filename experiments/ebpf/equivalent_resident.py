"""Persistent eBPF sentinel with explicit dynamic PID/birth authorization.

Functional parity uses PortalRun occupancy as a coarse general trigger. This is
deliberately NOT the final low-overhead resident configuration. Sparse semantic
events are aggregated and immediately notified; CPU stacks are on-demand only.
"""
import atexit
import ctypes as ct
import json
from pathlib import Path
import time

from .collector import atomic_json, process_identity

PROGRAM = r"""
#include <uapi/linux/ptrace.h>
struct state { u64 started, completed, portal_ns, wait, checkpoint, spill, last_notice; u32 depth; };
struct notice { u64 at; u32 pid, kind; };
BPF_HASH(allowed,u32,u64,32);
BPF_HASH(states,u32,struct state,32);
BPF_PERF_OUTPUT(events);
BPF_ARRAY(errors,u64,1);
int portal_enter(struct pt_regs *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32;
    if (!allowed.lookup(&pid)) return 0;
    struct state *s=states.lookup(&pid);
    if (s) { if (!s->depth) s->started=bpf_ktime_get_ns(); s->depth++; }
    return 0;
}
int portal_leave(struct pt_regs *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32;
    if (!allowed.lookup(&pid)) return 0;
    struct state *s=states.lookup(&pid);
    if (s && s->depth) {
        s->depth--;
        if (!s->depth) { s->portal_ns+=bpf_ktime_get_ns()-s->started; s->completed++; s->started=0; }
    }
    return 0;
}
int abort_portal(struct pt_regs *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32;
    if (!allowed.lookup(&pid)) return 0;
    struct state *s=states.lookup(&pid);
    if (s) { s->depth=0; s->started=0; }
    return 0;
}
static inline int notify(struct pt_regs *ctx,u32 kind) {
    u32 pid=bpf_get_current_pid_tgid()>>32;
    if (!allowed.lookup(&pid)) return 0;
    struct state *s=states.lookup(&pid);
    if (s) { if (kind==1) s->wait++; else if (kind==2) s->checkpoint++; else s->spill++; }
    u64 now=bpf_ktime_get_ns();
    if (s && s->last_notice && now-s->last_notice<100000000ULL) return 0;
    if (s) s->last_notice=now;
    struct notice e={.at=now,.pid=pid,.kind=kind};
    if (events.perf_submit(ctx,&e,sizeof(e))<0) {
        u32 zero=0; u64 *n=errors.lookup(&zero); if (n) __sync_fetch_and_add(n,1);
    }
    return 0;
}
int wait_event(struct pt_regs *ctx) { return notify(ctx,1); }
int checkpoint_event(struct pt_regs *ctx) { return notify(ctx,2); }
int spill_event(struct pt_regs *ctx) { return notify(ctx,3); }
"""


class Resident:
    HOOKS = (("PortalRun", "portal_enter", False), ("PortalRun", "portal_leave", True),
             ("AbortCurrentTransaction", "abort_portal", False),
             ("ProcSleep", "wait_event", False), ("CreateCheckPoint", "checkpoint_event", False),
             ("BufFileCreateTemp", "spill_event", False))

    def __init__(self, server, directory):
        from bcc import BPF
        self.server, self.directory = server, Path(directory)
        self.directory.mkdir(mode=0o700)
        self.targets, self.events, self.lost, self.closed = {}, [], 0, False
        self.event_names = {}
        self.bpf = BPF(text=PROGRAM)
        atexit.unregister(self.bpf.cleanup)
        self.output = (self.directory / "events.jsonl").open("w")
        self.bpf["events"].open_perf_buffer(self._notice, page_cnt=64, lost_cb=self._lost)
        self.manifest = {"status": "ready", "kind": "persistent_dynamic_PID_resident",
                         "always_on_CPU_stacks": False, "query_boundary_cost_accepted_for_parity": True,
                         "targets_history": [], "coarse_activity": "PortalRun occupancy, not exact pg_stat_activity",
                         "started_monotonic_ns": time.monotonic_ns()}
        atomic_json(self.directory / "manifest.json", self.manifest)

    def _lost(self, cpu, count):
        self.lost += count

    def _write(self, event):
        self.output.write(json.dumps(event) + "\n")
        self.output.flush()

    def _notice(self, cpu, data, size):
        event = self.bpf["events"].event(data)
        row = {"type": "semantic_event", "pid": event.pid, "timestamp_ns": event.at,
               "event": {1: "lock_wait", 2: "checkpoint", 3: "temp_file_create"}[event.kind]}
        self.events.append(row)
        self._write(row)

    def add(self, pids):
        for pid in pids:
            if pid in self.targets:
                continue
            target = process_identity(pid, self.server["binary"])
            try:
                event_names = []
                for symbol, function, returning in self.HOOKS:
                    before = set(self.bpf.uprobe_fds)
                    method = self.bpf.attach_uretprobe if returning else self.bpf.attach_uprobe
                    method(name=self.server["binary"], sym=symbol, fn_name=function, pid=pid)
                    event_names.extend(set(self.bpf.uprobe_fds) - before)
                if process_identity(pid, self.server["binary"]) != target:
                    raise RuntimeError("resident target identity changed while attaching")
                self.bpf["states"][ct.c_uint32(pid)] = self.bpf["states"].Leaf()
                self.bpf["allowed"][ct.c_uint32(pid)] = ct.c_uint64(target["start_ticks"])
                self.targets[pid] = target
                self.event_names[pid] = event_names
                self.manifest["targets_history"].append(target)
            except BaseException:
                self.close()
                raise

    def remove(self, pids):
        # Authorize first, disarm first. Detach only this owner's PID-specific
        # uprobes, never another collector's handles.
        for pid in pids:
            if pid not in self.targets:
                continue
            self.bpf["allowed"].__delitem__(ct.c_uint32(pid))
            # The process may already have exited after a finite pgbench
            # trial. Detach owned event handles; do NOT re-resolve symbols
            # through an exited/reused PID.
            for name in self.event_names.pop(pid):
                self.bpf.detach_uprobe_event(name)
            self.bpf["states"].__delitem__(ct.c_uint32(pid))
            del self.targets[pid]

    def snapshot(self):
        self.poll()
        now = time.monotonic_ns()
        rows = []
        for key, state in self.bpf["states"].items():
            rows.append({"type": "query_snapshot", "depth": 1, "pid": int(key.value),
                         "active": bool(state.depth), "timestamp_ns": now,
                         "elapsed_ns": now - state.started if state.started else 0,
                         "completed_portals": int(state.completed), "portal_ns": int(state.portal_ns),
                         "lock_wait_calls": int(state.wait), "checkpoint_calls": int(state.checkpoint),
                         "temp_file_calls": int(state.spill)})
        self._write({"type": "resident_aggregate", "timestamp_ns": now, "targets": rows})
        return [row for row in rows if row["active"]]

    def poll(self):
        self.bpf.perf_buffer_poll(timeout=1)

    def close(self):
        if not self.closed:
            try:
                self.bpf["allowed"].clear()
                self.manifest.update(
                    perf_lost_notifications=self.lost,
                    submit_failures=int(self.bpf["errors"][ct.c_int(0)].value))
                self.bpf.cleanup()
                self.manifest.update(status="completed", all_probes_detached=True,
                                     stopped_monotonic_ns=time.monotonic_ns())
            finally:
                self.output.close()
                self.closed = True
                atomic_json(self.directory / "manifest.json", self.manifest)
        return self.manifest
