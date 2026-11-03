"""Bounded PG12 backend buffer counters via BPF, without per-buffer uprobes.

Read the exported pgBufferUsage prefix on low-frequency task-clock events.
This is backend-scoped logical buffer activity, NOT physical disk traffic or
an exact replacement for all database-wide pg_stat_database counters.
"""
import atexit
import ctypes as ct
from pathlib import Path
import subprocess
import time

from .collector import atomic_json, process_identity
from .equivalent_sampling import register_task_fd

PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <uapi/linux/bpf_perf_event.h>
struct usage { s64 hit, read, dirty, written, local_hit, local_read,
                  local_dirty, local_written, temp_read, temp_written; };
struct observation { u64 first_ns, last_ns; struct usage first, last; };
struct temporary { u64 files, bytes; };
BPF_HASH(addresses,u32,u64,32);
BPF_HASH(observations,u32,struct observation,32);
BPF_HASH(temporary_files,u32,struct temporary,32);
BPF_ARRAY(errors,u64,1);
static inline void error(void) {
    u32 zero=0; u64 *v=errors.lookup(&zero); if(v) __sync_fetch_and_add(v,1);
}
int observe(struct bpf_perf_event_data *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32; u64 *address=addresses.lookup(&pid);
    if (!address) return 0;
    struct usage now={};
    if (bpf_probe_read(&now,sizeof(now),(void *)*address)) { error(); return 0; }
    struct observation zero={}, *v=observations.lookup_or_try_init(&pid,&zero);
    if(!v) { error(); return 0; }
    u64 at=bpf_ktime_get_ns();
    if(!v->first_ns) { v->first_ns=at; v->first=now; }
    v->last_ns=at; v->last=now;
    return 0;
}
int tempfile(struct pt_regs *ctx) {
    u32 pid=bpf_get_current_pid_tgid()>>32;
    if(!addresses.lookup(&pid)) return 0;
    struct temporary zero={}, *v=temporary_files.lookup_or_try_init(&pid,&zero);
    if(!v) { error(); return 0; }
    v->files++; v->bytes+=PT_REGS_PARM1(ctx);
    return 0;
}
"""


def symbol_address(binary, pid, symbol="pgBufferUsage"):
    """Resolve exported ELF symbol with the target's PIE load bias."""
    fields = subprocess.check_output(["nm", "-D", binary], text=True).splitlines()
    value = next(int(row.split()[0], 16) for row in fields if row.split()[-1:] == [symbol])
    header = subprocess.check_output(["readelf", "-h", binary], text=True)
    if "DYN (" not in header:
        return value
    for line in Path("/proc/{}/maps".format(pid)).read_text().splitlines():
        row = line.split()
        if len(row) >= 6 and row[-1] == binary and int(row[2], 16) == 0:
            return int(row[0].split("-")[0], 16) + value
    raise RuntimeError("cannot resolve target PIE load bias")


class BufferObserver:
    def __init__(self, pids, server, directory, frequency=49):
        if ct.sizeof(ct.c_long) != 8 or not 1 <= frequency <= 100:
            raise ValueError("PG12 64-bit long ABI and 1..100Hz required")
        from bcc import BPF, PerfType, PerfSWConfig
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700)
        self.targets = [process_identity(pid, server["binary"]) for pid in pids]
        self.closed = False
        self.bpf = BPF(text=PROGRAM)
        atexit.unregister(self.bpf.cleanup)
        self.pair = (PerfType.SOFTWARE, PerfSWConfig.TASK_CLOCK)
        self.manifest = {"status": "attaching", "frequency_hz_per_task": frequency,
                         "scope": "explicit backend PID/birth, PG12 BufferUsage long prefix",
                         "counter_semantics": "logical shared blocks, not physical disk IO",
                         "tempfile_semantics": "pgstat_report_tempfile(size), same event as PG temp_bytes",
                         "targets": self.targets}
        try:
            function = self.bpf.load_func(b"observe", BPF.PERF_EVENT)
            for pid in pids:
                self.bpf["addresses"][ct.c_uint(pid)] = ct.c_ulonglong(symbol_address(server["binary"], pid))
                self.bpf.attach_uprobe(name=server["binary"], sym="pgstat_report_tempfile",
                                       fn_name="tempfile", pid=pid)
                register_task_fd(self.bpf, self.pair, pid, function.fd, frequency)
            self.started = time.monotonic()
            self.manifest["status"] = "ready"
            atomic_json(self.directory / "manifest.json", self.manifest)
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            try:
                self.bpf["addresses"].clear()
                self.bpf.detach_perf_event(ev_type=self.pair[0], ev_config=self.pair[1])
                rows = []
                for pid, value in self.bpf["observations"].items():
                    elapsed = (value.last_ns - value.first_ns) / 1e9
                    delta = {name: int(getattr(value.last, name) - getattr(value.first, name))
                             for name in ("hit", "read", "dirty", "written", "temp_read", "temp_written")}
                    if min(delta.values()) < 0:
                        raise RuntimeError("non-monotonic buffer counters; refusing fabricated rates")
                    rows.append({"pid": int(pid.value), "elapsed": elapsed, "delta": delta})
                files = [value for _, value in self.bpf["temporary_files"].items()]
                elapsed = time.monotonic() - getattr(self, "started", time.monotonic())
                hit, reads = (sum(row["delta"][key] for row in rows) for key in ("hit", "read"))
                self.manifest.update(
                    status="completed", rows=rows, wall_seconds=elapsed,
                    errors=int(self.bpf["errors"][ct.c_int(0)].value),
                    readIO=sum(row["delta"]["read"] / row["elapsed"] for row in rows if row["elapsed"] > 0),
                    hit=100 * hit / (hit + reads) if hit + reads else None,
                    writeIO=sum(int(value.bytes) for value in files) / elapsed if elapsed > 0 else None,
                    temporary_files=sum(int(value.files) for value in files),
                    temporary_bytes=sum(int(value.bytes) for value in files),
                    measured_backend_count=len(rows))
            finally:
                self.bpf.cleanup()
                self.closed = True
                self.manifest["all_probe_FDs_closed"] = True
                atomic_json(self.directory / "manifest.json", self.manifest)
        return self.manifest
