"""Pure, testable sentinel thresholds and bounded incident state machine."""
import math
import statistics

RATE_FIELDS = ("commits", "rollbacks", "returned", "fetched", "blocks", "read_us",
               "write_us", "temp_files", "temp_bytes", "deadlocks")
FLOORS = {"commits": 100, "returned": 1000, "fetched": 1000, "blocks": 1000,
          "read_us": 10000, "write_us": 10000}


def rates(before, after):
    elapsed = after["mono"] - before["mono"]
    if elapsed <= 0 or before.get("errors", 0) or after.get("errors", 0):
        raise ValueError("invalid sentinel window")
    result = {"elapsed_seconds": elapsed}
    for name in RATE_FIELDS:
        value = after[name] - before[name]
        if value < 0:
            raise ValueError("sentinel reset; must recalibrate")
        result[name] = value / elapsed
    result["transactions"] = result["commits"] + result["rollbacks"]
    return result


class RateDetector:
    """Resource/demand anomalies, NOT a claim of business-SLO violation.

    A single policy is used for ALL cases; it has no scenario names, pressure
    clock, worker labels, original Prometheus alerts, or SQL snapshots.
    """
    def __init__(self, increase=1.6, decrease=.60, consecutive=2, drop_seconds=10):
        if (not math.isfinite(increase) or increase <= 1 or not 0 < decrease < 1
                or not isinstance(consecutive, int) or consecutive < 2
                or not math.isfinite(drop_seconds) or drop_seconds <= 0):
            raise ValueError("invalid sustained anomaly thresholds")
        self.increase, self.decrease, self.consecutive = increase, decrease, consecutive
        # Database-wide completion drops also occur during idle periods, delayed
        # native reports and short host stalls. Require a longer persistence
        # than a positive measured resource surge; this is still NOT an SLO.
        self.drop_seconds = drop_seconds
        self.baseline = {}
        self.streaks = {}
        self.streak_seconds = {}

    def fit(self, windows):
        if len(windows) < 4:
            raise ValueError("at least four healthy calibration windows required")
        for name in (*FLOORS, "transactions"):
            values = [row[name] for row in windows]
            if not all(math.isfinite(v) and v >= 0 for v in values):
                raise ValueError("invalid calibration counters")
            median = statistics.median(values)
            self.baseline[name] = {"median": median,
                                   "mad": statistics.median(abs(v - median) for v in values)}
        if self.baseline["transactions"]["median"] < 1:
            raise ValueError("no healthy database activity; cannot calibrate silently")
        self.reset()
        return self.baseline

    def reset(self):
        self.streaks = {}
        self.streak_seconds = {}

    def observe(self, window):
        if not self.baseline:
            raise RuntimeError("healthy baseline must precede anomaly detection")
        conditions = {}
        for name, floor in FLOORS.items():
            base = self.baseline[name]
            threshold = max(base["median"] * self.increase, base["median"] + 6 * base["mad"],
                            base["median"] + floor)
            if window[name] > threshold:
                conditions[name + "_surge"] = {
                    "metric": name, "measured_rate": window[name], "threshold": threshold,
                    "baseline": base, "direction": "increase"}
        base = self.baseline["transactions"]
        if base["median"] >= 100 and window["transactions"] < base["median"] * self.decrease:
            conditions["transaction_rate_drop"] = {
                "metric": "transactions", "measured_rate": window["transactions"],
                "threshold": base["median"] * self.decrease, "baseline": base, "direction": "decrease"}
        if window["temp_bytes"] > 1024 * 1024:
            conditions["temporary_file_rate"] = {"metric": "temp_bytes",
                                                 "measured_rate": window["temp_bytes"],
                                                 "threshold": 1024 * 1024}
        if window["deadlocks"] > 0:
            conditions["deadlock"] = {"metric": "deadlocks", "measured_rate": window["deadlocks"],
                                      "threshold": 0}
        for key in set(self.streaks) | set(conditions):
            self.streaks[key] = self.streaks.get(key, 0) + 1 if key in conditions else 0
            self.streak_seconds[key] = (self.streak_seconds.get(key, 0) + window["elapsed_seconds"]
                                        if key in conditions else 0)
        sustained = [dict(value, consecutive_windows=self.streaks[key],
                          sustained_seconds=self.streak_seconds[key], rule=key)
                     for key, value in conditions.items() if self.streaks[key] >= self.consecutive
                     and (key != "transaction_rate_drop" or self.streak_seconds[key] >= self.drop_seconds)]
        if sustained:
            return {"source": "ebpf_native_PG12_received_statistics", "signals": sustained,
                    "classification": "sustained_resource_or_activity_change",
                    "not_claimed": "root cause, business TPS regression, or per-query latency",
                    "window": window}
        return None


class IncidentGate:
    """At most one active burst, bounded count and cooldown, explicit return."""
    def __init__(self, max_incidents=1, cooldown_seconds=30):
        if not isinstance(max_incidents, int) or max_incidents < 1 or cooldown_seconds < 0:
            raise ValueError("invalid incident budget")
        self.max_incidents, self.cooldown = max_incidents, cooldown_seconds
        self.incidents, self.active, self.ready_at = 0, False, 0
        self.state = "resident"

    def start(self, now):
        if self.active or now < self.ready_at or self.incidents >= self.max_incidents:
            return False
        self.incidents += 1
        self.active, self.state = True, "burst"
        return True

    def diagnosis(self):
        if not self.active or self.state != "burst":
            raise RuntimeError("diagnosis requires an active captured incident")
        self.state = "diagnosis"

    def finish(self, now):
        if not self.active:
            raise RuntimeError("no active incident")
        self.active, self.state, self.ready_at = False, "resident", now + self.cooldown
