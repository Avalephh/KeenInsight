"""
DREAM: An Anomaly-Aware Approach for Multi-Component Tuning in DBMSs

A Python package for database query optimization and anomaly diagnosis.
"""

__version__ = "0.1.0"

from dream.agent.db_agent import DBAgent
from dream.agent.simple_db_agent import SimpleDBAgent
from dream.utils.types import QueryInfo, AnomalyInfo, CaseInfo

__all__ = [
    "DBAgent",
    "SimpleDBAgent",
    "QueryInfo",
    "AnomalyInfo",
    "CaseInfo",
]