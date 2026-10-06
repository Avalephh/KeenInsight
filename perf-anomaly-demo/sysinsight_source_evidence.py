#!/usr/bin/env python3
"""Build source and runtime call-chain evidence for the SysInsight prompt.

The original SysInsight input keeps only flat function sample rates.  That is
useful for matching knobs, but it loses the sampled stack path and the source
lines that justify a parameter/function association.  This module restores
both pieces without inventing a static call graph:

* runtime call chains come from the real ``perf`` folded-stack artifact;
* source excerpts come from the pinned PostgreSQL tree and the generated
  association library;
* every relation retains its source file, line and source digest.

The result is intentionally described as sampled evidence.  It must not be
read as a proof that a parameter has a particular performance direction; the
real workload/API measurement remains the authority for that decision.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_SOURCE_RELATIVE = Path("third_party/postgresql-12.22")
DEFAULT_ASSOCIATION = (
    ROOT / "db_profiles" / "postgresql" / "common" / "function_parameter_association.json"
)

MAX_KNOBS = 8
MAX_EVIDENCE_PER_KNOB = 3
MAX_RUNTIME_CHAINS = 8
MAX_FUNCTION_LINES = 60
MAX_RENDERED_CHARS = 30000


def _mask_c_comments_and_literals(text: str) -> str:
    """Blank C comments and literals while preserving offsets and newlines."""

    result: List[str] = []
    state = "code"
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if state == "code":
            if text.startswith("//", index):
                result.extend((" ", " "))
                index += 2
                state = "line_comment"
            elif text.startswith("/*", index):
                result.extend((" ", " "))
                index += 2
                state = "block_comment"
            elif char in ("'", '"'):
                result.append(" ")
                index += 1
                quote = char
                state = "literal"
            else:
                result.append(char)
                index += 1
        elif state == "line_comment":
            if char == "\n":
                result.append(char)
                index += 1
                state = "code"
            else:
                result.append(" ")
                index += 1
        elif state == "block_comment":
            if text.startswith("*/", index):
                result.extend((" ", " "))
                index += 2
                state = "code"
            else:
                result.append("\n" if char == "\n" else " ")
                index += 1
        else:
            if char == "\\":
                result.append(" ")
                index += 1
                if index < len(text):
                    result.append("\n" if text[index] == "\n" else " ")
                    index += 1
            elif char == quote:
                result.append(" ")
                index += 1
                state = "code"
            else:
                result.append("\n" if char == "\n" else " ")
                index += 1
    return "".join(result)


def _function_spans(masked_text: str) -> List[Tuple[int, int, str]]:
    """Find C function line spans using the same declaration shape as the builder."""

    patterns = (
        re.compile(
            r"(?m)^\s*(?:static\s+|inline\s+|extern\s+|const\s+|volatile\s+)*"
            r"(?:[A-Za-z_]\w*[\s\*]+)+(?P<name>[A-Za-z_]\w*)\s*"
            r"\([^;{}]*?\)\s*\{"
        ),
        re.compile(r"(?m)^\s*(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*?\)\s*\{"),
    )
    starts: List[Tuple[int, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(masked_text):
            name = match.group("name")
            if name not in {"if", "for", "while", "switch", "catch"}:
                starts.append((masked_text.count("\n", 0, match.start()) + 1, name))
    starts = sorted(set(starts))
    line_count = masked_text.count("\n") + 1
    return [
        (
            start,
            starts[index + 1][0] - 1 if index + 1 < len(starts) else line_count,
            name,
        )
        for index, (start, name) in enumerate(starts)
    ]


def resolve_source_root() -> Optional[Path]:
    """Find the checked-out source tree without requiring it in Git."""

    configured = os.environ.get("POSTGRES_SOURCE_ROOT", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            PROJECT_ROOT / DEFAULT_SOURCE_RELATIVE,
            Path("/root/keeninsight-postgres") / DEFAULT_SOURCE_RELATIVE,
        ]
    )
    for candidate in candidates:
        if candidate and (candidate / "src" / "backend").is_dir():
            return candidate.resolve()
    return None


def _association_path(profile: Optional[Mapping[str, Any]]) -> Path:
    if isinstance(profile, Mapping):
        files = profile.get("files", {})
        if isinstance(files, Mapping):
            value = files.get("association")
            if value:
                candidate = Path(str(value))
                if candidate.exists():
                    return candidate.resolve()
    return DEFAULT_ASSOCIATION


def _source_path(source_root: Path, relative: str) -> Optional[Path]:
    candidate = source_root / str(relative)
    if candidate.is_file():
        return candidate
    return None


def _function_excerpt(source_root: Path, relative: str, line_number: Any) -> Dict[str, Any]:
    """Return a bounded real source excerpt containing ``line_number``."""

    try:
        line = int(line_number)
    except (TypeError, ValueError):
        line = 0
    source_path = _source_path(source_root, relative)
    if source_path is None:
        return {"status": "source_file_missing", "file": relative, "line": line}
    try:
        raw = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"status": "source_file_unreadable", "file": relative, "line": line, "error": str(exc)}

    lines = raw.splitlines()
    masked = _mask_c_comments_and_literals(raw)
    spans = _function_spans(masked)
    containing = next(
        ((start, end, name) for start, end, name in spans if start <= line <= end),
        None,
    )
    if containing is None:
        start = max(1, line - 5)
        end = min(len(lines), line + 5)
        function_name = None
    else:
        start, end, function_name = containing

    if end - start + 1 > MAX_FUNCTION_LINES:
        head = max(1, MAX_FUNCTION_LINES - 20)
        selected = lines[start - 1 : start - 1 + head]
        selected.append("... [source excerpt truncated] ...")
        selected.extend(lines[max(start - 1 + head, end - 12) : end])
    else:
        selected = lines[start - 1 : end]
    return {
        "status": "completed",
        "file": relative,
        "line": line,
        "line_range": [start, end],
        "function": function_name,
        "code": "\n".join(selected),
    }


def _key_function_names(source_detection: Mapping[str, Any]) -> List[str]:
    compare = source_detection.get("source_compare", {})
    values = compare.get("key_functions", []) if isinstance(compare, Mapping) else []
    result: List[str] = []
    for item in values:
        if isinstance(item, Mapping):
            value = item.get("Function") or item.get("function")
        elif isinstance(item, (list, tuple)) and item:
            value = item[0]
        else:
            value = item
        if value and str(value) not in result:
            result.append(str(value))
    return result


def _source_detection(case_result: Mapping[str, Any]) -> Dict[str, Any]:
    value = case_result.get("sysinsight_source_detection")
    if not isinstance(value, Mapping) or not value:
        anomaly = case_result.get("anomaly", {})
        value = anomaly.get("sysinsight_source_detection", {}) if isinstance(anomaly, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _matched_knobs(source_detection: Mapping[str, Any]) -> List[str]:
    match = source_detection.get("source_match", {})
    values = match.get("matched_knob", []) if isinstance(match, Mapping) else []
    result: List[str] = []
    for item in values:
        value = item.get("knob_name") if isinstance(item, Mapping) else item
        if value and str(value) not in result:
            result.append(str(value))
    return result[:MAX_KNOBS]


def _folded_candidates(case_result: Mapping[str, Any], case_path: Optional[Path]) -> List[Path]:
    candidates: List[Path] = []
    case_dir = case_path.parent if case_path else None
    anomaly = case_result.get("anomaly", {})
    postprocess = anomaly.get("perf_postprocess", {}) if isinstance(anomaly, Mapping) else {}
    if isinstance(postprocess, Mapping):
        value = postprocess.get("folded_path")
        if value:
            raw = Path(str(value))
            candidates.extend([raw, ROOT / raw, case_dir / raw if case_dir else raw])
    if case_dir:
        candidates.extend([case_dir / "anomaly" / "anomaly.folded", case_dir / "anomaly.folded"])
        candidates.extend(sorted(case_dir.glob("**/*.folded")))
    result: List[Path] = []
    seen: Set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and str(resolved) not in seen:
            seen.add(str(resolved))
            result.append(resolved)
    return result


def _runtime_call_chains(
    case_result: Mapping[str, Any], case_path: Optional[Path], key_functions: Set[str]
) -> List[Dict[str, Any]]:
    if not key_functions:
        return []
    aggregated: Dict[str, int] = {}
    source_path = ""
    for folded_path in _folded_candidates(case_result, case_path):
        source_path = str(folded_path)
        try:
            with folded_path.open(encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line or " " not in raw_line:
                        continue
                    stack, count_text = raw_line.rsplit(" ", 1)
                    try:
                        count = int(count_text)
                    except ValueError:
                        continue
                    frames = [item for item in stack.split(";") if item and item != "[unknown]"]
                    if not frames or not key_functions.intersection(frames):
                        continue
                    normalized = ";".join(frames)
                    aggregated[normalized] = aggregated.get(normalized, 0) + max(0, count)
        except OSError:
            continue
        if aggregated:
            break
    result = []
    for stack, samples in sorted(aggregated.items(), key=lambda item: item[1], reverse=True)[:MAX_RUNTIME_CHAINS]:
        frames = stack.split(";")
        result.append(
            {
                "source": "perf_folded_stack",
                "artifact": source_path,
                "samples": samples,
                "matched_key_functions": sorted(key_functions.intersection(frames)),
                "frames": frames,
            }
        )
    return result


def _static_parameter_evidence(
    association_path: Path,
    source_root: Optional[Path],
    knob_names: Sequence[str],
    key_functions: Set[str],
) -> List[Dict[str, Any]]:
    if not association_path.is_file():
        return []
    try:
        payload = json.loads(association_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    by_knob = {
        str(item.get("knob_name")): item
        for item in payload
        if isinstance(item, Mapping) and item.get("knob_name")
    }
    result: List[Dict[str, Any]] = []
    for knob_name in knob_names:
        item = by_knob.get(knob_name)
        if not item:
            continue
        evidence = item.get("source_evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        ranked = sorted(
            [row for row in evidence if isinstance(row, Mapping)],
            key=lambda row: (
                0 if str(row.get("function", "")) in key_functions else 1,
                0 if row.get("context") == "control_flow" else 1,
                str(row.get("file", "")),
                int(row.get("line", 0) or 0),
            ),
        )
        seen: Set[Tuple[str, int, str]] = set()
        selected = []
        for row in ranked:
            identity = (str(row.get("file", "")), int(row.get("line", 0) or 0), str(row.get("function", "")))
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(row)
            if len(selected) >= MAX_EVIDENCE_PER_KNOB:
                break
        for row in selected:
            evidence_row: Dict[str, Any] = {
                "knob_name": knob_name,
                "guc_variable": item.get("guc_variable"),
                "function": row.get("function"),
                "context": row.get("context") or row.get("association_relation"),
                "file": row.get("file"),
                "line": row.get("line"),
                "source_line": row.get("source_line"),
                "source_sha256": row.get("source_sha256"),
                "evidence_type": row.get("evidence_type", "direct_source_identifier_use"),
            }
            if source_root and row.get("file"):
                evidence_row["source_excerpt"] = _function_excerpt(
                    source_root, str(row.get("file")), row.get("line")
                )
            result.append(evidence_row)
    return result


def source_code_for_matched_knobs(
    data: Any,
    profile: Optional[Mapping[str, Any]] = None,
    max_functions_per_knob: int = 12,
) -> List[Dict[str, Any]]:
    """Provide the original ``extractCode`` return shape from PG source.

    The acquired extractor expects one ``<knob>_code.txt`` file per parameter.
    PostgreSQL uses a pinned source tree instead of fabricated text files, so
    the wrapper supplies the same shape from the association provenance.  The
    function is deliberately bounded; the full association remains available
    in the profile artifact and the prompt renderer applies its own budget.
    """

    if not isinstance(data, list):
        return []
    source_root = resolve_source_root()
    association_path = _association_path(profile)
    if source_root is None or not association_path.is_file():
        return [
            {"knob_name": item.get("knob_name"), "data_flow_functions_code": []}
            for item in data
            if isinstance(item, Mapping)
        ]
    try:
        payload = json.loads(association_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = []
    by_knob = {
        str(item.get("knob_name")): item
        for item in payload
        if isinstance(item, Mapping) and item.get("knob_name")
    }
    result: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        knob_name = str(item.get("knob_name", ""))
        association = by_knob.get(knob_name, {})
        rows = association.get("source_evidence", [])
        rows_by_function: Dict[str, Mapping[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping) and row.get("function"):
                    rows_by_function.setdefault(str(row["function"]), row)
        functions = item.get("data_flow_functions", [])
        if not isinstance(functions, list):
            functions = []
        code_rows: List[Dict[str, Any]] = []
        for function in [str(value) for value in functions[:max_functions_per_knob]]:
            row = rows_by_function.get(function)
            if not row:
                continue
            excerpt = _function_excerpt(source_root, str(row.get("file", "")), row.get("line"))
            if excerpt.get("status") != "completed" or not excerpt.get("code"):
                continue
            code_rows.append({"function": function, "code": excerpt["code"]})
        result.append({"knob_name": knob_name, "data_flow_functions_code": code_rows})
    return result


def build_source_evidence(
    case_result: Mapping[str, Any],
    case_path: Optional[Path] = None,
    profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build bounded, provenance-bearing source and runtime call evidence."""

    source_detection = _source_detection(case_result)
    ordered_key_functions = _key_function_names(source_detection)
    key_functions = set(ordered_key_functions)
    knob_names = _matched_knobs(source_detection)
    association_path = _association_path(profile)
    source_root = resolve_source_root()
    # The original exception comparator can return hundreds of functions.  A
    # folded stack containing any of them would otherwise be dominated by
    # generic PostgresMain/kernel paths.  Keep the highest-ranked anomaly
    # functions, then add the functions explicitly selected by the matcher.
    runtime_targets = set(ordered_key_functions[:80])
    source_match = source_detection.get("source_match", {})
    matched_rows = source_match.get("matched_knob", []) if isinstance(source_match, Mapping) else []
    for row in matched_rows[:MAX_KNOBS] if isinstance(matched_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        runtime_targets.update(str(value) for value in row.get("data_flow_functions", [])[:4])
        runtime_targets.update(str(value) for value in row.get("control_flow_functions", [])[:4])
    runtime = _runtime_call_chains(case_result, case_path, runtime_targets)
    static = _static_parameter_evidence(
        association_path,
        source_root,
        knob_names,
        key_functions,
    )
    provenance: Dict[str, Any] = {}
    provenance_path = association_path.with_name("function_parameter_association.provenance.json")
    if provenance_path.is_file():
        try:
            loaded = json.loads(provenance_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                provenance = {
                    "source_root": loaded.get("source_root"),
                    "source_revision": loaded.get("source_revision"),
                    "source_digest": loaded.get("source_digest"),
                }
        except (OSError, ValueError):
            provenance = {}
    status = "completed" if runtime or static else "unavailable"
    reason = None
    if status == "unavailable":
        if not source_detection:
            reason = "source detection is not present in case_result"
        elif not source_root:
            reason = "pinned PostgreSQL source tree is not available"
        elif not association_path.is_file():
            reason = "source association library is not available"
        else:
            reason = "no matched source evidence or folded call chain was found"
    return {
        "status": status,
        "reason": reason,
        "semantics": {
            "runtime_call_chain": "perf folded-stack samples; frame order is preserved",
            "static_source_relation": "GUC binding plus direct source identifier use",
            "not_claimed": "complete static call graph or performance direction",
        },
        "provenance": provenance,
        "association_file": str(association_path),
        "matched_key_function_count": len(key_functions),
        "matched_knob_count": len(knob_names),
        "runtime_call_chains": runtime,
        "static_parameter_evidence": static,
    }


def render_source_evidence(evidence: Mapping[str, Any], max_chars: int = MAX_RENDERED_CHARS) -> str:
    """Render evidence into the original SysInsight prompt language."""

    runtime_lines = [
        "10. Runtime sampled call-chain and PostgreSQL source evidence:",
        "    The call chains below are real perf folded-stack samples. They preserve the observed frame order, but they are not a complete static call graph.",
    ]
    chains = evidence.get("runtime_call_chains", [])
    if isinstance(chains, list) and chains:
        for item in chains:
            if not isinstance(item, Mapping):
                continue
            frames = " -> ".join(str(frame) for frame in item.get("frames", []))
            matched = ", ".join(str(value) for value in item.get("matched_key_functions", []))
            runtime_lines.append("    - samples={}; matched key functions={}; call chain={}".format(item.get("samples", 0), matched, frames))
    else:
        runtime_lines.append("    - No runtime folded-stack chain was available for this case.")

    static_lines = [
        "11. PostgreSQL source evidence:",
        "    The source records below are extracted from the pinned PostgreSQL tree and retain file, line, context and SHA-256 evidence.",
    ]
    records = evidence.get("static_parameter_evidence", [])
    if isinstance(records, list) and records:
        for item in records:
            if not isinstance(item, Mapping):
                continue
            static_lines.append(
                "    - parameter={} (internal variable={}) -> {} [{}] at {}:{}; evidence={}; source_sha256={}".format(
                    item.get("knob_name"),
                    item.get("guc_variable"),
                    item.get("function"),
                    item.get("context"),
                    item.get("file"),
                    item.get("line"),
                    item.get("evidence_type"),
                    item.get("source_sha256"),
                )
            )
            static_lines.append("      source line: {}".format(item.get("source_line", "")))
            excerpt = item.get("source_excerpt", {})
            code = excerpt.get("code") if isinstance(excerpt, Mapping) else None
            if code:
                static_lines.append("      containing function source excerpt:")
                code_lines = str(code).splitlines()
                if len(code_lines) > 32:
                    code_lines = code_lines[:28] + ["... [source excerpt truncated] ..."] + code_lines[-3:]
                static_lines.extend("        " + line for line in code_lines)
    else:
        static_lines.append("    - No static source excerpt was available for the matched parameters.")

    runtime_text = "\n".join(runtime_lines)
    static_text = "\n".join(static_lines)
    # Keep both forms of evidence visible.  A long C function must not push
    # all provenance lines out of the prompt budget.
    runtime_budget = min(8000, max_chars // 3)
    static_budget = max_chars - runtime_budget - 80
    if len(runtime_text) > runtime_budget:
        runtime_text = runtime_text[: runtime_budget - 50] + "\n    ... [runtime chains truncated] ..."
    if len(static_text) > static_budget:
        static_text = static_text[: static_budget - 50] + "\n    ... [source evidence truncated by prompt budget] ..."
    rendered = runtime_text + "\n\n" + static_text
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 80] + "\n    ... [source evidence truncated by prompt budget] ..."
