#!/usr/bin/env python3
"""Build portable PostgreSQL profile artifacts from acquired repository data.

The generated files are mechanical projections of local repository assets:
the PG association records are built from the pinned PostgreSQL source tree
while retaining the acquired five-record PG subset, manual entries follow the
original getManualKnow.extract_knob_info shape, and numeric
constraints/defaults come from the acquired PostgreSQL official-document JSON.
The association target list is derived from the same document plus direct
uses in the pinned PostgreSQL backend source.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from build_pg_source_association import build_association_library, source_backed_knobs


HERE = Path(__file__).resolve().parent
DEFAULT_REPO_ROOT = Path(
    os.environ.get(
        "SYSINSIGHT_REPOSITORY_ROOT",
        str(HERE.parent.parent / "repositories/Avalephh-KeenInsight/branch-sources"),
    )
)
PG_SOURCE_ROOT = Path(
    os.environ.get(
        "POSTGRES_SOURCE_ROOT",
        str(HERE.parent.parent / "third_party/postgresql-12.22"),
    )
)


def provenance_path(path: Path, logical_path: str) -> str:
    """Keep generated provenance independent of the generating workstation."""

    resolved = path.resolve()
    project_root = HERE.parent.parent.resolve()
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return logical_path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_number(value: Any, integer: bool = False) -> Optional[Any]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if integer else value
    match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(value))
    if not match:
        return None
    number = float(match.group(0))
    if integer or number.is_integer():
        return int(number)
    return number


def to_constraint(doc: Dict[str, Any]) -> List[Any]:
    doc_type = str(doc.get("type", "")).lower()
    if doc_type in {"integer", "real", "double"}:
        value_type = "int" if doc_type == "integer" else "float"
        lower = parse_number(doc.get("min"), integer=value_type == "int")
        upper = parse_number(doc.get("max"), integer=value_type == "int")
        if lower is None:
            lower = parse_number(doc.get("default"), integer=value_type == "int")
        if upper is None:
            upper = lower
        if lower is None or upper is None:
            raise ValueError("no numeric range for {}".format(doc.get("name")))
        return [value_type, "linear", [lower, upper]]
    if doc_type == "enum":
        return ["enum", "linear", doc.get("values", [])]
    if doc_type == "bool":
        return ["enum", "linear", ["on", "off"]]
    raise ValueError("unsupported PG doc type {} for {}".format(doc_type, doc.get("name")))


def manual_entries(pg_root: Path) -> List[Dict[str, Any]]:
    normal_dir = pg_root / "structured_knowledge" / "normal"
    special_dir = pg_root / "structured_knowledge" / "special"
    entries: List[Dict[str, Any]] = []
    for path in sorted(normal_dir.glob("*.json")):
        data = read_json(path)
        special = read_json(special_dir / path.name) if (special_dir / path.name).exists() else {}
        entries.append(
            {
                "parameter": path.stem,
                "min_value": data.get("min_value", ""),
                "max_value": data.get("max_value", ""),
                "suggested_values": data.get("suggested_values", []),
                "special_value": special.get("special_value") if special.get("special_knob", False) else None,
            }
        )
    return entries


def target_knob_entries(constraints: Dict[str, Any], defaults: Dict[str, Any], docs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for name, constraint in constraints.items():
        value_type = constraint[0]
        values = constraint[2]
        if value_type in {"int", "float"}:
            minimum, maximum = values
        else:
            minimum, maximum = None, None
        doc = docs.get(name, {})
        result[name] = {
            "default": defaults.get(name),
            "dynamic": "Yes" if doc.get("context") in {"user", "sighup", "superuser"} else "No",
            "max": maximum,
            "min": minimum,
            "scope": doc.get("context", "unknown"),
            "type": "integer" if value_type == "int" else "real" if value_type == "float" else value_type,
            "start_value": defaults.get(name),
            "unit": doc.get("unit"),
        }
    return result


def read_target_knobs(path: Path) -> List[str]:
    return [
        item.strip()
        for item in path.read_text(encoding="utf-8").splitlines()
        if item.strip() and not item.lstrip().startswith("#")
    ]


def capture_live_pg_settings(names: Iterable[str], docs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Read the local instance once; return an empty dict when unavailable."""

    query = "SELECT name, setting FROM pg_settings ORDER BY name"
    commands = [
        ["sudo", "-u", "postgres", "psql", "-d", "keeninsight", "-At", "-F", "\t", "-c", query],
        ["psql", "-d", "keeninsight", "-At", "-F", "\t", "-c", query],
    ]
    output = None
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            output = result.stdout
            break
    if output is None:
        return {}

    wanted = set(names)
    values: Dict[str, Any] = {}
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or parts[0] not in wanted:
            continue
        name, raw = parts
        doc_type = str(docs.get(name, {}).get("type", "")).lower()
        if doc_type == "bool":
            values[name] = raw
        elif doc_type == "enum":
            values[name] = raw
        elif doc_type in {"integer", "real", "double"}:
            parsed = parse_number(raw, integer=doc_type == "integer")
            if parsed is not None:
                values[name] = parsed
    return values


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(repo_root: Path) -> None:
    knowledge_root = repo_root / "WorkloadTune_new" / "sysinsight" / "library" / "knowledge_collection" / "postgres"
    official = read_json(knowledge_root / "knob_info" / "official_document.json")
    docs_list = official["params"]
    docs = {item["name"]: item for item in docs_list}
    original_target_knob_path = knowledge_root / "target_knobs.txt"
    original_pg_knobs = read_target_knobs(original_target_knob_path)

    # The repository's PG target_knobs.txt is a small hand-curated list.  For
    # the PostgreSQL profile, derive the association set from every parameter
    # in the acquired official document and retain only GUCs that are
    # actually bound and used in the pinned PostgreSQL backend source.  The
    # numeric/enum/bool subset below becomes the automatic search space;
    # string GUCs remain available to the source association matcher but are
    # not fabricated into numeric constraints.  This is the expansion of the
    # five-record PG association asset.
    official_document_knobs = [item["name"] for item in docs_list]
    official_tunable_knobs = [
        item["name"]
        for item in docs_list
        if str(item.get("type", "")).lower() in {"integer", "real", "double", "enum", "bool"}
    ]
    source_backed = source_backed_knobs(PG_SOURCE_ROOT, official_document_knobs)
    missing_original = sorted(set(original_pg_knobs) - set(source_backed))
    if missing_original:
        raise RuntimeError(
            "the source-derived expansion dropped repository target knobs: {}".format(
                ", ".join(missing_original)
            )
        )
    pg_knobs = [
        *[name for name in original_pg_knobs if name in source_backed],
        *[name for name in source_backed if name not in set(original_pg_knobs)],
    ]

    common = HERE / "postgresql" / "common"
    common.mkdir(parents=True, exist_ok=True)
    target_knob_path = common / "postgresql_source_target_knobs.txt"
    target_knob_path.write_text(
        "# Derived from the official PostgreSQL parameter document and direct uses in pinned PostgreSQL source.\n"
        + "\n".join(pg_knobs)
        + "\n",
        encoding="utf-8",
    )
    source_target_provenance = {
        "format": "sysinsight-postgresql-source-target-knobs-v1",
        "official_document": provenance_path(
            knowledge_root / "knob_info" / "official_document.json",
            "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune_new/sysinsight/library/knowledge_collection/postgres/knob_info/official_document.json",
        ),
        "official_document_version": official.get("version"),
        "official_document_parameter_count": len(official_document_knobs),
        "official_tunable_candidate_count": len(official_tunable_knobs),
        "original_target_knob_file": provenance_path(
            original_target_knob_path,
            "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune_new/sysinsight/library/knowledge_collection/postgres/target_knobs.txt",
        ),
        "original_target_knob_count": len(original_pg_knobs),
        "source_root": provenance_path(PG_SOURCE_ROOT, "third_party/postgresql-12.22"),
        "source_revision": (
            (PG_SOURCE_ROOT / ".gitrevision").read_text(encoding="utf-8").strip()
            if (PG_SOURCE_ROOT / ".gitrevision").exists()
            else None
        ),
        "source_backed_target_knob_count": len(pg_knobs),
        "source_backed_target_knobs": pg_knobs,
        "source_backed_tunable_knob_count": len(set(official_tunable_knobs) & set(pg_knobs)),
        "excluded_official_document_knobs": sorted(set(official_document_knobs) - set(pg_knobs)),
        "excluded_official_tunable_knobs": sorted(set(official_tunable_knobs) - set(pg_knobs)),
        "selection_rule": "official document parameter plus direct variable use in src/backend/**/*.c; automatic dimensions additionally require type integer/real/double/enum/bool",
    }
    write_json(common / "postgresql_source_target_knobs.provenance.json", source_target_provenance)
    selected_docs = {name: docs[name] for name in pg_knobs if name in docs}
    # The original LLAMBO constraint format supports numerical, enum and bool
    # dimensions.  String GUCs remain in the source association library and
    # metadata, but are not offered as automatic numeric search dimensions.
    constraint_docs = {
        name: doc
        for name, doc in selected_docs.items()
        if str(doc.get("type", "")).lower() in {"integer", "real", "double", "enum", "bool"}
    }
    constraints = {name: to_constraint(constraint_docs[name]) for name in constraint_docs}
    defaults = {
        name: (
            selected_docs[name].get("default")
            if constraints[name][0] == "enum"
            else parse_number(selected_docs[name].get("default"), integer=constraints[name][0] == "int")
        )
        for name in constraints
    }

    association_manifest = build_association_library(
        PG_SOURCE_ROOT,
        target_knob_path,
        common / "function_parameter_association.json",
        common / "function_parameter_association.provenance.json",
    )
    association = read_json(common / "function_parameter_association.json")
    if {item.get("knob_name") for item in association} != set(pg_knobs):
        raise RuntimeError("source-derived PG association library does not cover derived PG source targets")
    write_json(common / "constraints.json", constraints)
    write_json(common / "defaults.json", defaults)
    write_json(common / "knob_info_manual.json", manual_entries(knowledge_root))
    write_json(common / "parameter_metadata.json", selected_docs)
    write_json(common / "gptuner_target_knobs.json", target_knob_entries(constraints, defaults, selected_docs))
    (common / "rules.txt").write_text("", encoding="utf-8")
    (common / "code").mkdir(parents=True, exist_ok=True)
    (common / "code" / "README.txt").write_text(
        "No PostgreSQL code snippets were fabricated. Function/parameter associations are generated from the pinned PostgreSQL 12.22 source tree; see function_parameter_association.provenance.json.\n",
        encoding="utf-8",
    )
    (common / "prompt_template.txt").write_text(
        "As a database parameter tuning expert, you should provide optimization recommendations for the parameter {variable} in {dbms_name} based on the following information:\n\n"
        "1. Database Environment:\n"
        "    - Database kernel: {dbms_info}\n"
        "    - Hardware configuration: {hardware_info}\n"
        "    - Configuration value convention: {unit_note}\n\n"
        "2. Workload Characteristics:\n"
        "    {benchmark}\n\n"
        "3. Target Parameter: {variable}\n\n"
        "4. The bottleneck functions that are affected by parameters in perf:\n"
        "    {keyFunction_section}\n\n"
        "5. Relevant Dataflow and Control Dependencies:\n"
        "    {dataflow_section}\n\n"
        "6. The rules extracted from historical data are association rules about parameter changes, function ranges and performance changes:\n"
        "    {rule_section}\n\n"
        "7. In the previous round of parameter configuration, the system resource usage was as follows:\n"
        "    {resource_usage}\n\n"
        "8. The recommended values, recommended ranges and recommended mechanisms of the parameters in the manual are as follows:\n"
        "    {recommendation_section}\n\n"
        "9. Optimization Goals:\n"
        "    {optimization_goal}\n\n"
        "Please do not recommend values that appear repeatedly.\n",
        encoding="utf-8",
    )

    # PostgreSQL 12 uses a real pg_settings snapshot for the prompt's previous
    # configuration. Other version profiles retain the acquired document
    # defaults because those servers are not present on this host.
    live_values = capture_live_pg_settings(constraints.keys(), selected_docs)
    pg12_initial = dict(defaults)
    pg12_initial.update(live_values)
    pg13_initial = dict(defaults)
    pg14_initial = dict(defaults)
    for version, initial in {"12": pg12_initial, "13": pg13_initial, "14": pg14_initial}.items():
        profile_dir = HERE / "postgresql" / version
        profile_dir.mkdir(parents=True, exist_ok=True)
        write_json(profile_dir / "initial_config.json", initial)
        write_json(profile_dir / "defaults.json", defaults)

    # The builder is intentionally idempotent. Profile manifests are checked
    # into the demo separately so their database/version switch is visible.
    print("built PG artifacts under {}".format(common))
    print("association records: {}".format(len(association)))
    print("source records with source use: {}".format(association_manifest["records_with_source_use"]))
    print("candidate dimensions: {}".format(len(constraints)))
    print("official tunable candidates: {}".format(len(official_tunable_knobs)))
    print("official candidates excluded without direct source use: {}".format(len(source_target_provenance["excluded_official_tunable_knobs"])))
    print("live PG12 settings captured: {}".format(len(live_values)))
    print("manual records: {}".format(len(manual_entries(knowledge_root))))
    print("official-document version: {}".format(official.get("version")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT))
    args = parser.parse_args()
    build(Path(args.repo_root).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
