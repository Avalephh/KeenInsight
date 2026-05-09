"""系统参数调优 — 根据候选 knob 生成调优方案。"""

from __future__ import annotations

import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import KNOB_CONFIG_FILE  # noqa: E402
from ..models import DiagnosisResult, KnowledgeEntry, ResolutionPlan

# PostgreSQL-specific tuning recommendations
_PG_KNOB_RECOMMENDATIONS = {
    "work_mem": {
        "increase_factor": 4.0,
        "description": "排序/哈希操作内存不足，增加 work_mem 可以减少磁盘溢出",
        "current_advice": "建议从当前值增加 4 倍（1MB → 64MB）"
    },
    "shared_buffers": {
        "increase_factor": 8.0,
        "description": "共享缓冲区过小会增加磁盘 I/O，命中率低",
        "current_advice": "建议设置为总内存的 25%（32MB → 2GB）"
    },
    "effective_cache_size": {
        "increase_factor": 4.0,
        "description": "查询规划器缓存假设，影响索引选择",
        "current_advice": "建议设置为总内存的 75%（512MB → 20GB）"
    },
    "maintenance_work_mem": {
        "increase_factor": 8.0,
        "description": "维护操作（VACUUM/INDEX BUILD）内存不足",
        "current_advice": "建议增加维护操作内存（16MB → 512MB）"
    },
    "max_wal_size": {
        "increase_factor": 4.0,
        "description": "WAL 写入频繁，增加此值可以减少检查点频率",
        "current_advice": "建议增加检查点间隔（256MB → 4GB）"
    },
    "checkpoint_completion_target": {
        "increase_factor": 1.0,
        "description": "检查点完成目标，增加可分散写入压力",
        "current_advice": "建议从 0.5 增加到 0.9",
        "custom_value": "0.9"
    },
    "random_page_cost": {
        "increase_factor": 1.0,
        "description": "随机页访问成本，影响索引使用决策",
        "current_advice": "建议从 4 降低到 1.1（SSD）",
        "custom_value": "1.1"
    },
    "effective_io_concurrency": {
        "increase_factor": 1.0,
        "description": "并发 I/O 操作数，SSD 应设置为 200",
        "current_advice": "建议从 1 增加到 200",
        "custom_value": "200"
    },
    "wal_buffers": {
        "increase_factor": 2.0,
        "description": "WAL 缓冲区不足",
        "current_advice": "建议从当前值增加"
    },
}


def _load_knob_defaults(knob_config_file: str) -> dict[str, object]:
    """从 mysql_knobs.json 读取每个 knob 的默认值和范围。"""
    import json
    try:
        with open(knob_config_file) as f:
            raw = json.load(f)
    except Exception:
        return {}
    # Support both list and dict formats
    if isinstance(raw, list):
        return {item["name"]: item for item in raw if isinstance(item, dict) and "name" in item}
    if isinstance(raw, dict):
        return raw
    return {}


class SystemKnobTuning:
    """系统参数调优器。"""

    def __init__(self, knob_config_file: str | None = None) -> None:
        path = knob_config_file or KNOB_CONFIG_FILE
        self._knob_meta = _load_knob_defaults(path)
        self._pg_recs = _PG_KNOB_RECOMMENDATIONS

    def generate_plan(
        self,
        diagnosis: DiagnosisResult,
        target_knobs: list[str],
        knowledge: list[KnowledgeEntry],
    ) -> ResolutionPlan:
        """为每个目标 knob 生成一条调优动作描述。"""
        actions = self.enumerate_actions(target_knobs)
        return ResolutionPlan(
            plan_type="system_knob_tuning",
            target=", ".join(target_knobs) if target_knobs else "none",
            actions=actions,
            validation_steps=[
                "Re-run workload benchmark and compare TPS/latency.",
                "Check error log for restart failures.",
                "Monitor cache hit ratio with: SELECT sum(blks_hit)*100.0/sum(blks_hit+blks_read) FROM pg_stat_database;",
            ],
        )

    def enumerate_actions(self, target_knobs: list[str]) -> list[str]:
        """为每个 knob 生成建议调整描述。"""
        actions: list[str] = []
        for knob in target_knobs:
            # Check for PostgreSQL-specific recommendations first
            pg_rec = self._pg_recs.get(knob)
            if pg_rec:
                actions.append(
                    f"Tune '{knob}': {pg_rec['description']}. "
                    f"{pg_rec['current_advice']}."
                )
            else:
                # Fall back to MySQL knob config
                meta = self._knob_meta.get(knob)
                if isinstance(meta, dict):
                    min_v = meta.get("min")
                    max_v = meta.get("max")
                    default = meta.get("default")
                    actions.append(
                        f"Tune '{knob}': range=[{min_v}, {max_v}], default={default}."
                    )
                else:
                    actions.append(f"Tune '{knob}': consult documentation for safe range.")
        return actions

    def get_recommended_value(self, knob: str, current_value: float) -> float | None:
        """根据推荐因子计算目标 knob 的推荐值。"""
        pg_rec = self._pg_recs.get(knob)
        if not pg_rec:
            return None

        # 如果有自定义值，使用自定义值
        if "custom_value" in pg_rec:
            try:
                return float(pg_rec["custom_value"])
            except (ValueError, TypeError):
                pass

        # 否则使用增长因子
        factor = pg_rec.get("increase_factor", 1.0)
        return current_value * factor