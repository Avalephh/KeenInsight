#!/usr/bin/env python3
"""Build a PostgreSQL function/parameter library from the pinned source tree.

The original SysInsight matcher consumes a JSON *list* with the keys
``knob_name``, ``data_flow_functions`` and ``control_flow_functions``.  This
builder keeps that shape, but adds source provenance to every record.  The
relationships are not hand-entered: PostgreSQL's ``guc.c`` binds a user-facing
parameter to an internal C variable (for example ``shared_buffers`` to
``NBuffers``), and the builder records real uses of that variable in backend
translation units.

This is deliberately a source-reference library, not a claim that every
reference is sufficient to prove a performance direction.  Direction and
numeric values still have to be validated by the measured workload/API loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get(
        "POSTGRES_SOURCE_ROOT",
        str(HERE.parent.parent / "third_party/postgresql-12.22"),
    )
)
DEFAULT_TARGET_KNOBS = (
    Path(
        os.environ.get(
            "SYSINSIGHT_REPOSITORY_ROOT",
            str(HERE.parent.parent / "repositories/Avalephh-KeenInsight/branch-sources"),
        )
    )
    / "WorkloadTune_new"
    / "sysinsight/library/knowledge_collection/postgres/target_knobs.txt"
)
DEFAULT_LEGACY_ASSOCIATION = (
    Path(
        os.environ.get(
            "SYSINSIGHT_REPOSITORY_ROOT",
            str(HERE.parent.parent / "repositories/Avalephh-KeenInsight/branch-sources"),
        )
    )
    / "dev/one/database/paramater_association_library.json"
)


def provenance_path(path: Path, logical_path: str) -> str:
    """Keep generated provenance independent of the generating workstation."""

    resolved = path.resolve()
    project_root = HERE.parent.parent.resolve()
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return logical_path


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_tree_digest(root: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_bytes(path.read_bytes()).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _mask_c_comments_and_literals(text: str) -> str:
    """Blank comments and C literals while preserving every character offset."""

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
                # Keep the offset stable for an escaped character too.
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


def _function_spans(
    masked_text: str, non_function_names: Optional[Iterable[str]] = None
) -> List[Tuple[int, int, str]]:
    """Return source line spans using the same declaration style as SysInsight."""

    non_function_names = set(non_function_names or [])
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
            if name not in {"if", "for", "while", "switch", "catch"} and name not in non_function_names:
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


def _source_macro_names(source_root: Path) -> set[str]:
    """Return function-like macros so calls such as ``forboth(...)`` are not functions."""

    names: set[str] = set()
    source_dir = source_root / "src"
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix not in {".c", ".h"}:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        masked = _mask_c_comments_and_literals(raw)
        names.update(
            re.findall(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)\s*\(", masked)
        )
    return names


def _legacy_records(path: Path) -> Dict[str, Dict[str, Any]]:
    """Read the acquired repository records for the five original PG GUCs.

    The file is a mixed MySQL/PostgreSQL library.  These records are retained
    as an explicitly labelled input; they are not treated as newly discovered
    source evidence by this builder.
    """

    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        return {}
    names = {
        "work_mem",
        "shared_buffers",
        "effective_cache_size",
        "maintenance_work_mem",
        "max_wal_size",
    }
    return {
        str(item["knob_name"]): item
        for item in value
        if isinstance(item, dict) and item.get("knob_name") in names
    }


def _source_function_locations(source_root: Path, names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Locate legacy function definitions/calls in the pinned C source tree."""

    wanted = set(names)
    locations: Dict[str, Dict[str, Any]] = {}
    for path in sorted((source_root / "src/backend").rglob("*.c")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        masked = _mask_c_comments_and_literals(raw)
        digest = _sha256_bytes(path.read_bytes())
        lines = raw.splitlines()
        for name in sorted(wanted - set(locations)):
            match = re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"\s*\(", masked)
            if not match:
                continue
            line = masked.count("\n", 0, match.start()) + 1
            locations[name] = {
                "function": name,
                "file": path.relative_to(source_root).as_posix(),
                "line": line,
                "source_line": lines[line - 1].strip() if line <= len(lines) else "",
                "source_sha256": digest,
            }
        if len(locations) == len(wanted):
            break
    return locations


def _guc_bindings(source_root: Path, target_knobs: List[str]) -> Dict[str, Dict[str, Any]]:
    """Read parameter -> internal variable bindings from PostgreSQL's guc.c."""

    guc_path = source_root / "src/backend/utils/misc/guc.c"
    raw = guc_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    bindings: Dict[str, Dict[str, Any]] = {}
    for index, line in enumerate(lines):
        name_match = re.search(r'\{"([^"\\]+)"\s*,', line)
        if not name_match or name_match.group(1) not in target_knobs:
            continue
        name = name_match.group(1)
        window = "\n".join(lines[index : index + 8])
        if not re.search(r"\bPGC_[A-Z_]+\b", window):
            continue
        variable: Optional[str] = None
        variable_line: Optional[int] = None
        for offset in range(1, 160):
            position = index + offset
            if position >= len(lines):
                break
            pointer_match = re.match(r"\s*&([A-Za-z_]\w*)\s*,", lines[position])
            if pointer_match:
                variable = pointer_match.group(1)
                variable_line = position + 1
                break
        bindings[name] = {
            "parameter": name,
            "variable": variable,
            "guc_file": guc_path.relative_to(source_root).as_posix(),
            "guc_line": index + 1,
            "guc_source_line": line.strip(),
            "variable_line": variable_line,
        }
    return bindings


def _scan_direct_source_refs(
    source_root: Path,
    bindings: Dict[str, Dict[str, Any]],
    c_files: Optional[List[Path]] = None,
) -> Tuple[Dict[str, Dict[str, List[Dict[str, Any]]]], Dict[str, str]]:
    """Scan backend C files for uses of variables bound by ``guc.c``."""

    if c_files is None:
        c_files = sorted((source_root / "src/backend").rglob("*.c"))
    function_refs: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    variable_to_knobs: Dict[str, List[str]] = defaultdict(list)
    for knob, binding in bindings.items():
        variable = binding.get("variable")
        if variable:
            variable_to_knobs[str(variable)].append(knob)

    variables = sorted(variable_to_knobs, key=len, reverse=True)
    variable_pattern = (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:"
            + "|".join(re.escape(item) for item in variables)
            + r")(?![A-Za-z0-9_])"
        )
        if variables
        else None
    )
    non_function_names = _source_macro_names(source_root)
    file_digests: Dict[str, str] = {}
    for path in c_files:
        relative = path.relative_to(source_root).as_posix()
        file_digests[relative] = _sha256_bytes(path.read_bytes())
        if relative == "src/backend/utils/misc/guc.c" or variable_pattern is None:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        masked = _mask_c_comments_and_literals(raw)
        spans = _function_spans(masked, non_function_names)
        raw_lines = raw.splitlines()
        for match in variable_pattern.finditer(masked):
            variable = match.group(0)
            line_number = masked.count("\n", 0, match.start()) + 1
            function = next(
                (name for start, end, name in spans if start <= line_number <= end),
                None,
            )
            if not function:
                continue
            context_kind = "control_flow" if _is_control_context(masked, match.start()) else "data_flow"
            source_line = raw_lines[line_number - 1].strip() if line_number <= len(raw_lines) else ""
            occurrence = {
                "function": function,
                "variable": variable,
                "file": relative,
                "line": line_number,
                "source_line": source_line,
                "context": context_kind,
                "source_sha256": file_digests[relative],
            }
            for knob in variable_to_knobs[variable]:
                function_refs[knob][function].append(occurrence)
    return function_refs, file_digests


def source_backed_knobs(source_root: Path, candidate_knobs: Iterable[str]) -> List[str]:
    """Return candidate GUCs with a real direct use in backend C source."""

    source_root = source_root.resolve()
    candidates = list(dict.fromkeys(str(item) for item in candidate_knobs))
    c_files = sorted((source_root / "src/backend").rglob("*.c"))
    bindings = _guc_bindings(source_root, candidates)
    function_refs, _ = _scan_direct_source_refs(source_root, bindings, c_files)
    return sorted(knob for knob in candidates if function_refs.get(knob))


def _is_control_context(masked_text: str, position: int) -> bool:
    """Classify a direct variable use occurring in a branch condition."""

    line_start = masked_text.rfind("\n", 0, position) + 1
    # PostgreSQL has multi-line branch conditions.  Restrict the look-back to
    # one statement-sized window so a previous unrelated branch is not used.
    lookback_start = max(0, position - 800)
    statement = masked_text[lookback_start:position]
    boundary = max(statement.rfind(";"), statement.rfind("{"), statement.rfind("}"))
    statement = statement[boundary + 1 :]
    if re.search(r"\b(?:if|while|for|switch)\s*\([^;{}]*$", statement):
        return True
    line = masked_text[line_start : masked_text.find("\n", position)]
    return "?" in line


def build_association_library(
    source_root: Path = DEFAULT_SOURCE_ROOT,
    target_knobs_path: Path = DEFAULT_TARGET_KNOBS,
    output_path: Path = HERE / "postgresql/common/function_parameter_association.json",
    manifest_path: Path = HERE / "postgresql/common/function_parameter_association.provenance.json",
    legacy_association_path: Path = DEFAULT_LEGACY_ASSOCIATION,
) -> Dict[str, Any]:
    """Build and write the matcher-compatible list and its provenance manifest."""

    source_root = source_root.resolve()
    target_knobs = [
        item.strip()
        for item in target_knobs_path.read_text(encoding="utf-8").splitlines()
        if item.strip() and not item.lstrip().startswith("#")
    ]
    c_files = sorted((source_root / "src/backend").rglob("*.c"))
    if not c_files:
        raise FileNotFoundError("no PostgreSQL backend C files below {}".format(source_root))

    bindings = _guc_bindings(source_root, target_knobs)
    legacy = _legacy_records(legacy_association_path.resolve())
    source_revision_path = source_root / ".gitrevision"
    source_revision = source_revision_path.read_text(encoding="utf-8").strip() if source_revision_path.exists() else None
    source_digest = _source_tree_digest(source_root, sorted((source_root / "src").rglob("*.c")))

    function_refs, file_digests = _scan_direct_source_refs(source_root, bindings, c_files)

    legacy_function_names = {
        function
        for item in legacy.values()
        for function in (item.get("data_flow_functions", []) or [])
        + (item.get("control_flow_functions", []) or [])
    }
    legacy_locations = _source_function_locations(source_root, legacy_function_names)

    records: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []
    for knob in target_knobs:
        binding = bindings.get(knob, {
            "parameter": knob,
            "variable": None,
            "guc_file": "src/backend/utils/misc/guc.c",
            "guc_line": None,
            "guc_source_line": None,
            "variable_line": None,
        })
        refs = function_refs.get(knob, {})
        direct_data_functions = sorted(
            function
            for function, occurrences in refs.items()
            if any(item["context"] == "data_flow" for item in occurrences)
        )
        direct_control_functions = sorted(
            function
            for function, occurrences in refs.items()
            if any(item["context"] == "control_flow" for item in occurrences)
        )
        old_record = legacy.get(knob, {})
        inherited_data_functions = list(old_record.get("data_flow_functions", []) or [])
        inherited_control_functions = list(old_record.get("control_flow_functions", []) or [])
        data_functions = sorted(set(direct_data_functions) | set(inherited_data_functions))
        control_functions = sorted(set(direct_control_functions) | set(inherited_control_functions))
        inherited_names = set(inherited_data_functions) | set(inherited_control_functions)
        inherited_evidence = [
            {
                **legacy_locations[function],
                "evidence_type": "legacy_association_function_source_validation",
                "association_source": provenance_path(
                    legacy_association_path,
                    "repositories/Avalephh-KeenInsight/branch-sources/dev/one/database/paramater_association_library.json",
                ),
                "association_relation": (
                    "data_flow" if function in inherited_data_functions else "control_flow"
                ),
                "note": (
                    "The function was present in the acquired five-record PG association "
                    "asset and was found in the pinned source tree; this row does not claim "
                    "a newly inferred GUC data/control use."
                ),
            }
            for function in sorted(inherited_names)
            if function in legacy_locations
        ]
        record: Dict[str, Any] = {
            "knob_name": knob,
            "data_flow_functions": data_functions,
            "control_flow_functions": control_functions,
            "data_flow_functions_num": len(data_functions),
            "control_flow_functions_num": len(control_functions),
            "total_functions_num": len(set(data_functions) | set(control_functions)),
            "association_method": "legacy_asset_plus_guc_binding_to_source_identifier_use",
            "guc_variable": binding.get("variable"),
            "legacy_association": {
                "source": provenance_path(
                    legacy_association_path,
                    "repositories/Avalephh-KeenInsight/branch-sources/dev/one/database/paramater_association_library.json",
                ),
                "present": bool(old_record),
                "data_flow_functions": inherited_data_functions,
                "control_flow_functions": inherited_control_functions,
                "function_source_validation_count": len(inherited_evidence),
            },
            "source_provenance": {
                "source_root": provenance_path(source_root, "third_party/postgresql-12.22"),
                "source_revision": source_revision,
                "source_digest": source_digest,
                "guc_binding": binding,
                "scan_scope": "src/backend/**/*.c excluding src/backend/utils/misc/guc.c",
                "comments_and_literals": "masked before identifier matching",
            },
            "source_evidence": inherited_evidence + [
                occurrence
                for function in sorted(refs)
                for occurrence in refs[function]
            ],
        }
        if not refs:
            record["unmatched_reason"] = (
                "No backend function contains a source identifier bound to this GUC; "
                "the record is retained without inferred function names."
            )
        records.append(record)
        summary.append({
            "knob_name": knob,
            "guc_variable": binding.get("variable"),
            "data_flow_functions": len(data_functions),
            "control_flow_functions": len(control_functions),
            "source_evidence_rows": len(record["source_evidence"]),
            "direct_source_use": bool(refs),
            "inherited_legacy_functions": len(inherited_evidence),
            "has_source_use": bool(refs),
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "format": "sysinsight-postgresql-source-association-provenance-v1",
        "source_root": provenance_path(source_root, "third_party/postgresql-12.22"),
        "source_revision": source_revision,
        "source_digest": source_digest,
        "guc_file": provenance_path(
            source_root / "src/backend/utils/misc/guc.c",
            "third_party/postgresql-12.22/src/backend/utils/misc/guc.c",
        ),
        "target_knobs_file": provenance_path(
            target_knobs_path,
            "perf-anomaly-demo/db_profiles/postgresql/common/postgresql_source_target_knobs.txt",
        ),
        "legacy_association_source": provenance_path(
            legacy_association_path,
            "repositories/Avalephh-KeenInsight/branch-sources/dev/one/database/paramater_association_library.json",
        ),
        "legacy_pg_record_count": len(legacy),
        "backend_c_file_count": len(c_files),
        "record_count": len(records),
        "records_with_source_use": sum(1 for item in summary if item["has_source_use"]),
        "records": summary,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--target-knobs", default=str(DEFAULT_TARGET_KNOBS))
    parser.add_argument("--output", default=str(HERE / "postgresql/common/function_parameter_association.json"))
    parser.add_argument("--manifest", default=str(HERE / "postgresql/common/function_parameter_association.provenance.json"))
    args = parser.parse_args()
    manifest = build_association_library(
        Path(args.source_root), Path(args.target_knobs), Path(args.output), Path(args.manifest)
    )
    print(json.dumps({
        "output": args.output,
        "manifest": args.manifest,
        "record_count": manifest["record_count"],
        "records_with_source_use": manifest["records_with_source_use"],
        "source_revision": manifest["source_revision"],
        "source_digest": manifest["source_digest"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
