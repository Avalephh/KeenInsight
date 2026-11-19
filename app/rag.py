from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from psycopg.errors import UndefinedFunction

from .db import Database
from .llm import LLMClient, LLMError
from .locks import RAG_CORPUS_ADVISORY_LOCK

LOG = logging.getLogger(__name__)

TEXT_EXTENSIONS = {
    ".bib",
    ".css",
    ".html",
    ".htm",
    ".js",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".tex",
    ".txt",
    ".yml",
    ".yaml",
}
RUNTIME_SOURCE_TYPES = (
    "runtime-workload",
    "runtime-action",
    "runtime-feedback",
    "runtime-incident",
    "runtime-snapshot",
    "knowledge-entry",
    "runtime-observability",
    "runtime-lifecycle",
    "runtime-model",
    "runtime-prediction",
    "runtime-differential",
    "runtime-optimizer",
    "runtime-experience",
    "runtime-source-analysis",
    "runtime-code-knowledge",
)
CHUNKING_VERSION = "locator-v1"

# ``simple`` text search intentionally has no language-specific stemming or
# stop-word assumptions.  This small query-side list prevents generic
# question words from dominating the document ranking while preserving
# PostgreSQL- and implementation-specific terms such as ``work_mem``.
RAG_QUERY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "by", "can",
    "does", "for", "from", "how", "in", "is", "it", "made", "of", "on",
    "or", "that", "the", "these", "this", "to", "what", "when", "where",
    "which", "with",
}


class RagError(ValueError):
    pass


class _VisibleTextParser(HTMLParser):
    """Extract readable text from the original static demo pages."""

    _ignored = {"script", "style", "svg", "noscript", "template"}
    _headings = {"h1", "h2", "h3", "h4", "h5", "h6", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ignored:
            self._ignored_depth += 1
        if tag in self._headings or tag in {"p", "div", "li", "tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1
        if tag in self._headings or tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def normalize_text(value: str) -> str:
    value = str(value or "").replace("\x00", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in value.split("\n")]
    normalized: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                normalized.append("")
            blank = True
        else:
            normalized.append(line)
            blank = False
    return "\n".join(normalized).strip()


def chunk_text(
    text: str, *, max_chars: int = 2200, overlap_chars: int = 280
) -> list[dict[str, Any]]:
    """Split source text while retaining paragraph/line boundaries."""
    normalized = normalize_text(text)
    if not normalized:
        return []
    if max_chars < 400 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("invalid RAG chunk size")
    chunks: list[dict[str, Any]] = []
    start = 0
    length = len(normalized)
    while start < length:
        rough_end = min(length, start + max_chars)
        end = rough_end
        if rough_end < length:
            search_start = start + max(1, int(max_chars * 0.55))
            candidates = [
                normalized.rfind("\n\n", search_start, rough_end),
                normalized.rfind("\n", search_start, rough_end),
                normalized.rfind(" ", search_start, rough_end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary
        content = normalized[start:end].strip()
        if content:
            chunks.append(
                {
                    "content": content,
                    "token_count": len(
                        re.findall(r"\w+|[^\w\s]", content, flags=re.UNICODE)
                    ),
                    "metadata": {
                        "char_start": start,
                        "char_end": end,
                    },
                }
            )
        if end >= length:
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def chunk_locator(text: str, start: int, end: int) -> dict[str, Any]:
    """Return a human-checkable locator in normalized source coordinates.

    Source files are normalized before indexing, so line numbers deliberately
    refer to the normalized text shown by the retriever.  PDF extraction adds
    ``[Page N]`` markers; when those markers intersect a chunk, page bounds
    are included as a second, more stable locator.
    """
    normalized = normalize_text(text)
    safe_start = max(0, min(int(start), len(normalized)))
    safe_end = max(safe_start, min(int(end), len(normalized)))
    locator: dict[str, Any] = {
        "coordinate_system": "normalized_source",
        "char_start": safe_start,
        "char_end": safe_end,
        "line_start": normalized.count("\n", 0, safe_start) + 1,
        "line_end": normalized.count("\n", 0, max(safe_start, safe_end - 1)) + 1,
    }
    page_markers = list(
        re.finditer(r"(?m)^\[Page\s+(\d+)\]\s*$", normalized)
    )
    if page_markers:
        page_start: int | None = None
        page_end: int | None = None
        end_position = max(safe_start, safe_end - 1)
        for marker in page_markers:
            page = int(marker.group(1))
            if marker.start() <= safe_start:
                page_start = page
            if marker.start() <= end_position:
                page_end = page
        if page_start is not None:
            locator["page_start"] = page_start
        if page_end is not None:
            locator["page_end"] = page_end
    return locator


def _parse_source_header(text: str) -> tuple[dict[str, str], str]:
    """Parse the tiny header emitted by scripts/collect_sources.py."""
    lines = text.replace("\r\n", "\n").split("\n")
    metadata: dict[str, str] = {}
    end = 0
    for index, line in enumerate(lines[:30]):
        if line.strip() == "---":
            end = index + 1
            break
        match = re.match(r"^([A-Z][A-Z0-9_]+):\s*(.*)$", line.strip())
        if match:
            metadata[match.group(1).lower()] = match.group(2).strip()
        elif line.strip():
            break
    return metadata, "\n".join(lines[end:]) if end else text


def read_text_source(path: Path) -> tuple[dict[str, str], str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - optional deployment path
            raise RagError(
                "PDF ingestion requires pypdf; install the project requirements"
            ) from exc
        reader = PdfReader(str(path))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            pages.append(f"[Page {index}]\n{page.extract_text() or ''}")
        return {}, normalize_text("\n\n".join(pages))
    raw = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = _parse_source_header(raw)
    if suffix in {".html", ".htm"}:
        parser = _VisibleTextParser()
        parser.feed(body)
        body = "\n".join(parser.parts)
    return metadata, normalize_text(body)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def upsert_document(
    db: Database,
    *,
    source_key: str,
    source_type: str,
    title: str,
    uri: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = normalize_text(content)
    if not content:
        return {"status": "empty", "source_key": source_key, "chunks": 0}
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    chunks = chunk_text(content)
    with db.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                metadata = dict(metadata or {})
                metadata.setdefault("chunking_version", CHUNKING_VERSION)
                cur.execute(
                    """
                    SELECT id, content_hash, metadata
                    FROM keeninsight.rag_documents
                    WHERE source_key = %s
                    FOR UPDATE
                    """,
                    (source_key,),
                )
                existing = cur.fetchone()
                existing_metadata = existing.get("metadata") if existing else None
                if (
                    existing
                    and existing["content_hash"] == content_hash
                    and (existing_metadata or {}).get("chunking_version")
                    == CHUNKING_VERSION
                ):
                    return {
                        "status": "unchanged",
                        "source_key": source_key,
                        "document_id": existing["id"],
                        "chunks": len(chunks),
                    }
                if existing:
                    cur.execute(
                        """
                        UPDATE keeninsight.rag_documents
                        SET source_type = %s, title = %s, uri = %s,
                            content_hash = %s, content = %s,
                            metadata = %s::jsonb, updated_at = now()
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            source_type,
                            title,
                            uri,
                            content_hash,
                            content,
                            _json(metadata),
                            existing["id"],
                        ),
                    )
                    document_id = cur.fetchone()["id"]
                    status = "updated"
                else:
                    cur.execute(
                        """
                        INSERT INTO keeninsight.rag_documents
                            (source_key, source_type, title, uri, content_hash,
                             content, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        RETURNING id
                        """,
                        (
                            source_key,
                            source_type,
                            title,
                            uri,
                            content_hash,
                            content,
                            _json(metadata),
                        ),
                    )
                    document_id = cur.fetchone()["id"]
                    status = "created"
                cur.execute(
                    "DELETE FROM keeninsight.rag_chunks WHERE document_id = %s",
                    (document_id,),
                )
                for index, chunk in enumerate(chunks):
                    chunk_metadata = {
                        **metadata,
                        **(chunk.get("metadata") or {}),
                        "source_key": source_key,
                        "locator": chunk_locator(
                            content,
                            int((chunk.get("metadata") or {}).get("char_start", 0)),
                            int((chunk.get("metadata") or {}).get("char_end", 0)),
                        ),
                    }
                    cur.execute(
                        """
                        INSERT INTO keeninsight.rag_chunks
                            (document_id, chunk_index, content, token_count,
                             metadata, search_vector)
                        VALUES (%s, %s, %s, %s, %s::jsonb,
                                to_tsvector('simple', %s))
                        """,
                        (
                            document_id,
                            index,
                            chunk["content"],
                            chunk["token_count"],
                            _json(chunk_metadata),
                            chunk["content"],
                        ),
                    )
    return {
        "status": status,
        "source_key": source_key,
        "document_id": document_id,
        "chunks": len(chunks),
    }


def _source_type_for(path: Path, metadata: dict[str, str]) -> str:
    if metadata.get("source_type"):
        return metadata["source_type"]
    name = path.name.lower()
    if "review" in name:
        return "review"
    if path.suffix.lower() in {".tex", ".bib"}:
        return "paper"
    if path.suffix.lower() in {".html", ".htm"}:
        return "legacy-demo"
    if path.suffix.lower() in {".md", ".yml", ".yaml"}:
        return "design"
    return "implementation"


def ingest_directory(db: Database, directory: str | Path) -> dict[str, Any]:
    root = Path(directory)
    result = {"created": 0, "updated": 0, "unchanged": 0, "empty": 0, "errors": []}
    if not root.exists():
        return result
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in TEXT_EXTENSIONS
        # Generated source files may legitimately represent dotfiles such as
        # .env.example.  Skip hidden directories (for example a copied VCS
        # folder), but do not discard a source solely because its filename is
        # hidden.
        and not any(
            part.startswith(".") for part in path.relative_to(root).parts[:-1]
        )
    )
    for path in paths:
        try:
            metadata, content = read_text_source(path)
            relative = path.relative_to(root).as_posix()
            source_key = metadata.get("source_key") or f"file:{relative}"
            source_type = _source_type_for(path, metadata)
            title = metadata.get("source_title") or path.stem
            uri = metadata.get("source_uri") or relative
            item = upsert_document(
                db,
                source_key=source_key,
                source_type=source_type,
                title=title,
                uri=uri,
                content=content,
                metadata={
                    "path": relative,
                    "ingest_kind": "static_source",
                },
            )
            result[item["status"]] = result.get(item["status"], 0) + 1
        except Exception as exc:  # keep one malformed source from hiding others
            result["errors"].append({"path": str(path), "error": str(exc)})
            LOG.warning("RAG source ingestion failed for %s: %s", path, exc)
    return result


def _runtime_document(
    db: Database,
    *,
    source_key: str,
    source_type: str,
    title: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return upsert_document(
        db,
        source_key=source_key,
        source_type=source_type,
        title=title,
        uri=source_key,
        content=content,
        metadata={"ingest_kind": "runtime_evidence", **(metadata or {})},
    )


def sync_runtime_evidence(db: Database, *, limit: int = 100) -> dict[str, Any]:
    """Mirror live evidence into RAG without touching demo relations."""
    results: list[dict[str, Any]] = []
    runs = db.fetch_all(
        """
        SELECT id, scenario, phase, status, parameters, observations,
               started_at, finished_at, error
        FROM keeninsight.workload_runs
        ORDER BY id DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in runs:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:workload_run:{row['id']}",
                source_type="runtime-workload",
                title=f"Workload run #{row['id']} · {row['scenario']} · {row['phase']}",
                content=(
                    f"Workload run #{row['id']}\n"
                    f"Scenario: {row['scenario']}\nPhase: {row['phase']}\n"
                    f"Status: {row['status']}\n"
                    f"Parameters:\n{json.dumps(row.get('parameters') or {}, indent=2, default=str)}\n"
                    f"Observations:\n{json.dumps(row.get('observations') or {}, indent=2, default=str)}\n"
                    f"Error: {row.get('error') or 'none'}"
                ),
                metadata={"run_id": row["id"], "scenario": row["scenario"]},
            )
        )

    actions = db.fetch_all(
        """
        SELECT id, incident_id, action_type, target, description, status,
               before_state, after_state, rollback_state, validation, error,
               proposed_at, applied_at, rolled_back_at
        FROM keeninsight.tuning_actions
        ORDER BY id DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in actions:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:tuning_action:{row['id']}",
                source_type="runtime-action",
                title=f"Tuning action #{row['id']} · {row['action_type']}",
                content=(
                    f"Tuning action #{row['id']}\n"
                    f"Action type: {row['action_type']}\nStatus: {row['status']}\n"
                    f"Target:\n{json.dumps(row.get('target') or {}, indent=2, default=str)}\n"
                    f"Description: {row.get('description') or ''}\n"
                    f"Validation:\n{json.dumps(row.get('validation') or {}, indent=2, default=str)}\n"
                    f"Before state:\n{json.dumps(row.get('before_state') or {}, indent=2, default=str)}\n"
                    f"After state:\n{json.dumps(row.get('after_state') or {}, indent=2, default=str)}\n"
                    f"Rollback state:\n{json.dumps(row.get('rollback_state') or {}, indent=2, default=str)}\n"
                    f"Error: {row.get('error') or 'none'}"
                ),
                metadata={"action_id": row["id"], "action_type": row["action_type"]},
            )
        )

    feedback = db.fetch_all(
        """
        SELECT f.id, f.action_id, f.baseline_run_id, f.after_run_id,
               f.outcome, f.created_at, a.action_type, a.target
        FROM keeninsight.tuning_feedback f
        JOIN keeninsight.tuning_actions a ON a.id = f.action_id
        ORDER BY f.id DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in feedback:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:tuning_feedback:{row['id']}",
                source_type="runtime-feedback",
                title=f"Measured feedback #{row['id']} · action #{row['action_id']}",
                content=(
                    f"Measured tuning feedback #{row['id']}\n"
                    f"Action: #{row['action_id']} {row['action_type']}\n"
                    f"Baseline run: #{row['baseline_run_id']}\n"
                    f"After run: #{row['after_run_id']}\n"
                    f"Target:\n{json.dumps(row.get('target') or {}, indent=2, default=str)}\n"
                    f"Outcome:\n{json.dumps(row.get('outcome') or {}, indent=2, default=str)}"
                ),
                metadata={"feedback_id": row["id"], "action_id": row["action_id"]},
            )
        )

    incidents = db.fetch_all(
        """
        SELECT id, signature, kind, severity, status, summary, first_seen,
               last_seen, resolved_at, evidence, diagnosis, metadata
        FROM keeninsight.incidents
        ORDER BY id DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in incidents:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:incident:{row['id']}",
                source_type="runtime-incident",
                title=f"Incident #{row['id']} · {row['kind']} · {row['status']}",
                content=(
                    f"Incident #{row['id']}\nKind: {row['kind']}\nSeverity: {row['severity']}\n"
                    f"Status: {row['status']}\nSummary: {row['summary']}\n"
                    f"Evidence:\n{json.dumps(row.get('evidence') or {}, indent=2, default=str)}\n"
                    f"Diagnosis:\n{json.dumps(row.get('diagnosis') or {}, indent=2, default=str)}"
                ),
                metadata={"incident_id": row["id"], "kind": row["kind"]},
            )
        )

    snapshot = db.fetch_one(
        """
        SELECT id, collected_at, server_version, database_name, payload,
               collection_error
        FROM keeninsight.snapshots ORDER BY collected_at DESC LIMIT 1
        """
    )
    if snapshot:
        payload = snapshot.get("payload") or {}
        selected = {
            "observed_at": payload.get("observed_at"),
            "server": payload.get("server"),
            "database": payload.get("database"),
            "rates": payload.get("rates"),
            "settings": payload.get("settings"),
            "activity": payload.get("activity"),
            "locks": payload.get("locks"),
            "query_deltas": payload.get("query_deltas"),
            "features": payload.get("features"),
            "host": payload.get("host"),
            "collection_error": snapshot.get("collection_error"),
        }
        results.append(
            _runtime_document(
                db,
                source_key="runtime:snapshot:latest",
                source_type="runtime-snapshot",
                title="Latest PostgreSQL telemetry snapshot",
                content=(
                    "Latest PostgreSQL telemetry snapshot\n"
                    + json.dumps(selected, indent=2, ensure_ascii=False, default=str)
                ),
                metadata={"snapshot_id": snapshot["id"]},
            )
        )

    knowledge = db.fetch_all(
        """
        SELECT id, source_type, title, root_cause, context, action_type,
               action_template, provenance, verified, confidence,
               feedback_count, success_count, failure_count
        FROM keeninsight.knowledge_entries
        ORDER BY id DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in knowledge:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:knowledge:{row['id']}",
                source_type="knowledge-entry",
                title=f"Knowledge entry · {row['title']}",
                content=(
                    f"Knowledge entry: {row['title']}\n"
                    f"Root cause: {row['root_cause']}\n"
                    f"Source type: {row['source_type']}\n"
                    f"Context:\n{json.dumps(row.get('context') or {}, indent=2, default=str)}\n"
                    f"Action type: {row.get('action_type') or 'none'}\n"
                    f"Action template:\n{json.dumps(row.get('action_template') or {}, indent=2, default=str)}\n"
                    f"Provenance: {row.get('provenance') or ''}\n"
                    f"Verified: {row.get('verified')}\n"
                    f"Confidence: {row.get('confidence')}"
                ),
                metadata={"knowledge_id": row["id"], "root_cause": row["root_cause"]},
            )
        )

    observability = db.fetch_all(
        """
        SELECT id, session_id, observed_at, event_type, probe_kind,
               function_name, pid, duration_ns, bytes, is_anomaly,
               transport, payload
        FROM keeninsight.ebpf_events ORDER BY observed_at DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in observability:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:ebpf_event:{row['id']}",
                source_type="runtime-observability",
                title=f"eBPF event #{row['id']} · {row['function_name']}",
                content=(
                    "Observed eBPF event\n"
                    + json.dumps(row, indent=2, ensure_ascii=False, default=str)
                ),
                metadata={"event_id": row["id"], "session_id": row.get("session_id")},
            )
        )

    lifecycle = db.fetch_all(
        """
        SELECT id, session_id, query_id, query_text, query_id_source,
               function_name, first_observed_at, last_observed_at,
               event_count, duration_ns, bytes, anomaly_count,
               source_event_ids, metadata
        FROM keeninsight.sql_lifecycle_events
        ORDER BY last_observed_at DESC, id DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in lifecycle:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:sql_lifecycle:{row['id']}",
                source_type="runtime-lifecycle",
                title=f"SQL lifecycle #{row['id']} · {row['function_name']}",
                content="Reconstructed SQL lifecycle evidence\n" + json.dumps(row, indent=2, ensure_ascii=False, default=str),
                metadata={"lifecycle_id": row["id"], "session_id": row.get("session_id"), "query_id": row.get("query_id")},
            )
        )

    model_runs = db.fetch_all(
        """
        SELECT id, model_type, algorithm, status, feature_names, target_name,
               train_count, metrics, artifact_path, model_digest, created_at,
               finished_at, error
        FROM keeninsight.model_runs ORDER BY id DESC LIMIT %s
        """,
        (max(1, min(limit, 200)),),
    )
    for row in model_runs:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:model_run:{row['id']}",
                source_type="runtime-model",
                title=f"Model run #{row['id']} · {row['model_type']}",
                content="Model training evidence\n" + json.dumps(row, indent=2, ensure_ascii=False, default=str),
                metadata={"model_run_id": row["id"], "model_type": row["model_type"]},
            )
        )

    predictions = db.fetch_all(
        """
        SELECT id, model_run_id, model_type, query, features, prediction, created_at
        FROM keeninsight.model_predictions ORDER BY created_at DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in predictions:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:model_prediction:{row['id']}",
                source_type="runtime-prediction",
                title=f"Model prediction #{row['id']} · {row['model_type']}",
                content="Persisted model prediction and features\n" + json.dumps(row, indent=2, ensure_ascii=False, default=str),
                metadata={"prediction_id": row["id"], "model_type": row["model_type"]},
            )
        )

    differential = db.fetch_all(
        """
        SELECT id, baseline_session_id, degraded_session_id, method,
               features, created_at
        FROM keeninsight.differential_profiles ORDER BY created_at DESC LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )
    for row in differential:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:differential_profile:{row['id']}",
                source_type="runtime-differential",
                title=f"Differential profile #{row['id']}",
                content="Function differential and SHAP evidence\n" + json.dumps(row, indent=2, ensure_ascii=False, default=str),
                metadata={"differential_profile_id": row["id"]},
            )
        )

    optimizer_runs = db.fetch_all(
        """
        SELECT id, query, status, baseline, knowledge, source_evidence,
               selected_candidate, ranking, created_at, finished_at, error
        FROM keeninsight.optimizer_runs ORDER BY id DESC LIMIT %s
        """,
        (max(1, min(limit, 200)),),
    )
    for row in optimizer_runs:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:optimizer_run:{row['id']}",
                source_type="runtime-optimizer",
                title=f"Tuning agent run #{row['id']}",
                content="Tuning agent evidence\n" + json.dumps(row, indent=2, ensure_ascii=False, default=str),
                metadata={"optimizer_run_id": row["id"]},
            )
        )

    experiences = db.fetch_all(
        """
        SELECT id, fingerprint, cluster_id, context, root_cause, action,
               support, success_count, confidence, provenance
        FROM keeninsight.tuning_experiences ORDER BY updated_at DESC LIMIT %s
        """,
        (max(1, min(limit, 200)),),
    )
    for row in experiences:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:experience:{row['id']}",
                source_type="runtime-experience",
                title=f"Mined tuning experience #{row['id']} · {row['root_cause']}",
                content="Mined tuning experience\n" + json.dumps(row, indent=2, ensure_ascii=False, default=str),
                metadata={"experience_id": row["id"], "root_cause": row["root_cause"]},
            )
        )

    source_rows = db.fetch_all(
        """
        SELECT id, source_root, knob, function_name, file_path, line_number,
               relation_type, ir_symbol, evidence, source_digest, analyzed_at
        FROM keeninsight.source_analyses ORDER BY analyzed_at DESC LIMIT %s
        """,
        (max(1, min(limit, 200)),),
    )
    for row in source_rows:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:source_analysis:{row['id']}",
                source_type="runtime-source-analysis",
                title=f"LLVM source propagation · {row.get('knob') or row.get('function_name')}",
                content="LLVM/source analysis evidence\n" + json.dumps(row, indent=2, ensure_ascii=False, default=str),
                metadata={"source_analysis_id": row["id"], "relation_type": row["relation_type"]},
            )
        )

    code_knowledge = db.fetch_all(
        """
        SELECT id, fingerprint, source_root, knob, target_functions,
               retrieval_context, hypothesis, validation, confidence,
               provenance, created_at, updated_at
        FROM keeninsight.code_knowledge
        ORDER BY updated_at DESC, id DESC LIMIT %s
        """,
        (max(1, min(limit, 200)),),
    )
    for row in code_knowledge:
        results.append(
            _runtime_document(
                db,
                source_key=f"runtime:code_knowledge:{row['id']}",
                source_type="runtime-code-knowledge",
                title=f"Code-derived tuning knowledge #{row['id']} · {row['knob']}",
                content=(
                    "Compiler-backed code retrieval and semantic hypothesis\n"
                    + json.dumps(row, indent=2, ensure_ascii=False, default=str)
                ),
                metadata={"code_knowledge_id": row["id"], "knob": row["knob"]},
            )
        )

    # Runtime RAG documents are a bounded derived index.  The complete
    # historical records remain in their source tables and in /api/export;
    # pruning only prevents a long-running service from growing its search
    # index without limit.
    runtime_keys = [item["source_key"] for item in results if item.get("source_key")]
    if runtime_keys:
        pruned = db.execute(
            """
            DELETE FROM keeninsight.rag_documents
            WHERE source_type = ANY(%s)
              AND NOT (source_key = ANY(%s))
            """,
            (list(RUNTIME_SOURCE_TYPES), runtime_keys),
        )
    else:
        pruned = db.execute(
            "DELETE FROM keeninsight.rag_documents WHERE source_type = ANY(%s)",
            (list(RUNTIME_SOURCE_TYPES),),
        )

    summary = {"created": 0, "updated": 0, "unchanged": 0, "empty": 0, "errors": []}
    for item in results:
        status = item.get("status")
        summary[status] = summary.get(status, 0) + 1
    return {
        **summary,
        "documents_processed": len(results),
        "limit": limit,
        "pruned": pruned,
    }


def _search_rows(
    db: Database, query: str, top_k: int, source_type: str | None
) -> list[dict[str, Any]]:
    # Fetch a wider candidate pool because ``search`` returns at most one
    # chunk per document. This lets a relevant document survive adjacent
    # chunks from a single long source.
    row_limit = min(100, max(top_k, top_k * 10))
    source_clause = ""
    if source_type:
        source_clause = " AND d.source_type = %s"

    def fts_rows(search_query: str) -> list[dict[str, Any]]:
        params: list[Any] = [search_query]
        if source_type:
            params.append(source_type)
        params.append(row_limit)
        statement = f"""
            WITH search_query AS (
                SELECT websearch_to_tsquery('simple', %s) AS tsq
            )
            SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.content,
                   d.source_key, d.source_type, d.title, d.uri,
                   c.metadata AS chunk_metadata,
                   ts_rank_cd(c.search_vector, search_query.tsq) AS rank,
                   ts_headline(
                       'simple', c.content, search_query.tsq,
                       'MaxWords=42,MinWords=12,ShortWord=2'
                   ) AS snippet
            FROM keeninsight.rag_chunks c
            JOIN keeninsight.rag_documents d ON d.id = c.document_id
            CROSS JOIN search_query
            WHERE search_query.tsq::text <> ''
              AND c.search_vector @@ search_query.tsq
              {source_clause}
            ORDER BY rank DESC, d.updated_at DESC, c.chunk_index
            LIMIT %s
        """
        return db.fetch_all(statement, params)

    try:
        rows = fts_rows(query)
    except UndefinedFunction:
        rows = []

    # Natural-language questions often contain a qualifier that is absent
    # from a short source passage. Add an OR candidate pool before falling
    # back to substring matching, while keeping every term parameterized in
    # SQL. The two pools are merged and re-ranked by discriminative query
    # term coverage below; native FTS rank remains the tie-breaker.
    terms = _query_terms(query)
    if len(terms) > 1:
        try:
            broad_rows = fts_rows(" OR ".join(terms))
        except UndefinedFunction:
            broad_rows = []
        if broad_rows:
            by_chunk = {row["chunk_id"]: row for row in rows}
            by_chunk.update({row["chunk_id"]: row for row in broad_rows})
            rows = list(by_chunk.values())
    if rows:
        return _rerank_rows(rows, query)

    # Exact/term fallback keeps the API useful for a phrase that PostgreSQL's
    # simple parser cannot tokenize, while remaining parameterized SQL.
    patterns = [query] + terms
    # Preserve an exact phrase match, but require all extracted terms for the
    # relaxed branch.  An OR over every individual word turns a no-hit query
    # containing common words such as "evidence" into unrelated results.
    conditions = "c.content ILIKE %s"
    if terms:
        all_terms = " AND ".join("c.content ILIKE %s" for _ in terms)
        conditions += f" OR ({all_terms})"
    fallback_params: list[Any] = [f"%{value}%" for value in patterns]
    if source_type:
        conditions = f"({conditions}) AND d.source_type = %s"
        fallback_params.append(source_type)
    fallback_params.append(row_limit)
    return db.fetch_all(
        f"""
        SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.content,
               d.source_key, d.source_type, d.title, d.uri,
               c.metadata AS chunk_metadata,
               0.0::double precision AS rank,
               left(c.content, 600) AS snippet
        FROM keeninsight.rag_chunks c
        JOIN keeninsight.rag_documents d ON d.id = c.document_id
        WHERE ({conditions})
        ORDER BY d.updated_at DESC, c.chunk_index
        LIMIT %s
        """,
        fallback_params,
    )


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_]{2,}", query.lower())
    return list(
        dict.fromkeys(term for term in terms if term not in RAG_QUERY_STOPWORDS)
    )[:12]


def _rerank_rows(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Prefer candidates covering query-specific terms over generic prose.

    PostgreSQL ``simple`` FTS is the source of truth for candidate generation.
    This deterministic second pass only corrects the common failure mode in
    natural-language questions where frequent connective words dominate
    ``ts_rank_cd``. It never invents a hit: every returned row was produced by
    FTS (or the explicit fallback), and the native rank breaks ties.
    """
    terms = _query_terms(query)
    if not terms:
        return rows

    def score(row: dict[str, Any]) -> tuple[float, int, float]:
        content = str(row.get("content") or "").lower()
        covered = sum(
            bool(
                re.search(
                    r"(?<![a-z0-9_])"
                    + re.escape(term)
                    + r"(?![a-z0-9_])",
                    content,
                )
            )
            for term in terms
        )
        coverage = covered / len(terms)
        return coverage, covered, float(row.get("rank") or 0.0)

    return sorted(rows, key=score, reverse=True)


def search(
    db: Database, query: str, *, top_k: int = 5, source_type: str | None = None
) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    if not query or len(query) > 5000:
        raise RagError("query must contain 1-5000 characters")
    top_k = max(1, min(int(top_k), 10))
    rows = _search_rows(db, query, top_k, source_type)
    result = []
    seen_documents: set[Any] = set()
    for row in rows:
        # A long source can contribute many adjacent chunks.  Keep the answer
        # context diverse by returning at most one chunk per document; the
        # wider SQL limit above gives other sources a chance to rank in.
        if row["document_id"] in seen_documents:
            continue
        seen_documents.add(row["document_id"])
        index = len(result) + 1
        result.append(
            {
                "citation_id": f"R{index}",
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "chunk_index": row["chunk_index"],
                "title": row["title"],
                "source_type": row["source_type"],
                "provenance": row["uri"],
                "source_key": row["source_key"],
                "locator": (row.get("chunk_metadata") or {}).get("locator"),
                "rank": float(row.get("rank") or 0.0),
                "snippet": row.get("snippet") or row.get("content") or "",
                "content": row.get("content") or "",
            }
        )
        if len(result) >= top_k:
            break
    return result


def _extractive_answer(query: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return (
            "No matching evidence was found in the indexed paper, review, design, "
            "implementation, or runtime records. Add a source or run an explicit "
            "RAG sync before making a claim."
        )
    lines = [
        "Grounded evidence brief (extractive fallback; no external generator is configured):"
    ]
    for hit in hits:
        content = re.sub(r"\s+", " ", hit.get("content") or "").strip()
        if len(content) > 720:
            content = content[:717].rsplit(" ", 1)[0] + "..."
        lines.append(f"[{hit['citation_id']}] {hit['title']}: {content}")
    lines.append(
        "The passages above are retrieved evidence; verify the cited source before "
        "using a recommendation in the paper or on a production database."
    )
    return "\n\n".join(lines)


def _external_answer(
    url: str | None,
    model: str | None,
    token: str | None,
    query: str,
    hits: list[dict[str, Any]],
    *,
    llm_client: LLMClient | None = None,
) -> tuple[str, dict[str, Any]] | None:
    if not hits:
        return None
    client = llm_client or LLMClient(url=url, model=model, token=token)
    if not client.configured:
        return None
    context = "\n\n".join(
        f"[{hit['citation_id']}] {hit['title']} ({hit['provenance']})\n"
        f"{hit['content'][:3000]}"
        for hit in hits
    )
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a grounded PostgreSQL research assistant. Answer only "
                    "from the supplied evidence, cite each material claim with the "
                    "supplied [R#] markers, and say clearly when the evidence is "
                    "insufficient. Do not invent measurements, sources, or actions. "
                    "Do not output executable SQL. Answer in the same language as "
                    "the question."
                ),
            },
            {
                "role": "user",
                "content": f"Question: {query}\n\nEvidence:\n{context}",
            },
        ],
    }
    try:
        # Keep this payload construction local for compatibility with older
        # callers; the shared client owns transport, auth, timeout and
        # redacted error handling.
        result = client.complete(
            payload["messages"],
            temperature=0,
            max_tokens=900,
        )
        content = str(result.get("content") or "").strip()
        if not content:
            return None
        valid_citations = {"[" + str(hit["citation_id"]) + "]" for hit in hits}
        cited = {marker for marker in valid_citations if marker in content}
        if not cited:
            # A response without a source marker is not accepted as a grounded
            # answer.  The extractive path remains the honest fallback.
            LOG.warning("external RAG answer was rejected because it had no valid citation")
            return None
        return content, {
            "generator": "external",
            "model": result.get("model") or client.model,
            "usage": result.get("usage") or {},
            "latency_ms": result.get("latency_ms"),
            "citations_validated": sorted(cited),
        }
    except LLMError:
        # Optional generation must never make retrieval unavailable.  The
        # shared client has already recorded a redacted operational error.
        return None


def ask(
    db: Database,
    query: str,
    *,
    top_k: int = 5,
    source_type: str | None = None,
    llm_url: str | None = None,
    llm_model: str | None = None,
    llm_token: str | None = None,
    mode: str = "lexical",
    vector_search: Any = None,
    llm_client: LLMClient | None = None,
) -> dict[str, Any]:
    if mode not in {"lexical", "vector", "hybrid"}:
        raise RagError("retrieval mode must be lexical, vector, or hybrid")
    if mode == "vector":
        if vector_search is None:
            raise RagError("vector retrieval is not configured")
        hits = vector_search(query, top_k=top_k, source_type=source_type)
    elif mode == "hybrid":
        lexical_hits = search(db, query, top_k=max(top_k, 5), source_type=source_type)
        if vector_search is None:
            raise RagError("hybrid retrieval requires the embedding index")
        vector_hits = vector_search(query, top_k=max(top_k, 5), source_type=source_type)
        merged: dict[Any, dict[str, Any]] = {}
        for rank, hit in enumerate(lexical_hits, 1):
            merged[hit["chunk_id"]] = {**hit, "hybrid_score": 1.0 / (rank + 10.0)}
        for rank, hit in enumerate(vector_hits, 1):
            current = merged.get(hit["chunk_id"])
            if current:
                current["hybrid_score"] += 1.0 / (rank + 10.0)
                current["vector_similarity"] = hit.get("vector_similarity")
            else:
                merged[hit["chunk_id"]] = {**hit, "hybrid_score": 1.0 / (rank + 10.0)}
        hits = sorted(merged.values(), key=lambda item: item.get("hybrid_score", 0.0), reverse=True)[:top_k]
        for index, hit in enumerate(hits, 1):
            hit["citation_id"] = "R%d" % index
            hit["retrieval_mode"] = "hybrid-rrf"
    else:
        hits = search(db, query, top_k=top_k, source_type=source_type)
    generated = _external_answer(
        llm_url,
        llm_model,
        llm_token,
        query,
        hits,
        llm_client=llm_client,
    )
    answer = generated[0] if generated else _extractive_answer(query, hits)
    generation = generated[1] if generated else {
        "generator": "extractive",
        "model": (llm_client.model if llm_client else llm_model),
        "reason": (
            "no_matching_evidence"
            if not hits
            else "external_generation_unavailable_or_rejected"
        ),
    }
    return {
        "query": query,
        "answer": answer,
        "generator": generation["generator"],
        "generation": generation,
        "citations": [
            {
                "citation_id": hit["citation_id"],
                "title": hit["title"],
                "source_type": hit["source_type"],
                "provenance": hit["provenance"],
                "chunk_index": hit["chunk_index"],
                "locator": hit.get("locator"),
            }
            for hit in hits
        ],
        "results": hits,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def database_status(db: Database) -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT
            (SELECT count(*) FROM keeninsight.rag_documents) AS documents,
            (SELECT count(*) FROM keeninsight.rag_chunks) AS chunks,
            (SELECT max(updated_at) FROM keeninsight.rag_documents) AS latest_update
        """
    ) or {}
    types = db.fetch_all(
        """
        SELECT source_type, count(*) AS documents
        FROM keeninsight.rag_documents
        GROUP BY source_type ORDER BY source_type
        """
    )
    return {
        "documents": int(row.get("documents") or 0),
        "chunks": int(row.get("chunks") or 0),
        "latest_update": row.get("latest_update"),
        "source_types": types,
    }


class RagIndexer:
    """Low-frequency background indexer kept separate from the workload path."""

    def __init__(
        self,
        db: Database,
        *,
        source_dir: str,
        interval_seconds: float,
        llm_url: str | None = None,
        llm_model: str | None = None,
        llm_token: str | None = None,
    ) -> None:
        self.db = db
        self.source_dir = Path(source_dir)
        self.interval_seconds = interval_seconds
        self.llm_url = llm_url
        self.llm_model = llm_model
        self.llm_token = llm_token
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._sync_count = 0
        self._last_sync_at: datetime | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="keeninsight-rag-indexer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def sync_once(self) -> dict[str, Any]:
        with self._sync_lock:
            with self.db.advisory_lock(RAG_CORPUS_ADVISORY_LOCK):
                static_result = ingest_directory(self.db, self.source_dir)
                runtime_result = sync_runtime_evidence(self.db)
                result = {
                    "static": static_result,
                    "runtime": runtime_result,
                    "database": database_status(self.db),
                }
                with self._state_lock:
                    self._sync_count += 1
                    self._last_sync_at = datetime.now(timezone.utc)
                    self._last_error = None
                    self._last_result = result
                return result

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.sync_once()
            except Exception as exc:  # keep the API alive if a source is bad
                LOG.exception("RAG background sync failed")
                with self._state_lock:
                    self._last_error = str(exc)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "source_dir": str(self.source_dir),
                "interval_seconds": self.interval_seconds,
                "sync_count": self._sync_count,
                "last_sync_at": self._last_sync_at.isoformat()
                if self._last_sync_at
                else None,
                "last_error": self._last_error,
                "last_result": self._last_result,
            }
