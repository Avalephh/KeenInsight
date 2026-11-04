"""Supervised ORIGINAL perf capture: PID/time/bytes bounds and owned cleanup.

The original capture/postprocessor runs unchanged in a dedicated process group.
The supervisor can interrupt perf AND its sleep/postprocessing children on a
budget/cancellation. RLIMIT_FSIZE is a per-file hard limit; aggregate output is
checked every 50ms (so its overshoot is NOT a strict byte-exact disk quota).
"""
import argparse
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

from .collector import atomic_json, process_identity

ROOT = Path(__file__).resolve().parents[2]


class CaptureBudgetExceeded(RuntimeError):
    pass


def output_bytes(directory):
    return sum(path.stat().st_size for path in Path(directory).rglob("*") if path.is_file())


def living_group_members(pgid):
    members = []
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            stat = path.read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
            members.append(int(path.parent.name))
    return members


def stop_group(process, grace=2):
    """Never signal a caller's group, postmaster, or any unowned perf session."""
    if process.pid == os.getpgrp():
        raise RuntimeError("refusing to stop caller's process group")
    # Even if the leader exited, its perf/sleep children can still own this PGID.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if not living_group_members(process.pid):
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        # A dead leader does not prove the group has no remaining children.
        time.sleep(.05)
    process.wait(timeout=grace)
    deadline = time.monotonic() + grace
    while living_group_members(process.pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("owned perf process group still has living members after SIGKILL")
        time.sleep(.05)


def supervised(command, directory, env, deadline_seconds, max_bytes, manifest):
    """Shared supervisor, deliberately injectable for cancellation/budget tests."""
    directory = Path(directory)
    started = time.monotonic()
    process = None
    status = "failed"
    reason = None

    def limit():
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    try:
        with (directory / "supervisor.log").open("w") as out:
            process = subprocess.Popen(command, cwd=str(ROOT), env=env, stdout=out,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       preexec_fn=limit)
            manifest.update(status="running", owned_process_group=process.pid)
            atomic_json(directory / "supervisor.json", manifest)
            while process.poll() is None:
                if time.monotonic() - started > deadline_seconds:
                    reason = "wall_clock_budget"
                    raise CaptureBudgetExceeded("perf/postprocess wall-clock budget exceeded")
                if output_bytes(directory) > max_bytes:
                    reason = "aggregate_output_budget"
                    raise CaptureBudgetExceeded("perf/postprocess aggregate output budget exceeded")
                time.sleep(.05)
            if output_bytes(directory) > max_bytes:
                reason = "aggregate_output_budget"
                raise CaptureBudgetExceeded("perf/postprocess output budget exceeded")
            if process.returncode:
                reason = "child_failed_or_file_limit"
                raise RuntimeError("original perf worker failed ({})".format(process.returncode))
            status = "completed"
    except BaseException:
        reason = reason or "failure_or_cancellation"
        raise
    finally:
        cleanup_error = None
        try:
            if process:
                stop_group(process)
        except Exception as exc:
            cleanup_error = type(exc).__name__ + ": " + str(exc)
        manifest.update(status="failed" if cleanup_error else status, reason=reason,
                        all_owned_processes_stopped=cleanup_error is None,
                        cleanup_error=cleanup_error,
                        elapsed_seconds=time.monotonic() - started,
                        output_bytes=output_bytes(directory))
        atomic_json(directory / "supervisor.json", manifest)
        if cleanup_error:
            raise RuntimeError("perf process cleanup was not confirmed: " + cleanup_error)


def capture(directory, pids, server, seconds, frequency, stackcollapse,
            max_mb=128, max_pids=16, grace_seconds=30):
    if (not isinstance(seconds, int) or not 1 <= seconds <= 60
            or not isinstance(frequency, int) or not 1 <= frequency <= 1000
            or not 1 <= len(set(pids)) == len(pids) <= max_pids <= 32
            or not 1 <= max_mb <= 512 or not 1 <= grace_seconds <= 120):
        raise ValueError("capture exceeds explicit PID/time/frequency/output bounds")
    stackcollapse = str(Path(stackcollapse).resolve(strict=True))
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    targets = [process_identity(pid, server["binary"]) for pid in pids]
    request = {"pids": pids, "seconds": seconds, "frequency": frequency,
               "stackcollapse": stackcollapse, "targets": targets, "binary": server["binary"]}
    atomic_json(directory / "request.json", request)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), str(ROOT / "app"),
                                                      env.get("PYTHONPATH"))))
    manifest = {"status": "starting", "targets": targets, "seconds": seconds,
                "frequency": frequency, "max_output_bytes": int(max_mb * 1024 ** 2),
                "wall_clock_budget_seconds": seconds + grace_seconds,
                "original_capture": "sysinsight_live_chain._capture_perf",
                "aggregate_budget_check_seconds": .05, "per_file_RLIMIT_FSIZE": True}
    supervised([sys.executable, "-m", "experiments.ebpf.hybrid_perf",
                "--worker", str(directory.resolve())], directory, env,
               seconds + grace_seconds, manifest["max_output_bytes"], manifest)
    result = json.loads((directory / "capture.json").read_text())
    if (result["postprocess"].get("status") != "sysinsight_counts_generated"
            or result["perf"].get("returncode") != 0):
        raise RuntimeError("original perf capture or original function extraction incomplete")
    return {"status": "completed", "folded_path": result["postprocess"]["folded_path"],
            "counts_path": result["postprocess"]["counts_path"],
            "sample_event": "original perf hardware cycles", "targets": targets,
            "requested_seconds": seconds, "capture": result, "supervisor": manifest,
            "all_perf_processes_stopped": True}


def worker(directory):
    sys.path.insert(0, str(ROOT / "app"))
    from sysinsight_live_chain import _capture_perf
    directory = Path(directory)
    request = json.loads((directory / "request.json").read_text())
    for target in request["targets"]:
        if process_identity(target["pid"], request["binary"]) != target:
            raise RuntimeError("target identity changed before perf attachment")
    result = _capture_perf(directory, request["pids"], request["seconds"], request["frequency"],
                           request["stackcollapse"], os.environ.copy())
    atomic_json(directory / "capture.json", result)
    for target in request["targets"]:
        if process_identity(target["pid"], request["binary"]) != target:
            raise RuntimeError("target exited or identity changed during original perf window")
    if (result["postprocess"].get("status") != "sysinsight_counts_generated"
            or result["perf"].get("returncode") != 0):
        raise RuntimeError("original capture returned incomplete evidence")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    worker(parser.parse_args().worker)
