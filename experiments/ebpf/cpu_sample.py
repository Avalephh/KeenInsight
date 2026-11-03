"""Explicit, short, PID-scoped eBPF CPU sampling; no perf subprocess.

Samples are neither invocation counts nor SQL latency/IO measurements.
This supplies a bounded read/CPU diagnosis experiment, not a new tuning model.
"""
import atexit
from collections import Counter
import math
import time

from .collector import process_identity

PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <uapi/linux/bpf_perf_event.h>
struct key_t { u32 pid; int stack; };
BPF_HASH(allowed, u32, u64, 1);
BPF_HASH(counts, struct key_t, u64, 2048);
BPF_STACK_TRACE(stacks, 1024);
BPF_ARRAY(health, u64, 2);
int sample_cpu(struct bpf_perf_event_data *ctx) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (!allowed.lookup(&pid)) return 0;
    int stack = stacks.get_stackid(ctx, BPF_F_USER_STACK);
    u32 index = stack < 0 ? 0 : 1;
    u64 *counter = health.lookup(&index);
    if (stack < 0) {
        if (counter) __sync_fetch_and_add(counter, 1);
        return 0;
    }
    struct key_t key = {.pid = pid, .stack = stack};
    u64 zero = 0;
    u64 *value = counts.lookup_or_try_init(&key, &zero);
    if (value) __sync_fetch_and_add(value, 1);
    else if (counter) __sync_fetch_and_add(counter, 1);
    return 0;
}
"""


def validate_window(pids, seconds, frequency):
    if len(pids) != 1 or any(p <= 1 for p in pids):
        raise ValueError("provide exactly one explicit PostgreSQL PID")
    if not math.isfinite(seconds) or not .1 <= seconds <= 5:
        raise ValueError("CPU diagnosis window must be 0.1..5 seconds")
    if not isinstance(frequency, int) or not 1 <= frequency <= 49:
        raise ValueError("sample frequency must be 1..49 Hz per target task")


def sample_window(pids, binary, seconds=2, frequency=49):
    validate_window(pids, seconds, frequency)
    # Check identities before any attach, and again before accepting evidence.
    targets = [process_identity(pid, binary) for pid in pids]
    from bcc import BPF, PerfType, PerfSWConfig
    bpf = BPF(text=PROGRAM)
    atexit.unregister(bpf.cleanup)  # BCC 0.12 cleanup must run exactly once.
    rows, health = [], {}
    began = ended = None
    attached = False
    try:
        for target in targets:
            pid = target["pid"]
            bpf["allowed"][bpf["allowed"].Key(pid)] = bpf["allowed"].Leaf(target["start_ticks"])
            if process_identity(pid, binary) != target:
                raise RuntimeError("sampling target changed during attach")
            # BCC 0.12's public cpu=-1 helper opens one FD per CPU; repeated
            # calls also overwrite its FD registry. Use ONE task-bound FD
            # (pid>0, cpu=-1) that follows this process across CPUs, and register
            # it for the normal BCC detach/cleanup path.
            function = bpf.load_func(b"sample_cpu", BPF.PERF_EVENT)
            fd = bpf._attach_perf_event(function.fd, PerfType.SOFTWARE, PerfSWConfig.TASK_CLOCK,
                                       0, frequency, pid, -1, -1)
            bpf.open_perf_events[(PerfType.SOFTWARE, PerfSWConfig.TASK_CLOCK)] = {-1: fd}
            attached = True
        began = time.monotonic_ns()
        time.sleep(seconds)
    finally:
        # Stop collection before reading and symbolizing any of the stacks.
        bpf["allowed"].clear()
        ended = time.monotonic_ns()
        try:
            if attached:
                bpf.detach_perf_event(ev_type=PerfType.SOFTWARE, ev_config=PerfSWConfig.TASK_CLOCK)
            for key, value in bpf["counts"].items():
                frames = []
                for address in bpf["stacks"].walk(key.stack):
                    symbol = bpf.sym(address, key.pid).decode("utf-8", errors="replace")
                    frames.append(symbol)
                rows.append({"pid": int(key.pid), "sample_count": int(value.value), "frames": frames})
            health = {"user_stack_errors": int(bpf["health"][bpf["health"].Key(0)].value),
                      "count_map_failures": int(bpf["health"][bpf["health"].Key(1)].value)}
        finally:
            bpf.cleanup()
    for target in targets:
        if process_identity(target["pid"], binary) != target:
            raise RuntimeError("sampling target exited or changed before evidence acceptance")
    functions = Counter()
    for row in rows:
        # One stack sample contributes at most once to each function.
        functions.update({name: row["sample_count"] for name in set(row["frames"])
                          if name and name != "[unknown]"})
    return {"schema": "keeninsight.ebpf-cpu-samples.v1",
            "status": "observed" if rows else "insufficient_samples",
            "targets": targets, "frequency_hz_per_task": frequency,
            "requested_seconds": seconds, "active_seconds": (ended - began) / 1e9,
            "detached_before_symbolization": True, "health": health,
            "sample_count": sum(row["sample_count"] for row in rows), "stacks": rows,
            "functions": [{"function": name, "samples": count}
                          for name, count in functions.most_common(40)],
            "semantics": "on-CPU stack samples, not invocation counts, off-CPU waits, IO or SQL latency",
            "scope": "explicit operator-selected diagnosis only; not an always-on sampler"}
