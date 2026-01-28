from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class QueryInfo:
    query_id: int
    query: str
    plan_json: str
    external_metrics: List[List[float]]
    internal_metrics: List[float]
    execution_time: float
    is_rewrite: bool


@dataclass
class AnomalyInfo:
    kpis: Dict[str, List[float]]
    kpi_descriptions: Dict[str, str]
    log_info: Dict[str, Any]


@dataclass
class ActionResult:
    action_type: str
    parameters: Dict[str, Any]
    execution_status: str
    impact_metrics: Dict[str, float]
    timestamp: datetime
    details: Optional[Dict[str, Any]] = None


@dataclass
class CaseInfo:
    query_info: QueryInfo
    anomaly_info: AnomalyInfo
    root_causes: List[str]
    actions_taken: List[ActionResult]
    result_metrics: Dict[str, float]
    confidence: float
    status: str
