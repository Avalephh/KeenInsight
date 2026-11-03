/*
 * PG12 ExecutorRun spans, not client latency. No stack sampling, SQL polling,
 * database mutation, or machine-wide event export. System totals cover every
 * selected probe invocation in authorized processes, including outside SQL;
 * query totals cover only their innermost active executor spans.
 */
#include <uapi/linux/ptrace.h>

#define METRICS 14
#define QUERY_DEPTH 4
#define CALL_DEPTH 4
#define SQL_BYTES 192

struct metric_t {
    u64 calls, duration_ns, value, maximum_ns, errors;
};
struct query_key_t { u32 tid, depth; };
struct query_t {
    u64 sequence, start_ns, birth, query_id, query_desc, cpu_start;
    struct metric_t metrics[METRICS];
    u64 sequence_end;
    u64 alerted, last_alert_ns[METRICS];
};
/* Top-level ExecutorRun invocations; not client transactions. */
struct query_totals_t {
    u64 sequence, completed, aborted, total_ns, maximum_ns, slow, detailed, sequence_end;
    u64 birth, start_ns;
    u64 latency_log2_us[32];
};
struct cpu_t { u64 total, on_since; };
struct call_key_t { u32 tid, metric, slot; };
struct call_t { u64 start_ns, query_start_ns, requested; u32 query_depth, reserved; };
struct event_t {
    u64 timestamp_ns, query_start_ns, process_start_ticks, query_id;
    u64 duration_ns, cpu_ns, value, total_ns, count, maximum_ns;
    u32 pid, tid, kind, metric, depth, flags;
    struct metric_t metrics[METRICS];
    char sql[SQL_BYTES];
};

BPF_HASH(targets, u32, u64, 32);
BPF_HASH(depths, u32, u32, 32);
BPF_HASH(queries, struct query_key_t, struct query_t, 128);
BPF_HASH(backend_activity, u32, struct query_t, 32);
BPF_HASH(query_totals, u32, struct query_totals_t, 32);
BPF_HASH(cpu_times, u32, struct cpu_t, 32);
BPF_HASH(call_depths, struct call_key_t, u32, 512);
BPF_HASH(calls, struct call_key_t, struct call_t, 4096);
BPF_PERCPU_ARRAY(query_scratch, struct query_t, 1);
BPF_PERCPU_ARRAY(event_scratch, struct event_t, 1);
/* failed perf submission, map capacity, depth overflow, read error, unmatched return */
BPF_PERCPU_ARRAY(health, u64, 5);
BPF_PERCPU_ARRAY(probe_counts, u64, METRICS);
/* emitted notifications, coalesced notifications, non-anomalous boundary pairs omitted */
BPF_PERCPU_ARRAY(message_stats, u64, 3);
BPF_PERF_OUTPUT(events);

static __always_inline void issue(u32 code) {
    u64 *n = health.lookup(&code);
    if (n) (*n)++;
}
static __always_inline u64 current_cpu(u32 tid, u64 now) {
#if TRACK_CPU
    struct cpu_t *cpu = cpu_times.lookup(&tid);
    return cpu ? cpu->total + (cpu->on_since ? now - cpu->on_since : 0) : 0;
#else
    return 0;
#endif
}
static __always_inline struct event_t *event(struct query_t *q, u32 depth, u32 kind) {
    u32 zero = 0;
    struct event_t *e = event_scratch.lookup(&zero);
    if (!e) return 0;
    __builtin_memset(e, 0, __builtin_offsetof(struct event_t, metrics));
    u64 identity = bpf_get_current_pid_tgid();
    e->pid = identity >> 32;
    e->tid = identity;
    e->timestamp_ns = bpf_ktime_get_ns();
    e->query_start_ns = q->start_ns;
    e->process_start_ticks = q->birth;
    e->query_id = q->query_id;
    e->depth = depth;
    e->kind = kind;
    return e;
}
static __always_inline void submit(void *ctx, struct event_t *e, u32 size) {
    if (events.perf_submit(ctx, e, size) < 0) issue(0);
}
static __always_inline int target(void) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    return targets.lookup(&pid) != 0;
}

int query_enter(struct pt_regs *ctx) {
    u64 identity = bpf_get_current_pid_tgid(), now = bpf_ktime_get_ns();
    u32 pid = identity >> 32, tid = identity, zero = 0;
    u64 *birth = targets.lookup(&pid);
    if (!birth) return 0;
    u32 *previous = depths.lookup(&tid);
    u32 depth = previous ? *previous + 1 : 1;
    if (depth > QUERY_DEPTH) {
        issue(2);
        depths.update(&tid, &depth);
        return 0;
    }
#if TRACK_CPU
    struct cpu_t *running = cpu_times.lookup(&tid);
    if (!running) {
        struct cpu_t cpu = {.total = 0, .on_since = now};
        cpu_times.update(&tid, &cpu);
    } else if (!running->on_since) running->on_since = now;
#endif
    struct query_t *q = query_scratch.lookup(&zero);
    if (!q) return 0;
    __builtin_memset(q, 0, sizeof(*q));
    q->sequence = q->sequence_end = 2;
    q->start_ns = now;
    q->birth = *birth;
    q->query_desc = PT_REGS_PARM1(ctx);
    q->cpu_start = current_cpu(tid, now);
    u64 plan = 0;
    if (bpf_probe_read(&plan, sizeof(plan), (void *)(q->query_desc + QD_PLAN_OFF)) ||
        !plan || bpf_probe_read(&q->query_id, sizeof(q->query_id),
                               (void *)(plan + PS_QUERYID_OFF))) issue(3);
    struct query_key_t key = {.tid = tid, .depth = depth};
    if (queries.update(&key, q) < 0 || depths.update(&tid, &depth) < 0) {
        issue(1);
        queries.delete(&key);
        return 0;
    }
#if EMIT_BOUNDARIES || CAPTURE_SQL
    struct event_t *e = event(q, depth, 1);
    if (!e) return 0;
#if CAPTURE_SQL
    __builtin_memset(e->sql, 0, sizeof(e->sql));
    u64 source = 0;
    if (!bpf_probe_read(&source, sizeof(source), (void *)(q->query_desc + QD_SOURCE_OFF)) && source) {
        if (bpf_probe_read_str(e->sql, sizeof(e->sql), (void *)source) < 0) issue(3);
    }
#endif
#if CAPTURE_SQL
    submit(ctx, e, sizeof(*e));
#else
    submit(ctx, e, __builtin_offsetof(struct event_t, metrics));
#endif
#endif
    return 0;
}

static __always_inline void completed_summary(u32 tid, u64 elapsed, u32 kind, u32 detailed) {
    struct query_totals_t *total = query_totals.lookup(&tid);
    if (!total) { issue(1); return; }
    total->sequence++;
    if (kind == 2) {
        total->completed++;
        total->total_ns += elapsed;
        if (elapsed > total->maximum_ns) total->maximum_ns = elapsed;
        if (elapsed >= LONG_QUERY_NS) total->slow++;
        u64 us = elapsed / 1000, bucket = 0;
#pragma unroll
        for (int i = 0; i < 31; i++) {
            if (us > 1) { bucket++; us >>= 1; }
        }
        if (bucket < 32) total->latency_log2_us[bucket]++;
    } else total->aborted++;
    if (detailed) total->detailed++;
    total->sequence++;
    total->sequence_end = total->sequence;
}
static __always_inline void finish(void *ctx, u32 tid, u32 depth, u32 kind, u32 reason) {
    struct query_key_t key = {.tid = tid, .depth = depth};
    struct query_t *q = queries.lookup(&key);
    if (q) {
        u64 elapsed = bpf_ktime_get_ns() - q->start_ns;
        u32 detailed = EMIT_BOUNDARIES || kind != 2 || q->alerted || elapsed >= LONG_QUERY_NS;
        if (depth == 1) completed_summary(tid, elapsed, kind, detailed);
        if (!detailed) {
            u32 index = 2;
            u64 *omitted = message_stats.lookup(&index);
            if (omitted) (*omitted)++;
            queries.delete(&key);
            return;
        }
        struct event_t *e = event(q, depth, kind);
        if (e) {
            e->duration_ns = e->timestamp_ns - q->start_ns;
            u64 cpu = current_cpu(tid, e->timestamp_ns);
            e->cpu_ns = cpu >= q->cpu_start ? cpu - q->cpu_start : 0;
            e->flags = reason;
#pragma unroll
            for (int i = 0; i < METRICS; i++) e->metrics[i] = q->metrics[i];
            submit(ctx, e, __builtin_offsetof(struct event_t, sql));
        }
        queries.delete(&key);
    }
}
int query_leave(struct pt_regs *ctx) {
    if (!target()) return 0;
    u32 tid = bpf_get_current_pid_tgid(), *current = depths.lookup(&tid);
    if (!current || !*current) return 0;
    u32 depth = *current;
    if (depth <= QUERY_DEPTH) finish(ctx, tid, depth, 2, 0);
    depth--;
    if (depth) depths.update(&tid, &depth);
    else {
        depths.delete(&tid);
    }
    return 0;
}
static __always_inline void abort_queries(void *ctx, u32 reason) {
    if (!target()) return;
    u32 tid = bpf_get_current_pid_tgid(), *d = depths.lookup(&tid);
    if (d) {
#pragma unroll
        for (int i = QUERY_DEPTH; i >= 1; i--) {
            if (*d >= i) finish(ctx, tid, i, 3, reason);
        }
    }
    depths.delete(&tid);
#pragma unroll
    for (int i = 0; i < METRICS; i++) {
        struct call_key_t k = {.tid = tid, .metric = i, .slot = 0};
        call_depths.delete(&k);
#pragma unroll
        for (int j = 0; j < CALL_DEPTH; j++) {
            k.slot = j;
            calls.delete(&k);
        }
    }
}
int transaction_abort(struct pt_regs *ctx) {
    abort_queries(ctx, 1);
    return 0;
}

static __always_inline int metric_enter(struct pt_regs *ctx, u32 metric, u64 requested) {
    if (!target()) return 0;
    u32 tid = bpf_get_current_pid_tgid(), *active = depths.lookup(&tid);
    u32 depth = active ? *active : 0;
    if (depth > QUERY_DEPTH) return 0;
    struct query_key_t qkey = {.tid = tid, .depth = depth};
    struct query_t *q = depth ? queries.lookup(&qkey) : backend_activity.lookup(&tid);
    if (!q) return 0;
    struct call_key_t key = {.tid = tid, .metric = metric, .slot = 0};
    u32 *old = call_depths.lookup(&key);
    u32 next = old ? *old + 1 : 1;
    if (call_depths.update(&key, &next) < 0) { issue(1); return 0; }
    if (next > CALL_DEPTH) { issue(2); return 0; }
    key.slot = next - 1;
    struct call_t call = {.start_ns = bpf_ktime_get_ns(),
                         .query_start_ns = q->start_ns, .requested = requested,
                         .query_depth = depth};
    if (calls.update(&key, &call) < 0) issue(1);
    return 0;
}
static __always_inline void accumulate(struct query_t *q, u32 metric, u64 elapsed,
                                       u64 value, u32 error) {
    if (metric >= METRICS) return;
    q->sequence++;
    struct metric_t *m = &q->metrics[metric];
    m->calls++;
    m->duration_ns += elapsed;
    m->value += value;
    if (elapsed > m->maximum_ns) m->maximum_ns = elapsed;
    m->errors += error;
    q->sequence++;
    q->sequence_end = q->sequence;
}
static __always_inline void notify(struct pt_regs *ctx, struct query_t *q, u32 depth,
                                  u32 metric, u64 elapsed, u64 value, u32 rate_trigger) {
    if (metric >= METRICS) return;
    u64 now = bpf_ktime_get_ns();
    u32 long_query = depth && now - q->start_ns >= LONG_QUERY_NS;
    if (elapsed < EVENT_LATENCY_NS && value < EVENT_BYTES && !long_query && !rate_trigger)
        return;
    metric &= 15;
    if (metric >= METRICS) return;
    q->alerted = 1;
    u32 index = 1;
    u64 *counter;
    if (q->last_alert_ns[metric] && now - q->last_alert_ns[metric] < ALERT_COOLDOWN_NS) {
        counter = message_stats.lookup(&index);
        if (counter) (*counter)++;
        return;
    }
    q->last_alert_ns[metric] = now;
    struct event_t *e = event(q, depth, 4);
    if (!e) return;
    e->metric = metric;
    e->count = 1;
    e->total_ns = e->maximum_ns = elapsed;
    e->value = value;
    e->flags = (elapsed >= EVENT_LATENCY_NS ? 1 : 0) |
               (value >= EVENT_BYTES ? 2 : 0) | (long_query ? 4 : 0) |
               (rate_trigger ? 8 : 0);
    submit(ctx, e, __builtin_offsetof(struct event_t, metrics));
    index = 0;
    counter = message_stats.lookup(&index);
    if (counter) (*counter)++;
}

/* Entry-only alternative: no return trap, call stack map, or fake timings.
 * Histograms/metrics are exact counts within the stated capture scope, not
 * downsampled counts. No returned-byte amount is claimed in this mode.
 */
static __always_inline int metric_count(struct pt_regs *ctx, u32 metric, u64 requested) {
    u32 tid = bpf_get_current_pid_tgid();
    struct query_t *backend = backend_activity.lookup(&tid);
    if (!backend || !target()) return 0;
    u32 *active = depths.lookup(&tid), depth = active ? *active : 0;
    if (depth > QUERY_DEPTH || metric >= METRICS) return 0;
    struct query_key_t key = {.tid = tid, .depth = depth};
    struct query_t *q = depth ? queries.lookup(&key) : backend;
    if (!q) return 0;
    accumulate(q, metric, 0, requested, 0);
    if (depth) accumulate(backend, metric, 0, requested, 0);
    if (metric >= METRICS) return 0;
    u32 trigger = metric == 9 || metric == 11 || metric == 12 ||
                  q->metrics[metric].calls == EVENT_CALLS;
    u64 *observed = probe_counts.lookup(&metric);
    if (observed) (*observed)++;
    notify(ctx, q, depth, metric, 0, requested, trigger);
    return 0;
}
static __always_inline int metric_leave(struct pt_regs *ctx, u32 metric, u32 returned_bytes) {
    if (!target()) return 0;
    u32 tid = bpf_get_current_pid_tgid();
    struct call_key_t key = {.tid = tid, .metric = metric, .slot = 0};
    u32 *depth = call_depths.lookup(&key);
    if (!depth || !*depth) return 0; /* probe may have been attached mid-call */
    u32 slot = *depth - 1;
    if (slot) call_depths.update(&key, &slot);
    else call_depths.delete(&key);
    if (slot >= CALL_DEPTH) return 0;
    key.slot = slot;
    struct call_t *call = calls.lookup(&key);
    if (!call) { issue(4); return 0; }
    u64 began = call->start_ns, query_start = call->query_start_ns, value = call->requested;
    u32 query_depth = call->query_depth;
    calls.delete(&key);
    struct query_key_t qkey = {.tid = tid, .depth = query_depth};
    struct query_t *backend = backend_activity.lookup(&tid);
    if (!backend) return 0;
    struct query_t *q = query_depth ? queries.lookup(&qkey) : backend;
    if (!q || q->start_ns != query_start) { issue(4); return 0; }
    s64 result = PT_REGS_RC(ctx);
    if (returned_bytes) value = result > 0 ? result : 0;
    u64 now = bpf_ktime_get_ns(), elapsed = now - began;
    /* BCC may reload helper-visible keys; re-establish a verifier bound. */
    if (metric >= METRICS) return 0;
    accumulate(q, metric, elapsed, value, returned_bytes && result < 0);
    if (query_depth) accumulate(backend, metric, elapsed, value, returned_bytes && result < 0);
    if (metric >= METRICS) return 0;
    u32 trigger = metric == 9 || q->metrics[metric].calls == EVENT_CALLS;
    u64 *observed = probe_counts.lookup(&metric);
    if (observed) (*observed)++;
    notify(ctx, q, query_depth, metric, elapsed, value, trigger);
    return 0;
}

/* Only scheduler counters are updated here; no per-switch events are exported. */
#if TRACK_CPU
TRACEPOINT_PROBE(sched, sched_switch) {
    u32 prev = args->prev_pid, next = args->next_pid;
    u64 now = bpf_ktime_get_ns();
    struct cpu_t *p = cpu_times.lookup(&prev);
    if (p && p->on_since) { p->total += now - p->on_since; p->on_since = 0; }
    struct cpu_t *n = cpu_times.lookup(&next);
    if (n) n->on_since = now;
    return 0;
}
#endif
TRACEPOINT_PROBE(sched, sched_process_exit) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (targets.lookup(&pid)) {
        abort_queries(args, 2);
        u32 tid = bpf_get_current_pid_tgid();
        struct query_t *backend = backend_activity.lookup(&tid);
        if (backend) {
            struct event_t *e = event(backend, 0, 5);
            if (e) {
                e->duration_ns = e->timestamp_ns - backend->start_ns;
                e->cpu_ns = current_cpu(tid, e->timestamp_ns);
                e->flags = 2;
#pragma unroll
                for (int i = 0; i < METRICS; i++) e->metrics[i] = backend->metrics[i];
                submit(args, e, __builtin_offsetof(struct event_t, sql));
            }
        }
        backend_activity.delete(&tid);
        cpu_times.delete(&tid);
        targets.delete(&pid); /* PID reuse must not inherit authorization. */
    }
    return 0;
}

/* Metric-specific entry/return wrappers are generated from the fixed catalog. */
