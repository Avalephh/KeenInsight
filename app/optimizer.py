"""Evidence-grounded query tuning agent.

The agent retrieves relevant operational knowledge, inspects a plan, produces
only typed candidates, verifies each candidate on the connected PostgreSQL
instance, checks result equivalence, and ranks the measured alternatives.
Hints are session-local and rewrites are restricted to a provably equivalent
pattern; neither path mutates the database.  Index candidates are returned as
the existing reviewed action type rather than being silently applied.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .db import Database
from .diagnose import explain_query, flatten_plan, summarize_plan, validate_read_only_query
from .llm import LLMClient, LLMError, parse_json_object
from .rag import search as rag_search


class OptimizerError(RuntimeError):
    pass


ALLOWED_HINTS = {
    "enable_hashjoin": {"on", "off"},
    "enable_mergejoin": {"on", "off"},
    "enable_nestloop": {"on", "off"},
    "enable_seqscan": {"on", "off"},
    "enable_indexscan": {"on", "off"},
    "enable_bitmapscan": {"on", "off"},
    # These are still session-local and allowlisted.  The agent can test a
    # query-specific resource/parallelism hypothesis without changing the
    # database default or affecting another session.
    "work_mem": {"4MB", "8MB", "16MB", "32MB", "64MB", "128MB"},
    "max_parallel_workers_per_gather": {"0", "1", "2", "3", "4", "8"},
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _canonical_result(rows: list[Any], *, order_sensitive: bool = False) -> tuple[int, str]:
    records = [
        json.dumps(row, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))
        for row in rows
    ]
    # SQL result order is not part of the semantics unless ORDER BY is
    # present.  Compare the observed result bag in the unordered case so a
    # valid hint/rewrite is not rejected merely because PostgreSQL selected a
    # different scan order; duplicates are retained by sorting records rather
    # than converting them to a set.
    if not order_sensitive:
        records.sort()
    text = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    return len(rows), hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_plan(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        raise OptimizerError("PostgreSQL returned an invalid JSON plan")
    root = raw.get("Plan") or {}
    nodes = flatten_plan(root)
    return {"document": raw, "root": root, "nodes": nodes, "summary": summarize_plan(raw, nodes)}


class QueryTuningAgent:
    def __init__(
        self,
        db: Database,
        *,
        rag_retriever: Callable[..., list[dict[str, Any]]] | None = None,
        source_evidence: Callable[[], dict[str, Any]] | None = None,
        llm_url: str | None = None,
        llm_model: str | None = None,
        llm_token: str | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.db = db
        self.rag_retriever = rag_retriever or rag_search
        self.source_evidence = source_evidence
        self.llm_client = llm_client or LLMClient(
            url=llm_url,
            model=llm_model,
            token=llm_token,
        )
        self._last_llm_generation: dict[str, Any] = {
            "attempted": False,
            "accepted_count": 0,
            "reason": "not_run",
        }

    def status(self) -> dict[str, Any]:
        run_count = self.db.fetch_one("SELECT count(*)::int AS count FROM keeninsight.optimizer_runs") or {}
        candidate_count = self.db.fetch_one("SELECT count(*)::int AS count FROM keeninsight.optimizer_candidates") or {}
        llm_status = self.llm_client.status()
        return {
            "agent": "retrieval + typed candidate generation + PostgreSQL verification",
            "generator": "external-llm-validated" if llm_status["configured"] else "evidence-rule-agent",
            "llm_configured": llm_status["configured"],
            "llm": llm_status,
            "optimizer_runs": int(run_count.get("count") or 0),
            "candidate_count": int(candidate_count.get("count") or 0),
        }

    def optimize(self, query: str, *, repetitions: int = 3) -> dict[str, Any]:
        self._last_llm_generation = {
            "attempted": False,
            "accepted_count": 0,
            "reason": "not_configured",
        }
        normalized = validate_read_only_query(query)
        baseline_plan = explain_query(self.db, normalized, analyze=False)
        knowledge = self.rag_retriever(self.db, normalized, top_k=5)
        source_evidence = self._source_evidence(baseline_plan)
        run = self.db.fetch_one(
            """
            INSERT INTO keeninsight.optimizer_runs
                (query, status, baseline, knowledge, source_evidence)
            VALUES (%s, 'running', %s::jsonb, %s::jsonb, %s::jsonb)
            RETURNING id
            """,
            (normalized, _json({"query": normalized, "plan": baseline_plan}), _json(knowledge), _json(source_evidence)),
        )
        if not run:
            raise OptimizerError("could not persist optimizer run")
        run_id = int(run["id"])
        try:
            candidates = self._generate_candidates(normalized, baseline_plan)
            # Optional LLM output is additive.  Every returned candidate is
            # passed through the same strict validator and verified below.
            candidates.extend(self._llm_candidates(normalized, baseline_plan))
            unique: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                unique.setdefault(str(candidate["candidate_key"]), candidate)
            candidate_results = []
            baseline_measurement = self._benchmark(normalized, {}, repetitions=repetitions)
            for candidate in unique.values():
                try:
                    if candidate.get("action_type") in {"index_selection", "index_removal"}:
                        # Index creation is a reviewed database action, not a
                        # hidden side effect of query optimization.  The same
                        # review gate applies to a typed removal proposal.
                        item = {
                            **candidate,
                            "run_id": run_id,
                            "plan": baseline_measurement.get("plan") or {},
                            "measurement": {
                                "status": "proposal_only",
                                "reason": "requires explicit reviewed index action",
                            },
                            "semantic_equivalence": {
                                "equivalent": False,
                                "verified": False,
                                "reason": "candidate was not applied during optimization",
                            },
                            "score": float("-inf"),
                        }
                        row = self.db.fetch_one(
                            """
                            INSERT INTO keeninsight.optimizer_candidates
                                (run_id, candidate_key, action_type, query, hints,
                                 proposal, plan, measurement, semantic_equivalence, score)
                            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                                    %s::jsonb, %s::jsonb, NULL)
                            RETURNING id, run_id, candidate_key, action_type, query,
                                      hints, proposal, plan, measurement,
                                      semantic_equivalence, score, rank, created_at
                            """,
                            (
                                run_id, item["candidate_key"], item["action_type"],
                                item["query"], _json(item.get("hints") or {}),
                                _json(item.get("proposal") or {}), _json(item.get("plan") or {}),
                                _json(item.get("measurement") or {}), _json(item.get("semantic_equivalence") or {}),
                            ),
                        )
                        item["id"] = row["id"] if row else None
                        candidate_results.append(item)
                        continue
                    measured = self._benchmark(
                        candidate["query"], candidate.get("hints") or {}, repetitions=repetitions
                    )
                    semantic = {
                        "equivalent": measured["result_hash"] == baseline_measurement["result_hash"] and measured["result_count"] == baseline_measurement["result_count"],
                        "baseline_result_count": baseline_measurement["result_count"],
                        "candidate_result_count": measured["result_count"],
                        "baseline_result_hash": baseline_measurement["result_hash"],
                        "candidate_result_hash": measured["result_hash"],
                        "method": "same-session-read-only-result-hash",
                    }
                    score = self._score(measured, semantic)
                    plan = measured.pop("plan")
                    item = {
                        **candidate,
                        "run_id": run_id,
                        "plan": plan,
                        "measurement": measured,
                        "semantic_equivalence": semantic,
                        "score": score,
                    }
                except Exception as exc:
                    item = {
                        **candidate,
                        "run_id": run_id,
                        "plan": {},
                        "measurement": {"error": str(exc)},
                        "semantic_equivalence": {"equivalent": False, "error": str(exc)},
                        "score": float("-inf"),
                    }
                row = self.db.fetch_one(
                    """
                    INSERT INTO keeninsight.optimizer_candidates
                        (run_id, candidate_key, action_type, query, hints,
                         proposal, plan, measurement, semantic_equivalence, score)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                            %s::jsonb, %s::jsonb, %s)
                    RETURNING id, run_id, candidate_key, action_type, query,
                              hints, proposal, plan, measurement,
                              semantic_equivalence, score, rank, created_at
                    """,
                    (
                        run_id,
                        item["candidate_key"],
                        item["action_type"],
                        item["query"],
                        _json(item.get("hints") or {}),
                        _json(item.get("proposal") or {}),
                        _json(item.get("plan") or {}),
                        _json(item.get("measurement") or {}),
                        _json(item.get("semantic_equivalence") or {}),
                        item["score"] if math.isfinite(item["score"]) else None,
                    ),
                )
                item["id"] = row["id"] if row else None
                candidate_results.append(item)
            ranking = self._rank_candidates(run_id, candidate_results)
            selected = ranking[0]["candidate_key"] if ranking else None
            finished = self.db.fetch_one(
                """
                UPDATE keeninsight.optimizer_runs
                SET status = 'completed', ranking = %s::jsonb,
                    selected_candidate = %s, finished_at = now()
                WHERE id = %s
                RETURNING id, query, status, baseline, knowledge,
                          source_evidence, selected_candidate, ranking,
                          created_at, finished_at, error
                """,
                (_json(ranking), selected, run_id),
            )
            return {
                "run": finished,
                "agent": self.status(),
                "baseline": {"plan": baseline_plan, "measurement": baseline_measurement},
                "knowledge": knowledge,
                "source_evidence": source_evidence,
                "candidates": candidate_results,
                "ranking": ranking,
                "selected_candidate": selected,
                "llm_generation": dict(self._last_llm_generation),
            }
        except Exception as exc:
            self.db.execute(
                "UPDATE keeninsight.optimizer_runs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                (str(exc), run_id),
            )
            if isinstance(exc, OptimizerError):
                raise
            raise OptimizerError("tuning agent failed: %s" % exc) from exc

    def verify_candidate(self, run_id: int, candidate_key: str) -> dict[str, Any]:
        row = self.db.fetch_one(
            """
            SELECT id, run_id, candidate_key, action_type, query, hints,
                   proposal, plan, measurement, semantic_equivalence, score, rank
            FROM keeninsight.optimizer_candidates
            WHERE run_id = %s AND candidate_key = %s
            """,
            (run_id, candidate_key),
        )
        if not row:
            raise KeyError("optimizer candidate was not found")
        if row.get("action_type") in {"index_selection", "index_removal"}:
            return {
                "candidate": row,
                "verification": {
                    "status": "proposal_only",
                    "verified": False,
                    "requires_explicit_reviewed_action": True,
                    "reason": "apply the typed reviewed index proposal, then rerun the workload/query protocol",
                },
            }
        measured = self._benchmark(row["query"], row.get("hints") or {}, repetitions=3)
        semantic = dict(row.get("semantic_equivalence") or {})
        baseline = self.db.fetch_one("SELECT baseline FROM keeninsight.optimizer_runs WHERE id = %s", (run_id,)) or {}
        baseline_measurement = self._benchmark(str((baseline.get("baseline") or {}).get("query") or row["query"]), {}, repetitions=3)
        semantic.update(
            {
                "verification_result_hash": measured["result_hash"],
                "verification_result_count": measured["result_count"],
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        semantic["equivalent"] = measured["result_hash"] == baseline_measurement["result_hash"] and measured["result_count"] == baseline_measurement["result_count"]
        return {"candidate": row, "verification": {"baseline": baseline_measurement, "candidate": measured, "semantic_equivalence": semantic}}

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT id, query, status, baseline, knowledge, source_evidence,
                   selected_candidate, ranking, created_at, finished_at, error
            FROM keeninsight.optimizer_runs ORDER BY created_at DESC LIMIT %s
            """,
            (max(1, min(int(limit), 200)),),
        )

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            """
            SELECT id, query, status, baseline, knowledge, source_evidence,
                   selected_candidate, ranking, created_at, finished_at, error
            FROM keeninsight.optimizer_runs WHERE id = %s
            """,
            (run_id,),
        )
        if row:
            row["candidates"] = self.db.fetch_all(
                """
                SELECT id, run_id, candidate_key, action_type, query, hints,
                       proposal, plan, measurement, semantic_equivalence,
                       score, rank, created_at
                FROM keeninsight.optimizer_candidates WHERE run_id = %s
                ORDER BY rank NULLS LAST, score DESC NULLS LAST, id
                """,
                (run_id,),
            )
        return row

    def _generate_candidates(self, query: str, baseline: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        nodes = baseline.get("nodes") or []
        node_types = {str(node.get("Node Type")) for node in nodes}
        hint_order = []
        if "Hash Join" in node_types:
            hint_order.append(("enable_hashjoin", "off"))
        if "Merge Join" in node_types:
            hint_order.append(("enable_mergejoin", "off"))
        if "Nested Loop" in node_types:
            hint_order.append(("enable_nestloop", "off"))
        if "Seq Scan" in node_types:
            hint_order.append(("enable_seqscan", "off"))
        if node_types.intersection({"Sort", "Incremental Sort", "Hash Join", "HashAggregate", "Aggregate", "WindowAgg"}):
            current = self.db.fetch_one(
                "SELECT setting FROM pg_settings WHERE name = 'work_mem'"
            ) or {}
            current_kb = _memory_kb(current.get("setting"))
            for target_kb in (16 * 1024, 32 * 1024, 64 * 1024, 128 * 1024):
                if target_kb > current_kb:
                    target = "%dMB" % (target_kb // 1024)
                    hint_order.append(("work_mem", target))
                    break
        if any(value in node_types for value in ("Gather", "Gather Merge")):
            current = self.db.fetch_one(
                "SELECT setting FROM pg_settings WHERE name = 'max_parallel_workers_per_gather'"
            ) or {}
            try:
                current_workers = int(current.get("setting") or 0)
            except (TypeError, ValueError):
                current_workers = 0
            target_workers = min(8, max(1, current_workers + 2))
            if target_workers > current_workers:
                hint_order.append(("max_parallel_workers_per_gather", str(target_workers)))
        for parameter, value in hint_order:
            hints = {parameter: value}
            candidates.append(
                {
                    "candidate_key": "hint:%s=%s" % (parameter, value),
                    "action_type": "query_hint",
                    "query": query,
                    "hints": hints,
                    "proposal": {"kind": "session_local_hint", "parameter": parameter, "value": value},
                }
            )
        try:
            # Reuse the same conservative plan-to-index derivation as the
            # reviewed action path.  The proposal is useful even though this
            # agent never creates a persistent index as a side effect.
            from .workload import _index_proposal_from_plan

            index_proposal = _index_proposal_from_plan(self.db, baseline)
        except Exception:
            index_proposal = None
        if index_proposal:
            target = index_proposal.get("target") or {}
            candidates.append(
                {
                    "candidate_key": "index-selection:%s.%s" % (target.get("table"), target.get("index")),
                    "action_type": "index_selection",
                    "query": query,
                    "hints": {},
                    "proposal": {
                        **index_proposal,
                        "requires_explicit_reviewed_action": True,
                    },
                }
            )
        try:
            removal = self._unused_index_proposal(baseline)
        except Exception:
            removal = None
        if removal:
            target = removal.get("target") or {}
            candidates.append(
                {
                    "candidate_key": "index-removal:%s.%s" % (target.get("table"), target.get("index")),
                    "action_type": "index_removal",
                    "query": query,
                    "hints": {},
                    "proposal": {
                        **removal,
                        "requires_explicit_reviewed_action": True,
                    },
                }
            )
        rewritten = rewrite_query(query)
        if rewritten and rewritten != query:
            candidates.append(
                {
                    "candidate_key": "rewrite:or-to-in",
                    "action_type": "query_rewrite",
                    "query": rewritten,
                    "hints": {},
                    "proposal": {"kind": "provably_equivalent", "rewrite": "OR equality predicates -> IN list", "original_query": query},
                }
            )
        return candidates

    def _unused_index_proposal(self, baseline: dict[str, Any]) -> dict[str, Any] | None:
        """Return one zero-scan, non-constraint index proposal for review."""
        relations = sorted(
            {
                str(node.get("Relation Name"))
                for node in (baseline.get("nodes") or [])
                if node.get("Relation Name") and node.get("Schema")
            }
        )
        if not relations:
            return None
        row = self.db.fetch_one(
            """
            SELECT s.schemaname AS schema_name, s.relname AS table_name,
                   s.indexrelname AS index_name, s.idx_scan,
                   c.oid::text AS index_oid,
                   pg_get_indexdef(c.oid) AS index_definition,
                   COALESCE(i.indisprimary, FALSE) AS indisprimary,
                   COALESCE(i.indisunique, FALSE) AS indisunique,
                   COALESCE(i.indisexclusion, FALSE) AS indisexclusion
            FROM pg_stat_user_indexes s
            JOIN pg_class c ON c.oid = s.indexrelid
            JOIN pg_index i ON i.indexrelid = s.indexrelid
            WHERE s.schemaname NOT IN ('keeninsight', 'pg_catalog', 'information_schema')
              AND s.relname = ANY(%s)
              AND s.idx_scan = 0
              AND NOT i.indisprimary AND NOT i.indisunique AND NOT i.indisexclusion
            ORDER BY s.indexrelname LIMIT 1
            """,
            (relations,),
        )
        if not row:
            return None
        return {
            "action_type": "drop_index",
            "target": {
                "schema": row["schema_name"],
                "table": row["table_name"],
                "index": row["index_name"],
            },
            "description": "Remove a zero-scan non-constraint index only after explicit review and post-change query verification.",
            "proposal_evidence": {
                "idx_scan": int(row.get("idx_scan") or 0),
                "index_oid": row.get("index_oid"),
                "index_definition": row.get("index_definition"),
                "relation_observed_in_plan": row["table_name"],
                "calculation": "pg_stat_user_indexes zero-scan plus non-constraint catalog checks",
            },
        }

    def _llm_candidates(self, query: str, baseline: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.llm_client.configured:
            return []

        payload = {
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return JSON only: {\"candidates\":[{\"action_type\":\"query_hint\",\"hints\":{\"enable_hashjoin\":\"off\"}}]}. Use only the allowlisted hint names. Do not write SQL, change database settings, or propose an index. These are hypotheses only; each candidate will be measured and result-checked by the server."},
                {"role": "user", "content": "Query:\n%s\nObserved PostgreSQL plan:\n%s" % (query, _json(baseline))},
            ],
        }
        try:
            result = self.llm_client.complete(
                payload["messages"],
                temperature=0,
                max_tokens=600,
            )
            parsed = parse_json_object(str(result.get("content") or ""))
            candidates = _validate_llm_candidates(query, parsed)
            for candidate in candidates:
                candidate.setdefault("proposal", {})["model"] = result.get("model") or self.llm_client.model
            self._last_llm_generation = {
                "attempted": True,
                "accepted_count": len(candidates),
                "model": result.get("model") or self.llm_client.model,
                "usage": result.get("usage") or {},
                "latency_ms": result.get("latency_ms"),
                "reason": "validated",
            }
            return candidates
        except (LLMError, ValueError, TypeError):
            # The deterministic evidence agent remains available, but the
            # response status never claims that an external model generated a
            # candidate when it did not.
            self._last_llm_generation = {
                "attempted": True,
                "accepted_count": 0,
                "reason": "request_or_validation_failed",
            }
            return []

    def _benchmark(self, query: str, hints: dict[str, Any], *, repetitions: int) -> dict[str, Any]:
        normalized = validate_read_only_query(query)
        clean_hints = validate_hints(hints)
        repetitions = max(1, min(int(repetitions), 7))
        with psycopg.connect(self.db.dsn, row_factory=dict_row, autocommit=False) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15000ms'")
            conn.execute("SET LOCAL lock_timeout = '2000ms'")
            for name, value in clean_hints.items():
                conn.execute(sql.SQL("SET LOCAL {} = {}").format(sql.Identifier(name), sql.Literal(value)))
            row = conn.execute(
                "EXPLAIN (ANALYZE TRUE, BUFFERS TRUE, FORMAT JSON, VERBOSE TRUE, SETTINGS TRUE) " + normalized
            ).fetchone()
            if not row:
                raise OptimizerError("EXPLAIN returned no plan")
            plan = _parse_plan(next(iter(row.values())))
            for _ in range(1):
                conn.execute(normalized).fetchall()
            timings = []
            result_rows: list[Any] = []
            for _ in range(repetitions):
                started = time.perf_counter()
                result_rows = conn.execute(normalized).fetchall()
                if len(result_rows) > 20000:
                    raise OptimizerError("candidate result exceeds the semantic verification limit of 20000 rows")
                timings.append((time.perf_counter() - started) * 1000.0)
            conn.rollback()
        result_count, result_hash = _canonical_result(
            result_rows, order_sensitive=bool(re.search(r"\border\s+by\b", normalized, re.IGNORECASE))
        )
        return {
            "timings_ms": timings,
            "median_query_time_ms": statistics.median(timings),
            "p95_query_time_ms": _percentile(timings, 0.95),
            "result_count": result_count,
            "result_hash": result_hash,
            "plan": plan,
            "hints": clean_hints,
        }

    @staticmethod
    def _score(measurement: dict[str, Any], semantic: dict[str, Any]) -> float:
        if not semantic.get("equivalent"):
            return float("-inf")
        plan = measurement.get("plan") or {}
        total_cost = _number(((plan.get("root") or {}).get("Total Cost")))
        return -_number(measurement.get("median_query_time_ms")) - total_cost * 0.0001

    def _rank_candidates(self, run_id: int, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        valid = [item for item in candidates if item.get("semantic_equivalence", {}).get("equivalent") and math.isfinite(item.get("score", float("-inf")))]
        pairwise = self._pairwise_rank_scores(run_id, valid)
        if pairwise.get("trained"):
            for item in valid:
                item["pairwise_score"] = pairwise["scores"].get(item["candidate_key"], 0.0)
            valid.sort(key=lambda item: (item.get("pairwise_score", 0.0), item["score"]), reverse=True)
        else:
            valid.sort(key=lambda item: item["score"], reverse=True)
        ranking = []
        for index, item in enumerate(valid, 1):
            self.db.execute(
                "UPDATE keeninsight.optimizer_candidates SET rank = %s WHERE id = %s",
                (index, item.get("id")),
            )
            ranking.append(
                {
                    "rank": index,
                    "candidate_key": item["candidate_key"],
                    "action_type": item["action_type"],
                    "score": item["score"],
                    "median_query_time_ms": item["measurement"].get("median_query_time_ms"),
                    "p95_query_time_ms": item["measurement"].get("p95_query_time_ms"),
                    "plan_total_cost": _number((item["plan"].get("root") or {}).get("Total Cost")),
                    "semantic_equivalent": True,
                    "ranking_method": "pairwise-logistic" if pairwise.get("trained") else "measured-latency-and-plan-cost",
                    "pairwise_model": pairwise,
                }
            )
        return ranking

    def _pairwise_rank_scores(self, current_run_id: int, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Fit a pairwise cost model from previous verified candidates."""
        try:
            import numpy as np
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
        except Exception as exc:
            return {"trained": False, "reason": "scikit-learn unavailable: %s" % exc}
        historical = self.db.fetch_all(
            """
            SELECT run_id, candidate_key, plan, measurement, score
            FROM keeninsight.optimizer_candidates
            WHERE run_id <> %s AND score IS NOT NULL
              AND semantic_equivalence->>'equivalent' = 'true'
            ORDER BY id DESC LIMIT 500
            """,
            (current_run_id,),
        )
        by_run: dict[int, list[dict[str, Any]]] = {}
        for row in historical:
            by_run.setdefault(int(row["run_id"]), []).append(row)
        pair_features = []
        labels = []
        for rows in by_run.values():
            for left_index in range(len(rows)):
                for right_index in range(left_index + 1, len(rows)):
                    left, right = rows[left_index], rows[right_index]
                    if left.get("score") == right.get("score"):
                        continue
                    left_features = self._candidate_features(left)
                    right_features = self._candidate_features(right)
                    pair_features.append([left_features[index] - right_features[index] for index in range(len(left_features))])
                    labels.append(1 if float(left["score"]) > float(right["score"]) else 0)
        if len(pair_features) < 6 or len(set(labels)) < 2:
            return {"trained": False, "reason": "fewer than six historical pairwise comparisons", "pair_count": len(pair_features)}
        matrix = np.asarray(pair_features, dtype=float)
        scaler = StandardScaler()
        scaled = scaler.fit_transform(matrix)
        model = LogisticRegression(max_iter=2000, random_state=17)
        model.fit(scaled, np.asarray(labels, dtype=int))
        score_map: dict[str, float] = {}
        for item in candidates:
            left = np.asarray([self._candidate_features(item)], dtype=float)
            wins = 0.0
            for other in candidates:
                if other is item:
                    continue
                diff = left - np.asarray([self._candidate_features(other)], dtype=float)
                wins += float(model.predict_proba(scaler.transform(diff))[0][1])
            score_map[item["candidate_key"]] = wins
        return {
            "trained": True,
            "algorithm": "StandardScaler + pairwise LogisticRegression",
            "historical_run_count": len(by_run),
            "pair_count": len(pair_features),
            "scores": score_map,
            "feature_names": ["median_query_time_ms", "p95_query_time_ms", "plan_total_cost", "plan_node_count"],
        }

    @staticmethod
    def _candidate_features(row: dict[str, Any]) -> list[float]:
        measurement = row.get("measurement") or {}
        plan = row.get("plan") or {}
        root = plan.get("root") or {}
        nodes = plan.get("nodes") or []
        return [
            _number(measurement.get("median_query_time_ms")),
            _number(measurement.get("p95_query_time_ms")),
            _number(root.get("Total Cost")),
            float(len(nodes)),
        ]

    def _source_evidence(self, baseline: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.source_evidence:
            return []
        try:
            payload = self.source_evidence()
            return payload.get("items", []) if isinstance(payload, dict) else []
        except Exception:
            return []


def validate_hints(hints: dict[str, Any]) -> dict[str, str]:
    if not isinstance(hints, dict) or len(hints) > 4:
        raise OptimizerError("query hints must be an object containing at most four settings")
    normalized = {}
    for name, value in hints.items():
        normalized_value = str(value).strip()
        allowed_values = ALLOWED_HINTS.get(name)
        if not allowed_values:
            raise OptimizerError("hint %r is not in the safe session-local allowlist" % name)
        if name in {"work_mem"}:
            comparison_value = normalized_value.upper()
            allowed_values = {item.upper() for item in allowed_values}
        else:
            comparison_value = normalized_value.lower()
        if comparison_value not in allowed_values:
            raise OptimizerError("hint %r is not in the safe session-local allowlist" % name)
        normalized[name] = comparison_value
    return normalized


def rewrite_query(query: str) -> str | None:
    """Rewrite only identical scalar equality disjunctions into IN."""
    normalized = validate_read_only_query(query)
    if re.search(r"\b(and|or)\b", normalized, re.IGNORECASE) is None:
        return None
    literal = r"(?:'(?:''|[^'])*'|[-+]?[0-9]+(?:\.[0-9]+)?)"
    pattern = re.compile(
        r"(?P<column>(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)\s*=\s*(?P<left>%s)\s+OR\s+"
        r"(?P=column)\s*=\s*(?P<right>%s)" % (literal, literal),
        re.IGNORECASE,
    )
    match = pattern.search(normalized)
    if not match:
        return None
    prefix = normalized[: match.start()]
    suffix = normalized[match.end() :]
    # Avoid changing a disjunction nested below a NOT or a non-boolean
    # context; the conservative pattern is intentionally top-level-ish.
    if prefix.count("(") != prefix.count(")"):
        return None
    replacement = "%s IN (%s, %s)" % (match.group("column"), match.group("left"), match.group("right"))
    return prefix + replacement + suffix


def _validate_llm_candidates(query: str, parsed: Any) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict) or not isinstance(parsed.get("candidates"), list):
        return []
    result = []
    for candidate in parsed["candidates"][:8]:
        if not isinstance(candidate, dict) or candidate.get("action_type") != "query_hint":
            continue
        try:
            hints = validate_hints(candidate.get("hints") or {})
        except OptimizerError:
            continue
        if not hints:
            continue
        key = "llm-hint:" + ",".join("%s=%s" % item for item in sorted(hints.items()))
        result.append({"candidate_key": key, "action_type": "query_hint", "query": query, "hints": hints, "proposal": {"kind": "external_llm_candidate", "validated": True}})
    return result


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _memory_kb(value: Any) -> float:
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(b|kb|mb|gb)?\s*$", str(value or ""), re.IGNORECASE)
    if not match:
        return 0.0
    amount = float(match.group(1))
    unit = (match.group(2) or "kb").lower()
    return amount * {"b": 1 / 1024.0, "kb": 1.0, "mb": 1024.0, "gb": 1024.0 * 1024.0}[unit]
