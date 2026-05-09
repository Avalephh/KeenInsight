"""主流程装配定义。"""

from __future__ import annotations

from .anomaly_diagnosis import (
    AnomalyDiagnosisCoordinator,
    DiagnosisRules,
    DifferentialProfiling,
    RootCause,
    RootCauseLocator,
    MulticlassScoringModel,
    SHAPModel,
    SlowQueryPrediction,
    ThresholdCheck,
    WindowedSystemPerformance,
)
from .anomaly_resolution import (
    AnomalyResolutionCoordinator,
    CostModel,
    SQLKnowledgeRetrieval,
    SQLOptimization,
    SyntaxSemanticValidation,
    KnowledgeRetrieval,
    SystemKnobTuning,
    TargetKnobSelection,
)
from .knowledge_base import (
    AgentAnalysis,
    ClusteringAnalysis,
    CodeRetrievalAgent,
    DataFlowControlFlow,
    DBSourceCode,
    HistoricalTuningRecords,
    ProcessingPipeline,
    LLVMAnalysis,
    Manuals,
    MultiSourceKnowledgeBase,
    StructuredRepresentation,
    TuningExperience,
    Summarization,
)
from .models import KnowledgeQuery, PipelineContext
from .system_monitoring import (
    AdaptiveMessageGeneration,
    AdaptiveAttachment,
    DBEvents,
    DBExecutionFunctions,
    KernelEvents,
    Kprobes,
    KernelResourceFunctions,
    SQLLifecycleReconstruction,
    SystemMonitoringCoordinator,
    Uprobes,
)


class KeenInsightPipeline:
    """KeenInsight 总流程装配类。"""

    def __init__(
        self,
        monitoring: SystemMonitoringCoordinator,
        diagnosis: AnomalyDiagnosisCoordinator,
        knowledge_base: MultiSourceKnowledgeBase,
        resolution: AnomalyResolutionCoordinator,
    ) -> None:
        self.monitoring = monitoring
        self.diagnosis = diagnosis
        self.knowledge_base = knowledge_base
        self.resolution = resolution

    def run_once(self, workload_profile: dict[str, object]) -> PipelineContext:
        """执行一次完整主流程。

        workload_profile 支持以下 key：
        - snapshot (MonitoringSnapshot): 已采集的监控快照；若缺失则创建空快照
        - sql_lifecycles (list[SQLLifecycle]): SQL 生命周期列表
        """
        from .models import MonitoringSnapshot, SQLLifecycle

        snapshot = workload_profile.get("snapshot") or MonitoringSnapshot(
            db_metrics=workload_profile.get("db_metrics", {}),
            system_metrics=workload_profile.get("system_metrics", {}),
        )
        sql_lifecycles = workload_profile.get("sql_lifecycles", [])

        ctx = PipelineContext(snapshot=snapshot, sql_lifecycles=sql_lifecycles)

        # Step 1-3: Diagnose
        ctx.diagnosis = self.diagnosis.diagnose(snapshot, sql_lifecycles)

        # Step 4: Build / retrieve knowledge
        for diag in ctx.diagnosis:
            query = KnowledgeQuery(
                category=diag.category,
                problem=diag.summary,
                filters={"root_causes": diag.root_causes},
            )
            ctx.knowledge.extend(self.knowledge_base.retrieve(query))

        # Step 5: Resolve
        ctx.resolution_plans = self.resolution.resolve(ctx.diagnosis, ctx.knowledge)

        return ctx

    def warmup_knowledge(self) -> None:
        """预热多源知识库（当前为空操作）。"""
        pass


def build_default_pipeline() -> KeenInsightPipeline:
    """构建默认装配版本的论文框架。"""

    monitoring = SystemMonitoringCoordinator(
        adaptive_attachment=AdaptiveAttachment(),
        user_probe_manager=Uprobes(),
        db_function_collector=DBExecutionFunctions(),
        db_event_collector=DBEvents(),
        kprobes=Kprobes(),
        kernel_resource_collector=KernelResourceFunctions(),
        kernel_events=KernelEvents(),
        message_generator=AdaptiveMessageGeneration(),
        sql_reconstructor=SQLLifecycleReconstruction(),
    )

    diagnosis = AnomalyDiagnosisCoordinator(
        system_analyzer=WindowedSystemPerformance(),
        threshold_check=ThresholdCheck(),
        differential_profiling=DifferentialProfiling(),
        shap_interpreter=SHAPModel(),
        root_cause=RootCause(),
        slow_query_prediction=SlowQueryPrediction(),
        rule_engine=DiagnosisRules(),
        scoring_model=MulticlassScoringModel(),
        root_cause_locator=RootCauseLocator(),
    )

    knowledge_base = MultiSourceKnowledgeBase(
        record_manager=HistoricalTuningRecords(),
        record_analyzer=AgentAnalysis(),
        pattern_summarizer=Summarization(),
        cluster_engine=ClusteringAnalysis(),
        tuning_repository=TuningExperience(),
        source_repository=DBSourceCode(),
        llvm_engine=LLVMAnalysis(),
        data_flow_control_flow=DataFlowControlFlow(),
        code_agent=CodeRetrievalAgent(),
        manuals=Manuals(),
        processing_pipeline=ProcessingPipeline(),
        structured_builder=StructuredRepresentation(),
    )

    resolution = AnomalyResolutionCoordinator(
        system_target_selector=TargetKnobSelection(),
        knowledge_retrieval=KnowledgeRetrieval(),
        system_knob_tuning=SystemKnobTuning(),
        sql_knowledge_retriever=SQLKnowledgeRetrieval(),
        sql_optimizer=SQLOptimization(),
        validator=SyntaxSemanticValidation(),
        cost_model=CostModel(),
    )

    return KeenInsightPipeline(
        monitoring=monitoring,
        diagnosis=diagnosis,
        knowledge_base=knowledge_base,
        resolution=resolution,
    )
