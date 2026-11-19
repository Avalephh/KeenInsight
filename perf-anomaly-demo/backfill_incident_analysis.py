#!/usr/bin/env python3
"""Generate function analysis post hoc from an incident's retained perf data.

Some interrupted incidents retain ``anomaly/anomaly_counts_*.txt`` and
``anomaly/anomaly.folded`` but no ``sysinsight_input.json`` because the
workflow stopped before the input/candidate stages.  This tool reruns the
original SysInsight comparison, source matching, runtime call-chain evidence
and SHAP explanation against those retained files.  It never invents a
candidate configuration or a before/after tuning comparison.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DEFAULT_SOURCE = Path("/root/new/repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune")
DEFAULT_POSTGRES_SOURCE = Path("/root/keeninsight-postgres/third_party/postgresql-12.22")
SCHEMA = "keeninsight.backfill.v1"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default=None):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return default


def jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def find_counts(incident_dir: Path) -> Path:
    candidates = sorted(
        path for path in (incident_dir / "anomaly").glob("anomaly_counts_*.txt")
        if not path.name.endswith("_btFunctions.txt")
    )
    if not candidates:
        raise FileNotFoundError("no anomaly_counts_*.txt in {}".format(incident_dir))
    return candidates[-1]


def find_folded(incident_dir: Path) -> Path:
    path = incident_dir / "anomaly" / "anomaly.folded"
    if not path.is_file():
        raise FileNotFoundError("no anomaly.folded in {}".format(incident_dir))
    return path


def normal_profile(configured: str) -> Path:
    if configured:
        path = Path(configured).resolve()
        if not path.is_file():
            raise FileNotFoundError("normal profile not found: {}".format(path))
        return path
    candidates = [
        path for path in (ROOT / "results").rglob("normal_profile_postgresql_demo.csv")
        if "/baseline/" in str(path)
    ]
    if not candidates:
        candidates = list((ROOT / "results").rglob("normal_profile_postgresql_demo.csv"))
    if not candidates:
        raise FileNotFoundError("no normal_profile_postgresql_demo.csv is available")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def infer_phase(incident_dir: Path) -> str:
    snapshot = load_json(incident_dir / "backend_snapshot.json", {}) or {}
    names = [
        str(row.get("application_name") or "")
        for row in snapshot.get("rows") or []
        if isinstance(row, dict)
    ]
    for phase in ("pressure_before", "pressure_after", "baseline"):
        if any(phase in name for name in names):
            return phase
    return "unknown"


def import_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load {}".format(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incident_dir", type=Path)
    parser.add_argument("--normal-profile", default="")
    parser.add_argument("--dbms", default=os.environ.get("SYSINSIGHT_DBMS", "postgresql"))
    parser.add_argument("--db-version", default=os.environ.get("SYSINSIGHT_DB_VERSION", "14"))
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE))
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    incident_dir = args.incident_dir.resolve()
    counts = find_counts(incident_dir)
    folded = find_folded(incident_dir)
    normal = normal_profile(args.normal_profile)
    source_root = Path(args.source_root).resolve()
    analyzer = source_root / "DBTuner" / "utils" / "analyzeException.py"
    matcher_path = source_root / "DBTuner" / "utils" / "matchFunctions.py"
    if not analyzer.is_file() or not matcher_path.is_file():
        raise FileNotFoundError("original SysInsight source not found under {}".format(source_root))
    if not os.environ.get("POSTGRES_SOURCE_ROOT") and (DEFAULT_POSTGRES_SOURCE / "src" / "backend").is_dir():
        os.environ["POSTGRES_SOURCE_ROOT"] = str(DEFAULT_POSTGRES_SOURCE)

    from db_profile import profile_summary, resolve_profile
    from sysinsight_detection import run_original_compare
    from sysinsight_shap import compact_evidence, explain_subprocess
    from sysinsight_source_evidence import build_source_evidence

    profile = resolve_profile(args.dbms, args.db_version)
    static_library = profile.path("association")
    if static_library is None or not Path(static_library).is_file():
        raise FileNotFoundError("no parameter association library for {}".format(args.dbms))

    key_path, key_functions = run_original_compare(analyzer, counts, normal)
    matcher = import_module(matcher_path, "sysinsight_backfill_match_functions")
    bk_functions, matched_knob, csv_func_to_knob = matcher.find_top_and_matched_functions(
        str(key_path), str(static_library)
    )

    detection = {
        "generated_at": now(),
        "scope": {
            "reason": "post-hoc function analysis from retained perf counts and folded stacks",
            "custom_filtering": False,
            "custom_threshold": False,
            "custom_top_k": False,
        },
        "source_compare": {
            "source": str(analyzer),
            "input_counts": str(counts),
            "normal_profile": str(normal),
            "key_function_file": str(key_path),
            "key_function_count": len(key_functions),
            "key_functions": jsonable(key_functions),
        },
        "source_match": {
            "source": str(matcher_path),
            "static_library": str(static_library),
            "bkFunctions_list": jsonable(bk_functions),
            "matched_knob": jsonable(matched_knob),
            "csv_func_to_knob": jsonable(csv_func_to_knob),
        },
        "profile": profile_summary(profile),
    }
    case = {
        "sysinsight_source_detection": detection,
        "anomaly": {"perf_postprocess": {"counts_path": str(counts), "folded_path": str(folded)}},
    }
    source = build_source_evidence(case, incident_dir / "case_result.json", profile)
    phase = infer_phase(incident_dir)
    full_shap = explain_subprocess(counts, context={"workload": "tpcc", "phase": phase})
    shap = compact_evidence(full_shap)

    payload = {
        "schema_version": SCHEMA,
        "generated_at": now(),
        "generated_by": "backfill_incident_analysis.py",
        "provenance": {
            "source": "retained perf counts and folded stacks",
            "counts_path": str(counts),
            "folded_path": str(folded),
            "normal_profile": str(normal),
            "phase": phase,
            "note": "Post-hoc function analysis only; no LLM candidate or tuning comparison is fabricated.",
        },
        "source_detection": detection,
        "source_evidence": source,
        "shap_analysis": shap,
    }
    output = Path(args.output) if args.output else incident_dir / "backfill_function_analysis.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", output)
    print("functions", len(key_functions), "matched_knobs", len(matched_knob),
          "runtime_chains", len(source.get("runtime_call_chains") or []),
          "source_examples", len(source.get("static_parameter_evidence") or []),
          "shap", shap.get("status"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
