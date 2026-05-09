"""窗口化系统性能分析。"""

from __future__ import annotations

from ..models import MonitoringSnapshot


class WindowedSystemPerformance:
    """将监控快照按时间窗口切分并聚合统计特征。"""

    def __init__(self, window_size: int = 10) -> None:
        # window_size: number of metric samples per window
        self.window_size = window_size

    def build_windows(
        self, snapshot: MonitoringSnapshot
    ) -> list[dict[str, object]]:
        """将 snapshot.system_metrics 中的时序数据切分为固定大小的窗口。

        system_metrics 支持两种格式：
        - dict[str, list[float]]  — 每个 key 是指标名，value 是时序列表
        - list[dict[str, float]]  — 每个元素是一个时间点的所有指标
        """
        metrics = snapshot.system_metrics

        # Normalise to list-of-dicts
        if isinstance(metrics, dict):
            if not metrics:
                return []
            # Convert {metric: [v0, v1, ...]} → [{metric: v0, ...}, ...]
            keys = list(metrics.keys())
            length = max(len(v) for v in metrics.values() if isinstance(v, list))
            rows: list[dict[str, object]] = []
            for i in range(length):
                row: dict[str, object] = {}
                for k in keys:
                    v = metrics[k]
                    row[k] = v[i] if isinstance(v, list) and i < len(v) else v
                rows.append(row)
        elif isinstance(metrics, list):
            rows = metrics  # type: ignore[assignment]
        else:
            return []

        windows: list[dict[str, object]] = []
        for start in range(0, len(rows), self.window_size):
            chunk = rows[start : start + self.window_size]
            windows.append({"start_idx": start, "samples": chunk})
        return windows

    def summarize_window_metrics(
        self, windows: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """对每个窗口计算均值、最大值、最小值。"""
        summaries: list[dict[str, object]] = []
        for win in windows:
            samples: list[dict[str, object]] = win.get("samples", [])
            if not samples:
                continue
            keys = {k for s in samples for k in s if isinstance(s.get(k), (int, float))}
            summary: dict[str, object] = {"start_idx": win.get("start_idx", 0)}
            for k in keys:
                vals = [float(s[k]) for s in samples if isinstance(s.get(k), (int, float))]
                if vals:
                    summary[f"{k}_mean"] = sum(vals) / len(vals)
                    summary[f"{k}_max"] = max(vals)
                    summary[f"{k}_min"] = min(vals)
            summaries.append(summary)
        return summaries
