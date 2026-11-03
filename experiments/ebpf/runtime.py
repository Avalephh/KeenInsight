"""Bounded collector subprocesses and incrementally consumed event streams."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

REPOSITORY = Path(__file__).resolve().parents[2]


class EventTail:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.pending = ""

    def poll(self):
        if not self.path.exists():
            return []
        with self.path.open() as handle:
            handle.seek(self.offset)
            self.pending += handle.read()
            self.offset = handle.tell()
        lines = self.pending.split("\n")
        self.pending = lines.pop()
        return [json.loads(line) for line in lines if line.strip()]


class CollectorProcess:
    def __init__(self, directory, pid, pg_config, scope, profile, seconds,
                 max_mb=8, capture_sql=False, report_seconds=.1):
        self.directory = Path(directory)
        self.log = self.directory.with_suffix(".log").open("w")
        args = [sys.executable, "-m", "experiments.ebpf", "collect",
                "--pid", str(pid), "--pg-config", pg_config,
                "--output", str(self.directory), "--scope", scope, "--profile", profile,
                "--detail-level", "lightweight", "--duration", str(seconds),
                "--max-output-mb", str(max_mb), "--report-seconds", str(report_seconds),
                "--collector-cpu-budget", "100000"]
        if scope == "query":
            args += ["--query-output", "all"]
        if capture_sql:
            args += ["--capture-sql"]
        self.process = subprocess.Popen(args, cwd=str(REPOSITORY), stdout=self.log,
                                        stderr=subprocess.STDOUT)
        self.events = EventTail(self.directory / "events.jsonl")
        self.closed = False
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                manifest = self.manifest()
                if manifest.get("status") == "ready":
                    self.started = time.monotonic()
                    return
                if self.process.poll() is not None or manifest.get("status") == "failed":
                    break
                time.sleep(.025)
            raise RuntimeError("eBPF collector not ready; inspect " + self.log.name)
        except BaseException:
            self.close(check=False)
            raise

    def manifest(self):
        path = self.directory / "manifest.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def close(self, check=True):
        if not self.closed:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise RuntimeError("collector failed to stop; killed; inspect " + self.log.name)
            finally:
                self.log.close()
                self.closed = True
        manifest = self.manifest()
        if check and (self.process.returncode or manifest.get("status") != "completed"):
            raise RuntimeError("collector failed; inspect " + self.log.name)
        return manifest
