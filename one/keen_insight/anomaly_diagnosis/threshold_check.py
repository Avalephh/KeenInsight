"""阈值检查。"""

from __future__ import annotations

# Default thresholds per scene (can be extended)
_DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "sysbench": {
        "cpu_mean": 90.0,
        "mem_mean": 90.0,
        "io_mean": 80.0,
        "tps_min": 100.0,   # below this is anomalous
        "lat_max": 500.0,   # above this is anomalous (ms)
    },
    "tpcc": {
        "cpu_mean": 90.0,
        "mem_mean": 90.0,
        "io_mean": 80.0,
        "tps_min": 50.0,
        "lat_max": 1000.0,
    },
    "tpch": {
        "cpu_mean": 95.0,
        "mem_mean": 90.0,
        "io_mean": 90.0,
    },
    "pgbench": {
        "cpu_mean": 95.0,
        "mem_mean": 95.0,
        "io_mean": 90.0,
        "tps_min": 7000.0,   # below this is anomalous (normal ~8000+ TPS)
        "lat_max": 5.0,      # above this is anomalous (ms, normal ~3.5ms)
    },
    "chbenchmark": {
        "cpu_mean": 95.0,
        "mem_mean": 95.0,
        "io_mean": 90.0,
        "tps_min": 30.0,     # below this is anomalous (normal ~44 TPS)
        "lat_max": 500.0,    # above this is anomalous (ms, normal ~225ms)
    },
    "default": {
        "cpu_mean": 90.0,
        "mem_mean": 90.0,
        "io_mean": 80.0,
    },
}


class ThresholdCheck:
    """对窗口化系统指标做快速阈值筛查。"""

    def __init__(self, scene: str = "default") -> None:
        self.scene = scene
        self._thresholds: dict[str, float] = self.load_threshold_profile(scene)

    def load_threshold_profile(self, scene: str) -> dict[str, float]:
        """加载指定场景下的阈值配置。"""
        return dict(
            _DEFAULT_THRESHOLDS.get(scene, _DEFAULT_THRESHOLDS["default"])
        )

    def check_system_thresholds(
        self, window_metrics: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """对每个窗口摘要检测是否超过阈值，返回触发异常的条目列表。

        每条返回记录包含：
        - window_idx: 窗口序号
        - metric: 触发的指标名
        - value: 实测值
        - threshold: 阈值
        - direction: 'high' | 'low'
        """
        violations: list[dict[str, object]] = []
        for idx, win in enumerate(window_metrics):
            for metric, threshold in self._thresholds.items():
                if metric not in win:
                    continue
                value = float(win[metric])  # type: ignore[arg-type]
                # Metrics ending with _min are lower-bound checks
                if metric.endswith("_min"):
                    if value < threshold:
                        violations.append({
                            "window_idx": idx,
                            "metric": metric,
                            "value": value,
                            "threshold": threshold,
                            "direction": "low",
                        })
                else:
                    if value > threshold:
                        violations.append({
                            "window_idx": idx,
                            "metric": metric,
                            "value": value,
                            "threshold": threshold,
                            "direction": "high",
                        })
        return violations
