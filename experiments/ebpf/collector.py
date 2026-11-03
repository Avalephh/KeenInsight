"""Bounded BCC driver. Imports of this module do not load/attach BPF programs."""
import atexit
import ctypes as ct
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .catalog import BY_NAME, CORE, PROBES, SCHEMA, choose_probes, prune_candidate

ROOT = Path(__file__).resolve().parent
METRIC_COUNT = len(PROBES)
HEALTH = ("perf_submit_failures", "map_capacity_failures", "depth_overflows",
          "user_memory_read_failures", "unmatched_metric_returns")


class Metric(ct.Structure):
    _fields_ = [(k, ct.c_uint64) for k in
                ("calls", "duration_ns", "value", "maximum_ns", "errors")]


class Event(ct.Structure):
    _fields_ = [(k, ct.c_uint64) for k in
                ("timestamp_ns", "query_start_ns", "process_start_ticks", "query_id",
                 "duration_ns", "cpu_ns", "value", "total_ns", "count", "maximum_ns")]
    _fields_ += [(k, ct.c_uint32) for k in ("pid", "tid", "kind", "metric", "depth", "flags")]
    _fields_ += [("metrics", Metric * METRIC_COUNT), ("sql", ct.c_char * 192)]


def command(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=30).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("prerequisite command failed: " + exc.stderr[-3000:]) from exc


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_server(pg_config):
    includes = command([pg_config, "--includedir-server"])
    binary = str(Path(command([pg_config, "--bindir"])) / "postgres")
    with tempfile.TemporaryDirectory(prefix="keen-ebpf-layout-") as tmp:
        output = str(Path(tmp) / "layout")
        command(["cc", "-I" + includes, str(ROOT / "pg_layout.c"), "-o", output])
        layout = json.loads(command([output]))
    version = command([binary, "--version"])
    if layout["pg_version_num"] // 10000 != 12 or "PostgreSQL) 12." not in version:
        raise ValueError("this experiment validates PG12 only; supply matching PG12 pg_config")
    if layout["pointer_bytes"] != 8 or sys.byteorder != "little":
        raise ValueError("only 64-bit little-endian server ABI is validated")
    symbols = set(line.split()[-1] for line in command(["nm", "-D", binary]).splitlines()
                  if line.split())
    for required in ("ExecutorRun", "AbortCurrentTransaction"):
        if required not in symbols:
            raise ValueError("required PostgreSQL symbol unavailable: " + required)
    return {"binary": str(Path(binary).resolve()), "binary_sha256": sha256_file(binary),
            "binary_version": version, "header_layout": layout,
            "kernel": platform.release(), "machine": platform.machine(),
            "bcc_available": importlib.util.find_spec("bcc") is not None,
            "tracefs_available": Path("/sys/kernel/debug/tracing/events/sched").is_dir()
                                 or Path("/sys/kernel/tracing/events/sched").is_dir(),
            "candidate_symbols": {p.name: p.kind == "kernel" or p.symbol in symbols
                                  for p in PROBES},
            "subtransaction_abort_symbol": "RollbackAndReleaseCurrentSubTransaction" in symbols}


def process_identity(pid, expected_binary):
    if pid <= 1:
        raise ValueError("invalid backend PID")
    directory = Path("/proc") / str(pid)
    if str((directory / "exe").resolve()) != str(Path(expected_binary).resolve()):
        raise ValueError("PID {} is not the selected postgres executable".format(pid))
    stat = (directory / "stat").read_text()
    birth = int(stat[stat.rfind(")") + 2:].split()[19])
    return {"pid": pid, "start_ticks": birth}


def atomic_json(path, data):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as handle:
        json.dump(data, handle, indent=2, allow_nan=False)
        handle.write("\n")
    os.replace(str(temp), str(path))


def metric_record(metric, name):
    return {"calls": int(metric.calls), "inclusive_duration_ns": int(metric.duration_ns),
            "value": int(metric.value), "value_semantics": BY_NAME[name].value,
            "maximum_duration_ns": int(metric.maximum_ns), "errors": int(metric.errors)}


def execution_id(run_id, pid, birth, began):
    return "{}:{}:{}:{}".format(run_id, pid, birth, began)


def base_record(run_id, pid, tid, birth, began, query_id, depth, timestamp):
    record = {"schema": SCHEMA, "run_id": run_id, "pid": int(pid), "tid": int(tid),
            "process_start_ticks": int(birth), "query_start_ns": int(began),
            "span_start_ns": int(began),
            "pg_query_id": str(query_id) if query_id else None,
            "query_id_source": "PlannedStmt.queryId" if query_id else "unavailable",
            "depth": int(depth), "timestamp_ns": int(timestamp), "clock": "monotonic",
            "scope": "ExecutorRun invocation" if depth else "authorized_backend_collection_window"}
    identity = execution_id(run_id, pid, birth, began)
    if depth:
        record["execution_id"] = identity
    else:
        record["activity_id"] = identity
        record["query_start_ns"] = None
    return record


def decode_event(run_id, raw, size, available):
    header = Event.metrics.offset
    if size < header:
        raise ValueError("short BPF event header")
    event = Event.from_buffer_copy(
        ct.string_at(raw, min(size, ct.sizeof(Event))).ljust(ct.sizeof(Event), b"\0"))
    kinds = {1: "query_begin", 2: "query_end", 3: "query_aborted", 4: "metric_alert",
             5: "backend_end"}
    if event.kind not in kinds:
        raise ValueError("unknown BPF event kind")
    expected = {1: (header, ct.sizeof(Event)),
                2: (Event.sql.offset, ct.sizeof(Event)),
                3: (Event.sql.offset, ct.sizeof(Event)), 4: (header,),
                5: (Event.sql.offset, ct.sizeof(Event))}[event.kind]
    # Linux/BCC 0.12 can expose the PERF_SAMPLE_RAW alignment's four trailing
    # bytes. They are transport padding, not fields or additional measurements.
    if size not in tuple(n + padding for n in expected for padding in (0, 4)):
        raise ValueError("incorrect BPF event ABI: kind={}, bytes={}, expected={}".format(
            event.kind, size, expected))
    record = base_record(run_id, event.pid, event.tid, event.process_start_ticks,
                         event.query_start_ns, event.query_id, event.depth, event.timestamp_ns)
    record["type"] = kinds[event.kind]
    if event.kind == 1:
        if event.sql:
            record["sql_prefix"] = event.sql.decode("utf-8", errors="replace")
            record["sql_text_semantics"] = "truncated sourceText; may contain a multi-statement batch"
        return record
    if event.kind in (2, 3, 5):
        record.update(duration_ns=int(event.duration_ns), on_cpu_ns=int(event.cpu_ns),
                      off_cpu_ns=max(0, int(event.duration_ns) - int(event.cpu_ns)),
                      cpu_scope="inclusive of nested executor calls" if event.depth else
                                "authorized process since collection arm",
                      resource_scope="innermost active executor; function durations can overlap"
                                     if event.depth else "all selected calls in authorized process; "
                                     "overlaps query totals, do not sum both levels",
                      reason={0: "returned", 1: "transaction_or_subtransaction_abort",
                              2: "backend_exit"}.get(event.flags, "unknown"),
                      metrics={p.name: metric_record(event.metrics[p.id], p.name)
                               for p in PROBES if p.name in available})
        return record
    if event.metric >= METRIC_COUNT:
        raise ValueError("metric ID outside catalog")
    record.update(probe=PROBES[event.metric].name, calls=int(event.count),
                  duration_ns=int(event.total_ns), value=int(event.value),
                  reasons=[label for bit, label in ((1, "call_latency"), (2, "resource_amount"),
                           (4, "long_running_query"), (8, "call_count_threshold")) if event.flags & bit],
                  accounting="notification_only; already included in cumulative totals")
    return record


def mark_coverage(record, available, pruned_at):
    """A detached probe is missing coverage, not a measured zero."""
    coverage = {}
    for name in available:
        cutoff = pruned_at.get(name)
        if cutoff is None or record["timestamp_ns"] < cutoff:
            coverage[name] = "attached"
        elif (record.get("span_start_ns") or record.get("query_start_ns", 0)) < cutoff:
            coverage[name] = "partial_pruned_during_span"
        else:
            coverage[name] = "not_observed_pruned"
            record.get("metrics", {}).pop(name, None)
    record["probe_coverage"] = coverage
    return record


def resolve_collection(args):
    lightweight = getattr(args, "detail_level", "lightweight") == "lightweight"
    return {"query_output": getattr(args, "query_output", None) or
                            ("aggregate" if lightweight else "all"),
            "metric_mode": getattr(args, "metric_mode", None) or
                           ("counts" if lightweight else "timed"),
            "cpu_mode": getattr(args, "cpu_mode", None) or ("off" if lightweight else "sched"),
            "scope": getattr(args, "scope", "system")}


def mark_measurements(record, collection):
    if collection["cpu_mode"] == "off":
        record.pop("on_cpu_ns", None)
        record.pop("off_cpu_ns", None)
        record["cpu_semantics"] = "not_collected_no_sched_hook"
    if collection["metric_mode"] == "counts":
        for name, metric in record.get("metrics", {}).items():
            for key in ("inclusive_duration_ns", "maximum_duration_ns", "errors"):
                metric.pop(key, None)
            if BY_NAME[name].value != "requested_bytes":
                metric.pop("value", None)
                metric.pop("value_semantics", None)
            metric["measurement"] = "entry_count_only_no_return_trap"
        if record.get("type") == "metric_alert":
            record.pop("duration_ns", None)
            if BY_NAME[record["probe"]].value != "requested_bytes":
                record.pop("value", None)
            record["measurement"] = "entry_count_only_no_return_trap"
    return record


class Sink:
    def __init__(self, directory, maximum, bootstrap=None, topic=None):
        fd = os.open(str(directory / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.handle = os.fdopen(fd, "w")
        self.bytes = self.records = 0
        self.maximum = maximum
        self.producer = None
        self.pending = []
        self.topic = topic
        if bootstrap:
            try:
                from kafka import KafkaProducer
                self.producer = KafkaProducer(bootstrap_servers=bootstrap.split(","),
                                             acks="all", max_block_ms=10000)
            except Exception:
                self.handle.close()
                raise

    def write(self, record):
        raw = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if self.bytes + len(raw) > self.maximum:
            raise RuntimeError("output size limit reached; capture stopped, no silent truncation")
        self.handle.write(raw.decode())
        if record.get("type") == "metric_alert":
            # A resident anomaly must be visible to the controller now, not
            # only after the next periodic aggregate flush.
            self.handle.flush()
        self.bytes += len(raw)
        self.records += 1
        if self.producer:
            if len(self.pending) >= 1024:
                self.pending.pop(0).get(timeout=10)
            self.pending.append(self.producer.send(self.topic, value=raw,
                                                   key=(record.get("execution_id") or
                                                        record.get("activity_id") or "").encode()))

    def close(self):
        self.handle.close()
        if self.producer:
            try:
                for future in self.pending:
                    future.get(timeout=10)
                self.producer.flush(timeout=10)
            finally:
                self.producer.close(timeout=10)


def source():
    text = (ROOT / "collector.bpf.c").read_text()
    for probe in PROBES:
        requested = "PT_REGS_PARM2(ctx)" if probe.name == "allocation" else "0"
        returned = 1 if probe.value == "returned_bytes" else 0
        text += "\nint enter_%d(struct pt_regs *ctx) { return metric_enter(ctx, %d, %s); }\n" % (
            probe.id, probe.id, requested)
        text += "int leave_%d(struct pt_regs *ctx) { return metric_leave(ctx, %d, %d); }\n" % (
            probe.id, probe.id, returned)
        text += "int count_%d(struct pt_regs *ctx) { return metric_count(ctx, %d, %s); }\n" % (
            probe.id, probe.id, requested)
    return text


def snapshot_records(bpf, run_id, available, long_ns):
    entries = [(key.tid, key.depth, q) for key, q in list(bpf["queries"].items())]
    entries += [(key.value, 0, q) for key, q in list(bpf["backend_activity"].items())]
    for tid, depth, q in entries:
        # Do not interpret a concurrently copied/mutating value as a consistent snapshot.
        if q.sequence & 1 or q.sequence != q.sequence_end:
            continue
        now = time.monotonic_ns()
        record = base_record(run_id, tid, tid, q.birth, q.start_ns, q.query_id, depth, now)
        try:
            cpu = bpf["cpu_times"][ct.c_uint32(tid)]
            total_cpu = int(cpu.total) + (max(0, now - int(cpu.on_since)) if cpu.on_since else 0)
            on_cpu = min(now - q.start_ns, max(0, total_cpu - int(q.cpu_start)))
            record.update(on_cpu_ns=on_cpu, off_cpu_ns=max(0, now - q.start_ns - on_cpu),
                          cpu_semantics="scheduler counter snapshot; query_end is authoritative")
        except KeyError:
            record["cpu_semantics"] = "unavailable: process exited during snapshot"
        record.update(type="query_snapshot" if depth else "backend_snapshot",
                      elapsed_ns=now - q.start_ns,
                      long_running=bool(depth and now - q.start_ns >= long_ns),
                      metrics={p.name: metric_record(q.metrics[p.id], p.name)
                               for p in PROBES if p.name in available},
                      accounting="cumulative snapshot; do not sum snapshots with query_end")
        yield record


def aggregate_records(bpf, run_id):
    for key, total in list(bpf["query_totals"].items()):
        if total.sequence & 1 or total.sequence != total.sequence_end:
            continue
        record = base_record(run_id, key.value, key.value, total.birth, total.start_ns,
                             0, 0, time.monotonic_ns())
        record.update(type="query_aggregate", scope="top_level_ExecutorRun_spans_not_transactions",
                      completed_spans=int(total.completed), aborted_spans=int(total.aborted),
                      total_executor_ns=int(total.total_ns), maximum_executor_ns=int(total.maximum_ns),
                      slow_completed_spans=int(total.slow), detailed_spans=int(total.detailed),
                      latency_log2_us=[int(n) for n in total.latency_log2_us],
                      accounting="cumulative; aggregate includes detailed spans, do not sum both",
                      histogram_semantics="floor(log2(max(1, ns/1000))), capped at 31; not exact percentiles")
        yield record


def validate_args(args):
    for name in ("duration", "report_seconds", "long_query_ms", "event_latency_ms",
                 "max_output_mb", "collector_cpu_budget", "overhead_budget"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(name + " must be finite and positive")
    if args.duration > 3600 or args.report_seconds < .1 or args.event_bytes <= 0:
        raise ValueError("duration <= 3600s, reporting >= 0.1s, event bytes > 0 required")
    if not 1 <= len(set(args.pid)) <= 32:
        raise ValueError("provide 1..32 explicit backend PIDs")
    cooldown = getattr(args, "alert_cooldown_ms", 100)
    if not math.isfinite(cooldown) or cooldown < 0 or getattr(args, "event_calls", 1000) <= 0:
        raise ValueError("alert cooldown must be finite/nonnegative and event calls positive")


def collect_run(args):
    validate_args(args)
    server = inspect_server(args.pg_config)
    targets = [process_identity(pid, server["binary"]) for pid in sorted(set(args.pid))]
    model = json.loads(Path(args.selector).read_text()) if args.selector else None
    names, selection = choose_probes(args.profile, model)
    collection = resolve_collection(args)
    if args.capture_sql and collection["query_output"] != "all":
        raise ValueError("--capture-sql requires --query-output all; SQL text is not lightweight")
    if args.capture_sql and collection["scope"] != "query":
        raise ValueError("--capture-sql requires query scope")
    costs = json.loads(Path(args.cost_model).read_text()) if args.cost_model else None
    if costs is not None:
        if costs.get("schema") != "keeninsight.probe-cost.v1" or not costs.get("calibration_id"):
            raise ValueError("cost model requires schema and calibration_id provenance")
        if costs.get("binary_sha256") != server["binary_sha256"] or costs.get("kernel") != server["kernel"]:
            raise ValueError("cost model must match the server binary and kernel")
        if costs.get("collection") != collection:
            raise ValueError("cost model must match scope/entry-vs-return/reporting/CPU configuration")
        if any(n not in costs["ns_per_call"] or not math.isfinite(float(costs["ns_per_call"][n]))
               or float(costs["ns_per_call"][n]) < 0 for n in names):
            raise ValueError("every selected probe requires a finite nonnegative calibrated cost")
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    run_id = uuid.uuid4().hex
    manifest = {"schema": SCHEMA, "run_id": run_id, "status": "starting", "server": server,
                "collector_pid": os.getpid(),
                "targets": targets, "selection": selection, "requested_probes": names,
                "arguments": vars(args), "started_at_unix_ns": time.time_ns(),
                "source_sha256": hashlib.sha256(source().encode()).hexdigest(),
                "dependencies": "BCC/eBPF only; no bridge, perf record, Prometheus, Grafana or LLM",
                "metric_semantics": "VFS bytes != physical disk IO; allocations != live memory; "
                                    "TCP returns != wire bytes; lock acquisition != lock wait",
                "overhead": "collector CPU is a pressure signal, not total instrumentation overhead",
                "cost_model": costs, "selector_model": model}
    manifest["collection"] = collection
    atomic_json(directory / "manifest.json", manifest)
    bpf = sink = None
    attached = []
    unavailable = {}
    lost = [0]
    callback_errors = []
    pruned_at = {}
    stop = [False]
    old_handlers = {}
    try:
        from bcc import BPF
        layout = server["header_layout"]
        flags = ["-DQD_PLAN_OFF=" + str(layout["qd_plan"]),
                 "-DQD_SOURCE_OFF=" + str(layout["qd_source"]),
                 "-DPS_QUERYID_OFF=" + str(layout["ps_queryid"]),
                 "-DCAPTURE_SQL=" + str(int(args.capture_sql)),
                 "-DEMIT_BOUNDARIES=" + str(int(collection["query_output"] == "all")),
                 "-DTRACK_CPU=" + str(int(collection["cpu_mode"] == "sched")),
                 "-DLONG_QUERY_NS=" + str(int(args.long_query_ms * 1e6)) + "ULL",
                 "-DEVENT_LATENCY_NS=" + str(int(args.event_latency_ms * 1e6)) + "ULL",
                 "-DEVENT_BYTES=" + str(args.event_bytes) + "ULL",
                 "-DEVENT_CALLS=" + str(args.event_calls) + "ULL",
                 "-DALERT_COOLDOWN_NS=" + str(int(args.alert_cooldown_ms * 1e6)) + "ULL"]
        bpf = BPF(text=source(), cflags=flags)
        # BCC 0.12's cleanup is not idempotent; own exactly one explicit cleanup.
        atexit.unregister(bpf.cleanup)
        if collection["scope"] == "query":
            for target in targets:
                kwargs = {"name": server["binary"], "pid": target["pid"]}
                bpf.attach_uprobe(sym="ExecutorRun", fn_name="query_enter", **kwargs)
                bpf.attach_uretprobe(sym="ExecutorRun", fn_name="query_leave", **kwargs)
                bpf.attach_uprobe(sym="AbortCurrentTransaction", fn_name="transaction_abort", **kwargs)
                if server["subtransaction_abort_symbol"]:
                    bpf.attach_uprobe(sym="RollbackAndReleaseCurrentSubTransaction",
                                      fn_name="transaction_abort", **kwargs)

        def detach(name):
            probe = BY_NAME[name]
            if probe.kind == "kernel":
                bpf.detach_kprobe(event=probe.symbol)
                if collection["metric_mode"] == "timed":
                    bpf.detach_kretprobe(event=probe.symbol)
            else:
                for target in targets:
                    bpf.detach_uprobe(name=server["binary"], sym=probe.symbol, pid=target["pid"])
                    if collection["metric_mode"] == "timed":
                        bpf.detach_uretprobe(name=server["binary"], sym=probe.symbol, pid=target["pid"])
            attached.remove(name)

        for name in names:
            probe = BY_NAME[name]
            try:
                if not server["candidate_symbols"][name]:
                    raise ValueError("symbol not exported by selected server")
                if probe.kind == "kernel":
                    bpf.attach_kprobe(event=probe.symbol, fn_name=(
                        "count_" if collection["metric_mode"] == "counts" else "enter_") + str(probe.id))
                    if collection["metric_mode"] == "timed":
                        try:
                            bpf.attach_kretprobe(event=probe.symbol, fn_name="leave_" + str(probe.id),
                                                maxactive=128)
                        except Exception:
                            bpf.detach_kprobe(event=probe.symbol)
                            raise
                else:
                    for target in targets:
                        bpf.attach_uprobe(name=server["binary"], sym=probe.symbol,
                                          fn_name=("count_" if collection["metric_mode"] == "counts"
                                                   else "enter_") + str(probe.id), pid=target["pid"])
                        if collection["metric_mode"] == "timed":
                            bpf.attach_uretprobe(name=server["binary"], sym=probe.symbol,
                                                 fn_name="leave_" + str(probe.id), pid=target["pid"])
                attached.append(name)
            except Exception as exc:
                unavailable[name] = str(exc)
                if name in CORE:
                    raise
                # Partial multi-PID attachment must not be mislabeled as unavailable.
                # Fail closed; cleanup detaches any already-attached partial pair.
                if server["candidate_symbols"][name]:
                    raise
        available = tuple(attached)
        sink = Sink(directory, int(args.max_output_mb * 1024 * 1024),
                    args.kafka_bootstrap, args.kafka_topic)

        def consume(cpu, data, size):
            if callback_errors:
                return
            try:
                record = decode_event(run_id, data, size, available)
                mark_coverage(record, available, pruned_at)
                mark_measurements(record, collection)
                record["attached_probes_at_delivery"] = list(attached)
                sink.write(record)
            except Exception as exc:
                callback_errors.append(str(exc))
                stop[0] = True

        bpf["events"].open_perf_buffer(consume, page_cnt=64,
                                      lost_cb=lambda cpu, count: lost.__setitem__(0, lost[0] + count))
        for target in targets:
            if process_identity(target["pid"], server["binary"]) != target:
                raise ValueError("backend identity changed while probes were attaching")
            key = ct.c_uint32(target["pid"])
            backend = bpf["backend_activity"].Leaf()
            backend.sequence = backend.sequence_end = 2
            backend.start_ns = time.monotonic_ns()
            backend.birth = target["start_ticks"]
            bpf["backend_activity"][key] = backend
            totals = bpf["query_totals"].Leaf()
            totals.birth, totals.start_ns = target["start_ticks"], backend.start_ns
            bpf["query_totals"][key] = totals
            if collection["cpu_mode"] == "sched":
                bpf["cpu_times"][key] = bpf["cpu_times"].Leaf()
            bpf["targets"][key] = ct.c_uint64(target["start_ticks"])
            if process_identity(target["pid"], server["binary"]) != target:
                del bpf["targets"][key]
                raise ValueError("backend identity changed while authorizing PID")
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, lambda number, frame: stop.__setitem__(0, True))
        manifest.update(status="ready", attached_probes=list(attached), unavailable_probes=unavailable,
                        ready_at_unix_ns=time.time_ns())
        atomic_json(directory / "manifest.json", manifest)
        started = previous = time.monotonic()
        next_report = started + args.report_seconds
        previous_cpu = time.process_time()
        previous_counts = [0] * METRIC_COUNT
        previous_lost = 0
        while not stop[0] and time.monotonic() - started < args.duration:
            bpf.perf_buffer_poll(timeout=100)
            now = time.monotonic()
            if now < next_report:
                continue
            for record in snapshot_records(bpf, run_id, available, int(args.long_query_ms * 1e6)):
                mark_coverage(record, available, pruned_at)
                mark_measurements(record, collection)
                record["attached_probes_at_delivery"] = list(attached)
                sink.write(record)
            if collection["scope"] == "query":
                for record in aggregate_records(bpf, run_id):
                    sink.write(record)
            counters = [int(bpf["probe_counts"].sum(ct.c_int(p.id)).value) for p in PROBES]
            cpu = time.process_time()
            cpu_percent = 100 * (cpu - previous_cpu) / (now - previous)
            estimated = None if costs is None else cpu_percent + 100 * sum(
                max(0, counters[p.id] - previous_counts[p.id]) * float(costs["ns_per_call"][p.name])
                for p in PROBES if p.name in attached) / ((now - previous) * 1e9)
            pressure = lost[0] > previous_lost or cpu_percent > args.collector_cpu_budget
            if estimated is not None:
                pressure = pressure or estimated > args.overhead_budget
            pruned = None
            if pressure and now - started >= 2 * args.report_seconds:
                protected = ("temp_file_create",) if args.profile == "events" else CORE
                pruned = prune_candidate(attached, model, protected)
                if pruned:
                    pruned_at[pruned] = time.monotonic_ns()
                    detach(pruned)
            sink.write({"schema": SCHEMA, "run_id": run_id, "type": "collector_health",
                        "timestamp_ns": time.monotonic_ns(), "collector_cpu_percent": cpu_percent,
                        "estimated_cpu_overhead_percent": estimated,
                        "budget_semantics": "one-way pruning; collector pressure plus optional "
                                            "independently calibrated estimate, not measured DB slowdown",
                        "perf_lost_notifications": lost[0], "pruned_probe": pruned,
                        "pruned_at_ns": dict(pruned_at),
                        "probe_completed_calls": dict(zip((p.name for p in PROBES), counters)),
                        "attached_probes": list(attached)})
            for target in targets:
                try:
                    valid = process_identity(target["pid"], server["binary"]) == target
                except (OSError, ValueError):
                    valid = False
                if not valid:
                    try:
                        del bpf["targets"][ct.c_uint32(target["pid"])]
                    except KeyError:
                        pass
            sink.handle.flush()
            previous, previous_cpu, previous_counts, previous_lost = now, cpu, counters, lost[0]
            next_report = now + args.report_seconds
        manifest["disarmed_at_monotonic_ns"] = time.monotonic_ns()
        bpf["targets"].clear()
        for _ in range(3):
            bpf.perf_buffer_poll(timeout=10)
        if callback_errors:
            raise RuntimeError("; ".join(callback_errors[:3]))
        for record in snapshot_records(bpf, run_id, available, int(args.long_query_ms * 1e6)):
            mark_coverage(record, available, pruned_at)
            mark_measurements(record, collection)
            record.update(type="incomplete_capture_stop" if record["depth"] else "backend_final",
                          disarmed_at_monotonic_ns=manifest["disarmed_at_monotonic_ns"])
            sink.write(record)
        if collection["scope"] == "query":
            for record in aggregate_records(bpf, run_id):
                record["type"] = "query_aggregate_final"
                sink.write(record)
        manifest.update(status="completed", capture_wall_seconds=time.monotonic() - started)
    except BaseException as exc:
        manifest.update(status="failed", error="{}: {}".format(type(exc).__name__, exc))
        raise
    finally:
        already_failing = sys.exc_info()[0] is not None
        cleanup_errors = []
        manifest["finished_at_unix_ns"] = time.time_ns()
        manifest["perf_lost_notifications"] = lost[0]
        if bpf is not None:
            try:
                manifest["bpf_health"] = {name: int(bpf["health"].sum(ct.c_int(i)).value)
                                          for i, name in enumerate(HEALTH)}
                manifest["attached_probes_at_stop"] = list(attached)
                manifest["pruned_at_ns"] = pruned_at
                manifest["message_stats"] = {
                    name: int(bpf["message_stats"].sum(ct.c_int(i)).value) for i, name in enumerate(
                        ("alert_notifications", "coalesced_alert_notifications",
                         "normal_boundary_pairs_omitted"))}
            except Exception as exc:
                cleanup_errors.append("health: " + str(exc))
            try:
                bpf.cleanup()
            except Exception as exc:
                cleanup_errors.append("detach: " + str(exc))
        if sink:
            try:
                sink.close()
            except Exception as exc:
                cleanup_errors.append("sink: " + str(exc))
            finally:
                manifest.update(output_bytes=sink.bytes, output_records=sink.records)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if cleanup_errors:
            manifest.update(status="failed", cleanup_errors=cleanup_errors)
        atomic_json(directory / "manifest.json", manifest)
        if cleanup_errors and not already_failing:
            raise RuntimeError("collector finalization failed: " + "; ".join(cleanup_errors))
