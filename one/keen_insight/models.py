"""系统中的核心数据对象定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProbeAttachment:
    """描述一次探针挂载请求。"""

    name: str
    probe_type: str
    target: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitoringSnapshot:
    """封装一次监控周期内采集到的原始信息。"""

    user_events: list[dict[str, Any]] = field(default_factory=list)
    kernel_events: list[dict[str, Any]] = field(default_factory=list)
    db_metrics: dict[str, Any] = field(default_factory=dict)
    system_metrics: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


@dataclass
class SQLLifecycle:
    """表示一条 SQL 从进入系统到执行结束的生命周期视图。"""

    sql_id: str
    raw_sql: str
    stages: list[str] = field(default_factory=list)
    related_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DiagnosisResult:
    """统一封装系统级和 SQL 级诊断结果。"""

    category: str
    summary: str
    root_causes: list[str] = field(default_factory=list)
    confidence: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeQuery:
    """表示一次知识检索请求。"""

    category: str
    problem: str
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeEntry:
    """表示知识库中的一条结构化经验或规则。"""

    source: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolutionPlan:
    """表示异常处置阶段输出的方案。"""

    plan_type: str
    target: str
    actions: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    estimated_cost: float | None = None


@dataclass
class PipelineContext:
    """在主流程中传递的上下文对象。"""

    snapshot: MonitoringSnapshot | None = None
    sql_lifecycles: list[SQLLifecycle] = field(default_factory=list)
    diagnosis: list[DiagnosisResult] = field(default_factory=list)
    knowledge: list[KnowledgeEntry] = field(default_factory=list)
    resolution_plans: list[ResolutionPlan] = field(default_factory=list)

