from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    collect_interval_seconds: float
    snapshot_retention_hours: float
    rag_sync_interval_seconds: float
    rag_sources_dir: str
    rag_llm_url: str | None
    rag_llm_model: str | None
    rag_llm_token: str | None
    host: str
    port: int
    kafka_bootstrap_servers: str
    kafka_topic: str
    kafka_group_id: str
    ebpf_enabled: bool
    ebpf_probe_interval_seconds: float
    ebpf_auto_adapt: bool
    ebpf_max_event_rate: float
    ebpf_long_query_threshold_seconds: float
    model_dir: str
    embedding_model: str
    embedding_cache_dir: str | None
    llvm_source_root: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        interval = float(os.getenv("KI_COLLECT_INTERVAL_SECONDS", "5"))
        if interval < 1:
            raise ValueError("KI_COLLECT_INTERVAL_SECONDS must be at least 1 second")
        retention_hours = float(os.getenv("KI_SNAPSHOT_RETENTION_HOURS", "24"))
        if retention_hours < 1:
            raise ValueError("KI_SNAPSHOT_RETENTION_HOURS must be at least 1 hour")
        rag_interval = float(os.getenv("KI_RAG_SYNC_INTERVAL_SECONDS", "60"))
        if rag_interval < 15:
            raise ValueError("KI_RAG_SYNC_INTERVAL_SECONDS must be at least 15 seconds")
        probe_interval = float(os.getenv("KI_EBPF_PROBE_INTERVAL_SECONDS", "10"))
        if probe_interval < 1:
            raise ValueError("KI_EBPF_PROBE_INTERVAL_SECONDS must be at least 1 second")
        max_event_rate = float(os.getenv("KI_EBPF_MAX_EVENT_RATE", "1000"))
        if max_event_rate <= 0:
            raise ValueError("KI_EBPF_MAX_EVENT_RATE must be positive")
        long_query_threshold = float(os.getenv("KI_EBPF_LONG_QUERY_THRESHOLD_SECONDS", "1.0"))
        if long_query_threshold <= 0:
            raise ValueError("KI_EBPF_LONG_QUERY_THRESHOLD_SECONDS must be positive")
        return cls(
            database_url=os.getenv(
                "KI_DATABASE_URL",
                "postgresql://keeninsight:keeninsight@localhost:5432/keeninsight",
            ),
            collect_interval_seconds=interval,
            snapshot_retention_hours=retention_hours,
            rag_sync_interval_seconds=rag_interval,
            rag_sources_dir=os.getenv("KI_RAG_SOURCES_DIR", "rag_sources"),
            rag_llm_url=os.getenv("KI_RAG_LLM_URL") or None,
            # Keep the low-cost target explicit whenever an endpoint is
            # enabled, while still leaving the no-endpoint default fully
            # deterministic and offline.
            rag_llm_model=(
                os.getenv("KI_RAG_LLM_MODEL")
                or ("gpt-4o-mini" if os.getenv("KI_RAG_LLM_URL") else None)
            ),
            rag_llm_token=os.getenv("KI_RAG_LLM_TOKEN") or None,
            host=os.getenv("KI_HOST", "127.0.0.1"),
            port=int(os.getenv("KI_PORT", "8000")),
            kafka_bootstrap_servers=os.getenv("KI_KAFKA_BOOTSTRAP_SERVERS", ""),
            kafka_topic=os.getenv("KI_KAFKA_TOPIC", "keeninsight.events"),
            kafka_group_id=os.getenv("KI_KAFKA_GROUP_ID", "keeninsight-api"),
            ebpf_enabled=_truthy(os.getenv("KI_EBPF_ENABLED", "false")),
            ebpf_probe_interval_seconds=probe_interval,
            ebpf_auto_adapt=_truthy(os.getenv("KI_EBPF_AUTO_ADAPT", "false")),
            ebpf_max_event_rate=max_event_rate,
            ebpf_long_query_threshold_seconds=long_query_threshold,
            model_dir=os.getenv("KI_MODEL_DIR", "artifacts/models"),
            embedding_model=os.getenv("KI_RAG_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            embedding_cache_dir=os.getenv("KI_RAG_EMBEDDING_CACHE_DIR") or None,
            llvm_source_root=os.getenv("KI_LLVM_SOURCE_ROOT") or None,
        )


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
