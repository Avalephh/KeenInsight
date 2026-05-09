"""差分剖析接口 — 委托给顶层 DifferentialPofiling 实现。"""

from __future__ import annotations

import sys
import os

# Ensure project root is importable regardless of how the package is loaded
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from DifferentialPofiling import DifferentialProfiling as _RealDP  # noqa: E402


class DifferentialProfiling:
    """差分剖析器 — 委托给 DifferentialPofiling.DifferentialProfiling。"""

    def __init__(self) -> None:
        self._impl = _RealDP()

    def compare_profiles(
        self,
        baseline_profile: dict[str, object],
        abnormal_profile: dict[str, object],
    ) -> dict[str, object]:
        """生成差分剖析结果。"""
        return self._impl.compare_profiles(baseline_profile, abnormal_profile)

    def rank_changed_features(
        self, differential_profile: dict[str, object]
    ) -> list[dict[str, object]]:
        """对变化显著的特征排序。"""
        return self._impl.rank_changed_features(differential_profile)
