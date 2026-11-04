"""PG12 eBPF sentinel: observe the EXISTING statistics collector's recv().

No PortalRun/ExecutorRun, per-tuple, buffer, WAL-flush or query-text probes.
Only the selected cluster's statistics receiver is instrumented. PG12 already
batches ordinary backend reports (normally >=500ms apart); this does NOT add
queries or force reports. The counters are database-scoped RECEIVED statistics,
not business TPS, query latency, or proof of a particular root cause.
"""
import atexit
import ctypes as ct
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .collector import atomic_json, process_identity

PROGRAM = r"""
#include <uapi/linux/ptrace.h>
struct header { s32 kind, size; u32 database; s32 entries, commits, rollbacks;
                s64 read_us, write_us; };
struct totals {
    u64 recv_calls, messages, reports, commits, rollbacks, returned, fetched,
        blocks, hits, read_us, write_us, temp_files, temp_bytes, deadlocks, errors;
};
BPF_ARRAY(counters,struct totals,1);
BPF_HASH(inflight,u32,u64,4);
BPF_ARRAY(enabled,u32,1);
static inline struct totals *counts(void) { u32 z=0; return counters.lookup(&z); }
int enter_recv(struct pt_regs *ctx) {
    u32 z=0, *on=enabled.lookup(&z);
    if(!on || !*on) return 0;
    u32 tid=bpf_get_current_pid_tgid();
    u64 address=PT_REGS_PARM2(ctx);
    struct totals *c=counts(); if(!c) return 0;
    c->recv_calls++;
    if(inflight.lookup(&tid)) c->errors++; /* missed return or unexpected nesting */
    if(inflight.update(&tid,&address)) c->errors++;
    return 0;
}
int exit_recv(struct pt_regs *ctx) {
    u32 tid=bpf_get_current_pid_tgid(); u64 *p=inflight.lookup(&tid);
    if(!p) return 0;
    u64 address=*p; inflight.delete(&tid);
    s64 size=PT_REGS_RC(ctx);
    if(size<8) return 0; /* EAGAIN/EINTR and ignored short messages */
    struct totals *c=counts(); if(!c) return 0;
    s32 hdr[2]={};
    if(bpf_probe_read(hdr,sizeof(hdr),(void *)address)) { c->errors++; return 0; }
    if(hdr[1]!=size) { c->errors++; return 0; }
    c->messages++;
    if(hdr[0]!=TAB_TYPE && hdr[0]!=TEMP_TYPE && hdr[0]!=DEADLOCK_TYPE) return 0;
    u32 database=0;
    if(size<12 || bpf_probe_read(&database,4,(void *)(address+8))) {
        c->errors++; return 0;
    }
    if(database!=DATABASE_OID) return 0;
    if(hdr[0]==TAB_TYPE) {
        struct header h={};
        if(size<TAB_HEADER || bpf_probe_read(&h,sizeof(h),(void *)address)) {
            c->errors++; return 0;
        }
        if(h.entries<0 || h.entries>ENTRIES ||
           size!=TAB_HEADER+h.entries*ENTRY_BYTES ||
           h.commits<0 || h.rollbacks<0 || h.read_us<0 || h.write_us<0) {
            c->errors++; return 0;
        }
        c->reports++; c->commits+=h.commits; c->rollbacks+=h.rollbacks;
        c->read_us+=h.read_us; c->write_us+=h.write_us;
        for(int i=0;i<ENTRIES;i++) {
            if(i>=h.entries) break;
            u64 entry=address+TAB_HEADER+i*ENTRY_BYTES;
            s64 returned=0, fetched=0, blocks=0, hits=0;
            if(bpf_probe_read(&returned,8,(void *)(entry+RETURNED_OFFSET)) ||
               bpf_probe_read(&fetched,8,(void *)(entry+FETCHED_OFFSET)) ||
               bpf_probe_read(&blocks,8,(void *)(entry+BLOCKS_OFFSET)) ||
               bpf_probe_read(&hits,8,(void *)(entry+HIT_OFFSET))) {
                c->errors++; return 0;
            }
            if(returned<0 || fetched<0 || blocks<0 || hits<0 || hits>blocks) {
                c->errors++; return 0;
            }
            c->returned+=returned; c->fetched+=fetched;
            c->blocks+=blocks; c->hits+=hits;
        }
    } else if(hdr[0]==TEMP_TYPE) {
        u64 bytes=0;
        if(size!=TEMP_BYTES ||
           bpf_probe_read(&bytes,8,(void *)(address+TEMP_SIZE_OFFSET))) {
            c->errors++; return 0;
        }
        c->temp_files++; c->temp_bytes+=bytes;
    } else {
        c->deadlocks++;
    }
    return 0;
}
"""

FIELDS = ("recv_calls", "messages", "reports", "commits", "rollbacks", "returned",
          "fetched", "blocks", "hits", "read_us", "write_us", "temp_files",
          "temp_bytes", "deadlocks", "errors")


def stats_layout(pg_config):
    includes = subprocess.check_output([pg_config, "--includedir-server"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="keen-pgstats-layout-") as tmp:
        exe = str(Path(tmp) / "layout")
        subprocess.run(["cc", "-I" + includes,
                        str(Path(__file__).with_name("pg_stats_layout.c")), "-o", exe],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = json.loads(subprocess.check_output([exe], text=True))
    if (result["pg_version_num"] // 10000 != 12 or result["pointer_bytes"] != 8
            or sys.byteorder != "little" or result["header_bytes"] != 8
            or result["tab_header_bytes"] != 40 or not 1 <= result["entries"] <= 8
            or any(result[key] != value for key, value in {
                "database_offset": 8, "entries_offset": 12, "commits_offset": 16,
                "rollbacks_offset": 20, "read_us_offset": 24, "write_us_offset": 32}.items())):
        raise ValueError("unsupported PG12 statistics message ABI")
    return result


def program(layout, database_oid):
    if not isinstance(database_oid, int) or not 0 < database_oid < 2 ** 32:
        raise ValueError("explicit positive database OID required")
    names = {
        "TAB_TYPE": "tab_type", "TEMP_TYPE": "temp_type", "DEADLOCK_TYPE": "deadlock_type",
        "TAB_HEADER": "tab_header_bytes", "ENTRY_BYTES": "entry_bytes", "ENTRIES": "entries",
        "RETURNED_OFFSET": "returned_offset", "FETCHED_OFFSET": "fetched_offset",
        "BLOCKS_OFFSET": "blocks_offset", "HIT_OFFSET": "hit_offset",
        "TEMP_BYTES": "temp_bytes", "TEMP_SIZE_OFFSET": "temp_size_offset",
    }
    definitions = ["#define DATABASE_OID {}".format(database_oid)]
    definitions += ["#define {} {}".format(key, int(layout[value])) for key, value in names.items()]
    return "\n".join(definitions) + "\n" + PROGRAM


def receiver_target(admin, server):
    """One-time control-plane discovery; NEVER uses SQL metrics as the alarm."""
    data = Path(admin.execute("SHOW data_directory", value=True))
    postmaster = int((data / "postmaster.pid").read_text().splitlines()[0])
    children = Path("/proc/{}/task/{}/children".format(postmaster, postmaster)).read_text().split()
    matches = [int(pid) for pid in children
               if "stats collector" in Path("/proc/{}/cmdline".format(pid)).read_text()]
    if len(matches) != 1:
        raise RuntimeError("need exactly one PG12 stats collector for selected postmaster")
    target = process_identity(matches[0], server["binary"])
    target.update(postmaster_pid=postmaster,
                  database_oid=int(admin.execute(
                      "SELECT oid FROM pg_database WHERE datname=current_database()", value=True)))
    libraries = {str(Path(row.split()[-1]).resolve())
                 for row in Path("/proc/{}/maps".format(target["pid"])).read_text().splitlines()
                 if len(row.split()) >= 6 and (
                     "/libc-" in row or "/libpthread-" in row
                     or row.split()[-1].endswith(("/libc.so.6", "/libpthread.so.0")))}
    # On glibc 2.31 PostgreSQL's recv PLT resolves to libpthread, NOT libc.
    # Attach to both mapped implementations; never silently collect zeros
    # just because a libc-only uprobe attached successfully.
    target["recv_libraries"] = []
    for library in sorted(libraries):
        symbols = subprocess.check_output(["nm", "-D", "--defined-only", library], text=True)
        if any(row.split()[-1].split("@")[0] == "recv" for row in symbols.splitlines() if row.split()):
            target["recv_libraries"].append(library)
    if not target["recv_libraries"]:
        raise RuntimeError("cannot resolve receiver's recv implementations")
    return target


class StatsSentinel:
    def __init__(self, target, server, directory, pg_config):
        from bcc import BPF
        self.target, self.server = target, server
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700)
        self.closed = False
        self.bpf = None
        self.manifest = {
            "status": "attaching", "target": target, "server": server,
            "layout": stats_layout(pg_config), "hooks": ["mapped recv entry", "mapped recv return"],
            "scope": "selected PG12 statistics collector PID/birth + database OID",
            "resident_stacks": False, "resident_query_text": False,
            "per_query_backend_probes": False, "forces_database_reports": False,
            "semantics": "received native PG counters; NOT business TPS or per-query latency",
            "known_loss": "native PG12 UDP reports can be lost before receipt; not measurable here",
        }
        try:
            if self.manifest["layout"]["pg_version_num"] != server["header_layout"]["pg_version_num"]:
                raise RuntimeError("statistics headers differ from selected server inspection")
            if process_identity(target["pid"], server["binary"]) != {
                    "pid": target["pid"], "start_ticks": target["start_ticks"]}:
                raise RuntimeError("receiver identity changed")
            self.bpf = BPF(text=program(self.manifest["layout"], target["database_oid"]))
            atexit.unregister(self.bpf.cleanup)
            for library in target["recv_libraries"]:
                self.bpf.attach_uprobe(name=library, sym="recv", fn_name="enter_recv", pid=target["pid"])
                self.bpf.attach_uretprobe(name=library, sym="recv", fn_name="exit_recv", pid=target["pid"])
            self.bpf["enabled"][ct.c_int(0)] = ct.c_uint(1)
            self.manifest.update(status="ready", started_monotonic=time.monotonic())
            self.output = (self.directory / "windows.jsonl").open("w")
            atomic_json(self.directory / "manifest.json", self.manifest)
        except BaseException as exc:
            self.manifest["error"] = type(exc).__name__ + ": " + str(exc)
            self.close()
            raise

    def snapshot(self):
        if self.closed:
            raise RuntimeError("sentinel is closed")
        identity = process_identity(self.target["pid"], self.server["binary"])
        if identity["start_ticks"] != self.target["start_ticks"]:
            raise RuntimeError("stats receiver PID reused; rediscovery required")
        value = self.bpf["counters"][ct.c_int(0)]
        result = {"mono": time.monotonic(), **{name: int(getattr(value, name)) for name in FIELDS}}
        if result["errors"]:
            raise RuntimeError("eBPF statistics decoding error; no diagnosis with corrupt counters")
        self.output.write(json.dumps(result) + "\n")
        self.output.flush()
        return result

    def close(self):
        if not self.closed:
            try:
                if self.bpf:
                    self.bpf["enabled"][ct.c_int(0)] = ct.c_uint(0)
                    value = self.bpf["counters"][ct.c_int(0)]
                    self.manifest["final_counters"] = {name: int(getattr(value, name)) for name in FIELDS}
                    self.manifest["pending_recv_at_disarm"] = len(self.bpf["inflight"])
                    self.bpf.cleanup()  # owned event handles, also valid after target exit
                self.manifest.update(status="failed" if self.manifest.get("error") else "completed",
                                     all_probes_detached=True,
                                     stopped_monotonic=time.monotonic())
            finally:
                if hasattr(self, "output"):
                    self.output.close()
                self.closed = True
                atomic_json(self.directory / "manifest.json", self.manifest)
        return self.manifest
