"""Reusable eBPF alarm -> bounded rich capture -> original diagnosis controller."""
import time

from .hybrid_policy import IncidentGate, rates


class HybridController:
    def __init__(self, detector, journal, max_incidents=1, cooldown_seconds=30, clock=None):
        self.detector, self.journal = detector, journal
        self.gate = IncidentGate(max_incidents, cooldown_seconds)
        self.clock = clock or time.monotonic

    def observe(self, before, after, resolve_targets, capture_rich, diagnose=None):
        """Only BPF snapshot rates can trigger this function's heavy branch.

        Control-plane discovery, rich SQL, perf and the model are callbacks
        invoked AFTER the gate opens. Neither a scenario label nor a pressure
        start time is a detector input. Capture implementations must enforce
        their own PID/time/bytes bounds even when diagnosis fails.
        """
        window = rates(before, after)
        alert = self.detector.observe(window)
        if not alert or not self.gate.start(self.clock()):
            return None
        alert["observed_at_unix_ns"] = time.time_ns()
        self.journal.stage("ebpf_alert", alert=alert)
        try:
            targets = resolve_targets(alert)
            self.journal.stage("perf_burst_started", targets=targets,
                               incident=self.gate.incidents)
            bundle = capture_rich(targets)
            if not bundle.get("profile", {}).get("all_perf_processes_stopped"):
                raise RuntimeError("capture did not confirm perf cleanup; refusing diagnosis")
            self.journal.stage("perf_burst_stopped", all_perf_processes_stopped=True,
                               evidence=bundle.get("counts"))
            self.gate.diagnosis()
            result = diagnose(bundle, alert) if diagnose else None
            self.journal.stage("diagnosis_completed", executed=diagnose is not None)
            return {"alert": alert, "bundle": bundle, "diagnosis": result}
        except BaseException as exc:
            self.journal.stage("incident_aborted", reason=type(exc).__name__ + ": " + str(exc))
            raise
        finally:
            self.gate.finish(self.clock())
            self.detector.reset()
            self.journal.stage("resident_resumed", cooldown_until=self.gate.ready_at,
                               incidents_used=self.gate.incidents,
                               incident_budget=self.gate.max_incidents)
