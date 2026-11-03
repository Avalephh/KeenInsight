"""Multi-backend eBPF profiling for functional parity, not overhead tuning.

Uses task-bound perf-event FDs as BPF triggers, never a perf subprocess.
All FDs are registered together so BCC 0.12 cannot overwrite earlier PIDs.
The old 49Hz/one-PID/five-second smoke sampler remains a separate experiment.
"""
import atexit
from collections import Counter
import ctypes as ct
import math
from pathlib import Path
import time

from .collector import atomic_json, process_identity

PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <uapi/linux/bpf_perf_event.h>
struct qkey { u32 pid, depth; };
struct span { u64 began, qid, source; };
struct sample_key { u32 pid; int user_stack, kernel_stack; u64 began, qid; };
BPF_HASH(allowed, u32, u64, 32);
BPF_HASH(depths, u32, u32, 32);
BPF_HASH(spans, struct qkey, struct span, 512);
BPF_HASH(counts, struct sample_key, u64, 65536);
BPF_STACK_TRACE(stacks, 16384);
BPF_ARRAY(health, u64, 4);
static inline void problem(u32 index) {
    u64 *v = health.lookup(&index);
    if (v) __sync_fetch_and_add(v, 1);
}
int query_enter(struct pt_regs *ctx) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (!allowed.lookup(&pid)) return 0;
    u32 zero=0, *old=depths.lookup_or_try_init(&pid,&zero);
    if (!old) { problem(2); return 0; }
    u32 depth=*old+1;
    *old=depth;
    if (depth>16) { problem(3); return 0; }
    struct qkey key={.pid=pid,.depth=depth};
    struct span span={.began=bpf_ktime_get_ns()};
    u64 desc=PT_REGS_PARM1(ctx), plan=0;
    bpf_probe_read(&plan,sizeof(plan),(void *)(desc+QD_PLAN_OFF));
    if (plan) bpf_probe_read(&span.qid,sizeof(span.qid),(void *)(plan+PS_QUERYID_OFF));
    bpf_probe_read(&span.source,sizeof(span.source),(void *)(desc+QD_SOURCE_OFF));
    if (spans.update(&key,&span)) problem(2);
    return 0;
}
int query_leave(struct pt_regs *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32, *depth=depths.lookup(&pid);
    if (!depth || !*depth) return 0; /* attached after entry */
    struct qkey key={.pid=pid,.depth=*depth};
    spans.delete(&key);
    if (*depth>1) (*depth)--;
    else depths.delete(&pid);
    return 0;
}
int query_abort(struct pt_regs *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32;
    if (!allowed.lookup(&pid)) return 0;
    #pragma unroll
    for (int i=1;i<=16;i++) {
        struct qkey key={.pid=pid,.depth=i}; spans.delete(&key);
    }
    depths.delete(&pid);
    return 0;
}
int sample(struct bpf_perf_event_data *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32;
    if (!allowed.lookup(&pid)) return 0;
    struct sample_key key={.pid=pid};
    key.user_stack=stacks.get_stackid(ctx,BPF_F_USER_STACK);
    key.kernel_stack=stacks.get_stackid(ctx,0);
    if (key.user_stack<0) problem(0);
    if (key.kernel_stack<0) problem(1);
    if (key.user_stack<0 && key.kernel_stack<0) return 0;
    u32 *depth=depths.lookup(&pid);
    if (depth && *depth<=16) {
        struct qkey qkey={.pid=pid,.depth=*depth};
        struct span *span=spans.lookup(&qkey);
        if (span) { key.began=span->began; key.qid=span->qid; }
    }
    u64 zero=0, *value=counts.lookup_or_try_init(&key,&zero);
    if (value) __sync_fetch_and_add(value,1);
    else problem(2);
    return 0;
}
"""


def validate(pids, seconds, frequency):
    if (not 1 <= len(pids) <= 32 or len(pids) != len(set(pids))
            or any(not isinstance(pid, int) or pid <= 1 for pid in pids)):
        raise ValueError("provide 1..32 distinct explicit PostgreSQL PIDs")
    if not math.isfinite(seconds) or not .1 <= seconds <= 120:
        raise ValueError("equivalence capture window must be 0.1..120s")
    if not isinstance(frequency, int) or not 1 <= frequency <= 1000:
        raise ValueError("equivalence sample frequency must be 1..1000Hz")


def folded_rows(rows):
    """Real sampled stacks, root->leaf, preserving repeated recursive frames."""
    counts = Counter()
    for row in rows:
        frames = list(reversed(row["user_frames"])) + list(reversed(row["kernel_frames"]))
        frames = [frame.replace(";", ":").replace("\n", "") for frame in frames if frame]
        if frames:
            counts[";".join(frames)] += row["sample_count"]
    return counts


def register_task_fd(bpf, pair, pid, function_fd, frequency):
    registry = bpf.open_perf_events.setdefault(pair, {})
    if pid in registry:
        raise ValueError("duplicate task-bound sampling FD")
    registry[pid] = bpf._attach_perf_event(function_fd, pair[0], pair[1], 0, frequency, pid, -1, -1)


def sample(pids, server, directory, seconds=15, frequency=300, on_ready=None,
           on_stopped=None, on_tick=None):
    validate(pids, seconds, frequency)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    targets = [process_identity(pid, server["binary"]) for pid in pids]
    from bcc import BPF, PerfType, PerfSWConfig
    layout = server["header_layout"]
    flags = ["-D{}={}".format(name, layout[key]) for name, key in
             (("QD_PLAN_OFF", "qd_plan"), ("QD_SOURCE_OFF", "qd_source"),
              ("PS_QUERYID_OFF", "ps_queryid"))]
    bpf = BPF(text=PROGRAM, cflags=flags)
    atexit.unregister(bpf.cleanup)
    pair = (PerfType.SOFTWARE, PerfSWConfig.TASK_CLOCK)
    fds = {}
    bpf.open_perf_events[pair] = fds
    manifest = {"schema": "keeninsight.ebpf-equivalent-profile.v1", "status": "attaching",
                "targets": targets, "frequency_hz_per_task": frequency, "requested_seconds": seconds,
                "sample_event": "task-clock on-CPU timer; NOT hardware CPU cycle count",
                "binary_sha256": server["binary_sha256"], "kernel": server["kernel"],
                "semantics": "user+kernel on-CPU stack samples; not function invocation counts"}
    atomic_json(directory / "manifest.json", manifest)
    rows = []
    try:
        function = bpf.load_func(b"sample", BPF.PERF_EVENT)
        for target in targets:
            pid = target["pid"]
            bpf["allowed"][ct.c_uint32(pid)] = ct.c_uint64(target["start_ticks"])
            bpf.attach_uretprobe(name=server["binary"], sym="ExecutorRun", fn_name="query_leave", pid=pid)
            bpf.attach_uprobe(name=server["binary"], sym="ExecutorRun", fn_name="query_enter", pid=pid)
            for symbol in ("AbortCurrentTransaction", "RollbackAndReleaseCurrentSubTransaction"):
                bpf.attach_uprobe(name=server["binary"], sym=symbol, fn_name="query_abort", pid=pid)
            # ONE cpu=-1 task FD follows each PID across CPUs.
            register_task_fd(bpf, pair, pid, function.fd, frequency)
            if process_identity(pid, server["binary"]) != target:
                raise RuntimeError("target identity changed during attach")
        began = time.monotonic_ns()
        manifest.update(status="ready", started_monotonic_ns=began)
        atomic_json(directory / "manifest.json", manifest)
        if on_ready:
            on_ready()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if on_tick:
                on_tick()
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        bpf["allowed"].clear()
        bpf.detach_perf_event(ev_type=pair[0], ev_config=pair[1])
        manifest.update(detached_monotonic_ns=time.monotonic_ns(), detached_before_symbolization=True)
        if on_stopped:
            on_stopped()
        symbols = {}
        for key, value in bpf["counts"].items():
            def frames(stack_id, kernel=False):
                if stack_id < 0:
                    return []
                result = []
                for addr in bpf["stacks"].walk(stack_id):
                    identity = (-1 if kernel else key.pid, addr)
                    if identity not in symbols:
                        symbols[identity] = (bpf.ksym(addr) if kernel else bpf.sym(addr, key.pid)
                                             ).decode("utf-8", errors="replace")
                    result.append(symbols[identity])
                return result
            rows.append({"pid": int(key.pid), "sample_count": int(value.value),
                         "execution_start_ns": int(key.began) or None,
                         "pg_query_id": str(key.qid) if key.qid else None,
                         "user_frames": frames(key.user_stack),
                         "kernel_frames": frames(key.kernel_stack, True)})
        health = {name: int(bpf["health"][ct.c_int(i)].value) for i, name in enumerate(
            ("user_stack_errors", "kernel_stack_errors", "map_capacity_errors", "query_depth_overflows"))}
        for target in targets:
            if process_identity(target["pid"], server["binary"]) != target:
                raise RuntimeError("profile target exited/reused before symbolization completed")
        if health["map_capacity_errors"]:
            raise RuntimeError("incomplete profile: " + str(health))
        folded = folded_rows(rows)
        (directory / "profile.folded").write_text("".join(
            "{} {}\n".format(stack, count) for stack, count in sorted(folded.items())))
        atomic_json(directory / "samples.json", {"rows": rows})
        manifest.update(status="completed", sample_count=sum(row["sample_count"] for row in rows),
                        per_pid_samples={str(pid): sum(r["sample_count"] for r in rows if r["pid"] == pid)
                                         for pid in pids}, health=health,
                        active_seconds=(manifest["detached_monotonic_ns"] - began) / 1e9,
                        folded_path=str(directory / "profile.folded"),
                        query_association="PID/birth + ExecutorRun start/queryId when available; "
                                          "outside-executor samples remain backend scoped",
                        query_association_complete=not bool(health["query_depth_overflows"]),
                        output_bytes=sum(p.stat().st_size for p in directory.iterdir() if p.is_file()))
        if not folded:
            raise RuntimeError("no usable CPU stack samples")
    except BaseException as exc:
        manifest.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        try:
            bpf["allowed"].clear()
            bpf.cleanup()
            manifest["all_probe_FDs_closed"] = True
        except BaseException as exc:
            manifest.update(status="failed", all_probe_FDs_closed=False, cleanup_error=str(exc))
            raise
        finally:
            atomic_json(directory / "manifest.json", manifest)
    return manifest
