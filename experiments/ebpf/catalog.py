"""Explicit probe allowlist. IDs are part of the on-disk event schema."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Probe:
    id: int
    name: str
    kind: str
    symbol: str
    value: str
    bootstrap_priority: float


PROBES = (
    Probe(0, "vfs_read", "kernel", "vfs_read", "returned_bytes", 1.0),
    Probe(1, "vfs_write", "kernel", "vfs_write", "returned_bytes", 1.0),
    Probe(2, "tcp_send", "kernel", "tcp_sendmsg", "returned_bytes", .2),
    Probe(3, "tcp_receive", "kernel", "tcp_recvmsg", "returned_bytes", .2),
    Probe(4, "allocation", "user", "MemoryContextAlloc", "requested_bytes", .2),
    Probe(5, "temp_file_write", "user", "BufFileWrite", "calls", .8),
    Probe(6, "hash_spill", "user", "ExecHashJoinSaveTuple", "calls", .9),
    Probe(7, "lock_acquire", "user", "LockAcquireExtended", "calls", .7),
    Probe(8, "wal_flush", "user", "XLogFlush", "calls", .8),
    Probe(9, "temp_file_create", "user", "BufFileCreateTemp", "calls", 1.0),
    Probe(10, "temp_file_read", "user", "BufFileRead", "calls", .9),
    Probe(11, "lock_wait", "user", "ProcSleep", "calls", 1.0),
    Probe(12, "checkpoint", "user", "CreateCheckPoint", "calls", 1.0),
    Probe(13, "wal_sync", "user", "pg_fdatasync", "calls", .8),
)
BY_NAME = {p.name: p for p in PROBES}
CORE = ("vfs_read", "vfs_write")
BOOTSTRAP = CORE + ("temp_file_write", "hash_spill", "lock_acquire", "wal_flush")
SCHEMA = "keeninsight.ebpf.v2"


def choose_probes(profile, model=None):
    if profile == "diagnostic-system":
        return ["vfs_read", "vfs_write", "lock_wait", "wal_flush", "wal_sync",
                "checkpoint", "temp_file_read", "hash_spill"], "bounded_system_parity_diagnosis"
    if profile == "six-resident":
        return ["temp_file_create", "lock_wait", "checkpoint"], \
               "semantic_wait_checkpoint_spill_sentinels_not_complete_CPU_detection"
    if profile == "diagnostic-lock":
        return ["lock_wait", "wal_flush"], "bounded_actual_lock_wait_and_WAL"
    if profile == "diagnostic-wal":
        return ["checkpoint", "wal_flush", "wal_sync"], "bounded_WAL_checkpoint_diagnosis"
    if profile == "resident":
        # The first closed-loop incident is temporary-file materialization.
        # Do not keep commit/WAL or per-tuple paths attached for this incident.
        return ["temp_file_create"], "resident_temporary_file_sentinel"
    if profile == "diagnostic":
        return ["temp_file_create", "temp_file_read", "hash_spill", "lock_acquire"], \
               "bounded_database_function_diagnosis"
    if profile == "boundary":
        return [], "executor_boundary_control_not_full_observability"
    if profile == "events":
        if model is None:
            return ["temp_file_create", "wal_flush"], "low_fanout_bootstrap_not_lasso"
        # Validate exactly as for the legacy profile, but do not pin the
        # machine-wide VFS hooks in an always-on lightweight collection.
        choose_probes("selected", model)
        names = ["temp_file_create"] + [
            p.name for p in PROBES if p.name != "temp_file_create"
            and abs(float(model["coefficients"].get(p.name, 0))) > 1e-12]
        return names, "lasso_lightweight"
    if profile == "core":
        return list(CORE), "core"
    if profile == "all":
        return [p.name for p in PROBES], "training_all_candidates"
    if profile != "selected":
        raise ValueError("unknown profile")
    if model is None:
        return list(BOOTSTRAP), "untrained_bootstrap_not_lasso"
    if model.get("schema") != "keeninsight.lasso.v1":
        raise ValueError("unsupported selector model")
    weights = model.get("coefficients", {})
    if set(weights) - set(BY_NAME):
        raise ValueError("selector contains unknown probe names")
    if not all(math.isfinite(float(value)) for value in weights.values()):
        raise ValueError("selector coefficients must be finite")
    return list(CORE) + [p.name for p in PROBES if p.name not in CORE
                        and abs(float(weights.get(p.name, 0))) > 1e-12], "lasso"


def prune_candidate(attached, model=None, protected=CORE):
    weights = model["coefficients"] if model else {}
    optional = [BY_NAME[n] for n in attached if n not in protected]
    return min(optional, key=lambda p: (abs(float(weights.get(p.name, 0)))
               if model else p.bootstrap_priority, p.id)).name if optional else None
