"""PostgreSQL 知识检索 — 返回与 knob 调优相关的知识。"""

from __future__ import annotations

import json
import os
from typing import Any

from ..models import DiagnosisResult, KnowledgeEntry


# PostgreSQL-specific tuning knowledge base
_PG_TUNING_KNOWLEDGE = [
    {
        "knob": "work_mem",
        "category": "memory",
        "title": "work_mem 调优建议",
        "content": "work_mem 控制排序和哈希操作的最大内存使用。建议值： OLTP 4-64MB，OLAP 64-512MB。太小会导致磁盘溢出，太大可能耗尽内存。",
        "tags": ["work_mem", "排序", "哈希", "内存"],
    },
    {
        "knob": "shared_buffers",
        "category": "memory",
        "title": "shared_buffers 调优建议",
        "content": "shared_buffers 是 PostgreSQL 最重要的 knob。建议设置为系统内存的 25%。过高可能导致反常的查询计划。",
        "tags": ["shared_buffers", "缓存", "内存"],
    },
    {
        "knob": "effective_cache_size",
        "category": "planner",
        "title": "effective_cache_size 调优建议",
        "content": "effective_cache_size 是查询规划器的提示参数，建议设置为 (total_memory - shared_buffers) 的 75%。",
        "tags": ["effective_cache_size", "规划器", "索引"],
    },
    {
        "knob": "maintenance_work_mem",
        "category": "maintenance",
        "title": "maintenance_work_mem 调优建议",
        "content": "维护操作（VACUUM、CREATE INDEX）使用 maintenance_work_mem。建议设置为 128-512MB。",
        "tags": ["maintenance_work_mem", "VACUUM", "索引"],
    },
    {
        "knob": "max_wal_size",
        "category": "wal",
        "title": "max_wal_size 调优建议",
        "content": "增加 max_wal_size 可以减少检查点频率，降低 I/O 压力。建议 1-4GB。",
        "tags": ["max_wal_size", "检查点", "WAL"],
    },
]


class KnowledgeRetrieval:
    """PostgreSQL 知识检索器。

    检索与候选 knob 相关的调优经验。
    """

    def retrieve(
        self,
        diagnosis: DiagnosisResult,
        target_knobs: list[str],
    ) -> list[KnowledgeEntry]:
        """检索与目标 knob 相关的知识条目。"""
        entries: list[KnowledgeEntry] = []

        for kb_entry in _PG_TUNING_KNOWLEDGE:
            # 匹配 knob 名称
            if kb_entry["knob"] in target_knobs:
                entry = KnowledgeEntry(
                    source="pg_knowledge_base",
                    title=kb_entry["title"],
                    content=kb_entry["content"],
                    tags=kb_entry["tags"],
                    metadata={
                        "knob": kb_entry["knob"],
                        "category": kb_entry["category"],
                    },
                )
                entries.append(entry)

        return entries

    def summarize_retrieved_knowledge(
        self, entries: list[KnowledgeEntry]
    ) -> list[KnowledgeEntry]:
        """摘要检索结果。"""
        return entries