"""异常诊断总协调器。"""

from __future__ import annotations

from ..models import DiagnosisResult, MonitoringSnapshot, SQLLifecycle
from .diagnosis_rules import DiagnosisRules
from .differential_profiling import DifferentialProfiling
from .root_cause import RootCause
from .root_cause_locator import RootCauseLocator
from .multiclass_scoring_model import MulticlassScoringModel
from .shap_model import SHAPModel
from .slow_query_prediction import SlowQueryPrediction
from .threshold_check import ThresholdCheck
from .windowed_system_performance import WindowedSystemPerformance


class AnomalyDiagnosisCoordinator:
    """异常诊断总协调器。"""

    def __init__(
        self,
        system_analyzer: WindowedSystemPerformance,
        threshold_check: ThresholdCheck,
        differential_profiling: DifferentialProfiling,
        shap_interpreter: SHAPModel,
        root_cause: RootCause,
        slow_query_prediction: SlowQueryPrediction,
        rule_engine: DiagnosisRules,
        scoring_model: MulticlassScoringModel,
        root_cause_locator: RootCauseLocator,
    ) -> None:
        self.system_analyzer = system_analyzer
        self.threshold_check = threshold_check
        self.differential_profiling = differential_profiling
        self.shap_interpreter = shap_interpreter
        self.root_cause = root_cause
        self.slow_query_prediction = slow_query_prediction
        self.rule_engine = rule_engine
        self.scoring_model = scoring_model
        self.root_cause_locator = root_cause_locator

    def diagnose_system_level(
        self, snapshot: MonitoringSnapshot
    ) -> DiagnosisResult:
        """执行系统级异常诊断。

        流程：
        1. 窗口化系统指标 → 阈值检测
        2. 差分剖析 + SHAP 解释
        3. 热点检测 → 规则评估 → 打分 → 根因定位
        """
        # Step 1: windowed metrics + threshold check
        windows = self.system_analyzer.build_windows(snapshot)
        window_summaries = self.system_analyzer.summarize_window_metrics(windows)
        threshold_alerts = self.threshold_check.check_system_thresholds(window_summaries)

        # Step 2: differential profiling (requires baseline/abnormal paths in db_metrics)
        diff_result: dict[str, object] = {}
        shap_features: list[dict[str, object]] = []

        baseline_profile = snapshot.db_metrics.get("baseline_profile")
        abnormal_profile = snapshot.db_metrics.get("abnormal_profile")
        if baseline_profile and abnormal_profile:
            diff_result = self.differential_profiling.compare_profiles(
                baseline_profile, abnormal_profile
            )

        shap_input = snapshot.db_metrics.get("shap_input")
        if shap_input:
            try:
                shap_features = self.shap_interpreter.explain_system_features(shap_input)
            except Exception as exc:
                shap_features = []
                print(f"[WARN] SHAP explain failed: {exc}")

        # Step 3: hotspot detection → rules → scoring → root cause
        hotspots = self.root_cause.detect_system_hotspots(threshold_alerts, shap_features)
        rules = self.rule_engine.evaluate_system_rules(snapshot, hotspots)
        scored = self.scoring_model.score_system_root_causes(hotspots, rules, shap_features)
        return self.root_cause_locator.locate_system_root_cause(scored)

    def diagnose_sql_level(
        self,
        snapshot: MonitoringSnapshot,
        sql_lifecycles: list[SQLLifecycle],
    ) -> DiagnosisResult:
        """执行 SQL 级异常诊断（预留接口，当前返回空结论）。"""
        return self.root_cause_locator.locate_sql_root_cause([])

    def diagnose(
        self,
        snapshot: MonitoringSnapshot,
        sql_lifecycles: list[SQLLifecycle],
    ) -> list[DiagnosisResult]:
        """统一执行全部异常诊断流程。"""
        results: list[DiagnosisResult] = []
        results.append(self.diagnose_system_level(snapshot))
        results.append(self.diagnose_sql_level(snapshot, sql_lifecycles))
        return results
