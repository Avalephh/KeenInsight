"""异常处置总协调器。"""

from __future__ import annotations

from ..models import DiagnosisResult, KnowledgeEntry, ResolutionPlan
from .cost_model import CostModel
from .sql_knowledge_retrieval import SQLKnowledgeRetrieval
from .sql_optimization import SQLOptimization
from .syntax_semantic_validation import SyntaxSemanticValidation
from .system_knob_tuning import SystemKnobTuning
from .knowledge_retrieval import KnowledgeRetrieval
from .target_knob_selection import TargetKnobSelection


class AnomalyResolutionCoordinator:
    """异常处置总协调器。"""

    def __init__(
        self,
        system_target_selector: TargetKnobSelection,
        knowledge_retrieval: KnowledgeRetrieval,
        system_knob_tuning: SystemKnobTuning,
        sql_knowledge_retriever: SQLKnowledgeRetrieval,
        sql_optimizer: SQLOptimization,
        validator: SyntaxSemanticValidation,
        cost_model: CostModel,
    ) -> None:
        self.system_target_selector = system_target_selector
        self.knowledge_retrieval = knowledge_retrieval
        self.system_knob_tuning = system_knob_tuning
        self.sql_knowledge_retriever = sql_knowledge_retriever
        self.sql_optimizer = sql_optimizer
        self.validator = validator
        self.cost_model = cost_model

    def resolve_system_level(
        self,
        diagnosis: DiagnosisResult,
        knowledge: list[KnowledgeEntry],
    ) -> ResolutionPlan:
        """生成系统级处置方案。"""
        candidates = self.system_target_selector.select_targets(diagnosis)
        ranked = self.system_target_selector.rank_targets(candidates, knowledge)
        retrieved = self.knowledge_retrieval.retrieve(diagnosis, ranked)
        plan = self.system_knob_tuning.generate_plan(diagnosis, ranked, retrieved)
        plan.estimated_cost = self.cost_model.estimate_system_plan(plan)
        return plan

    def resolve_sql_level(
        self,
        diagnosis: DiagnosisResult,
        knowledge: list[KnowledgeEntry],
    ) -> ResolutionPlan:
        """生成 SQL 级处置方案。"""
        sql_knowledge = self.sql_knowledge_retriever.retrieve(diagnosis)
        plan = self.sql_optimizer.generate_plan(diagnosis, sql_knowledge)
        plan.estimated_cost = self.cost_model.estimate_sql_plan(plan)
        return plan

    def resolve(
        self,
        diagnoses: list[DiagnosisResult],
        knowledge: list[KnowledgeEntry],
    ) -> list[ResolutionPlan]:
        """统一执行异常处置流程，对每条诊断结果生成处置方案。"""
        plans: list[ResolutionPlan] = []
        for diagnosis in diagnoses:
            if diagnosis.category == "system":
                plans.append(self.resolve_system_level(diagnosis, knowledge))
            elif diagnosis.category == "sql":
                plans.append(self.resolve_sql_level(diagnosis, knowledge))
        return plans
