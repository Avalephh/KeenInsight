#!/usr/bin/env python3
"""Run the original SysInsight file-based exception extraction on one run.

The only purpose of this wrapper is to pass the generated files to the
original SysInsight functions and record their return values.  It does not
add noise filtering, thresholds, Top-K selection, or tuning actions.
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
from db_profile import available_profiles, profile_summary, resolve_profile  # type: ignore
SOURCE_ROOT = Path(
    os.environ.get(
        "SYSINSIGHT_SOURCE_ROOT",
        str(ROOT.parent / "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune"),
    )
)
DEFAULT_SOURCE_ANALYZER = SOURCE_ROOT / "DBTuner/utils/analyzeException.py"
DEFAULT_MATCHER = SOURCE_ROOT / "DBTuner/utils/matchFunctions.py"
DEFAULT_STATIC_LIBRARY = SOURCE_ROOT / "DBTuner/utils/paramater_association_library.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default="", help="One results/<timestamp> directory")
    parser.add_argument("--dbms", default=os.environ.get("SYSINSIGHT_DBMS", "postgresql"))
    parser.add_argument("--db-version", default=os.environ.get("SYSINSIGHT_DB_VERSION", "12"))
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--source-analyzer", default=str(DEFAULT_SOURCE_ANALYZER))
    parser.add_argument("--matcher", default=str(DEFAULT_MATCHER))
    parser.add_argument("--static-library", default="")
    parser.add_argument("--normal-profile", default="")
    return parser.parse_args()


def latest_run() -> Path:
    candidates = [path for path in (ROOT / "results").glob("20*") if path.is_dir()]
    if not candidates:
        raise FileNotFoundError("no results/<timestamp> directory found")
    return sorted(candidates)[-1]


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def import_module(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_anomaly_counts(run_dir: Path) -> Path:
    source_outputs = sorted(
        path
        for path in run_dir.glob("anomaly_counts_*.txt")
        if not path.name.endswith("_btFunctions.txt")
    )
    if source_outputs:
        return source_outputs[-1]
    # Compatibility with an earlier test artifact; new runs use the original
    # get_perf_function_range output name above.
    compatibility_path = run_dir / "anomaly_counts.tsv"
    if compatibility_path.exists():
        return compatibility_path
    raise FileNotFoundError("no anomaly function-range file found")


def run_original_compare(
    source_path: Path, counts_path: Path, normal_profile_path: Path
) -> tuple[Path, list[dict[str, Any]]]:
    module = import_module(source_path, "sysinsight_original_analyze_exception")
    output_path, functions = module.compare_file_sample_rate(
        str(counts_path), str(normal_profile_path)
    )
    return Path(output_path), functions


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


def main() -> int:
    args = parse_args()
    if args.list_profiles:
        print("\n".join(available_profiles()))
        return 0
    profile = resolve_profile(args.dbms, args.db_version)
    run_dir = Path(args.run_dir).resolve() if args.run_dir else latest_run()
    if not run_dir.is_dir():
        print(f"run directory not found: {run_dir}", file=sys.stderr)
        return 2

    try:
        counts_path = find_anomaly_counts(run_dir)
        normal_profile_path = Path(args.normal_profile).resolve() if args.normal_profile else run_dir / "normal_profile_postgresql_demo.csv"
        if not normal_profile_path.exists():
            raise FileNotFoundError(f"normal profile not found: {normal_profile_path}")

        key_path, key_functions = run_original_compare(
            Path(args.source_analyzer), counts_path, normal_profile_path
        )

        matcher = import_module(Path(args.matcher), "sysinsight_original_match_functions")
        static_library = Path(args.static_library).resolve() if args.static_library else profile.path("association")
        if static_library is None:
            raise FileNotFoundError("profile has no association library")
        bk_functions, matched_knob, csv_func_to_knob = matcher.find_top_and_matched_functions(
            str(key_path), str(static_library)
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError, ImportError) as exc:
        print(f"原始 SysInsight 流程失败：{exc}", file=sys.stderr)
        return 2

    run_summary_path = run_dir / "summary.json"
    run_summary: dict[str, Any] = {}
    if run_summary_path.exists():
        try:
            run_summary = json.loads(run_summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            run_summary = {}

    result = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "scope": {
            "prometheus_alert_to_perf": "custom trigger extension in run_demo.py",
            "function_range": "original SysInsight DBEnv.get_perf_function_range",
            "exception_compare": "original SysInsight analyzeException.compare_file_sample_rate",
            "function_matching": "original SysInsight matchFunctions.find_top_and_matched_functions",
            "custom_filtering": False,
            "custom_threshold": False,
            "custom_top_k": False,
        },
        "source_compare": {
            "source": str(Path(args.source_analyzer)),
            "input_counts": relative(counts_path),
            "normal_profile": relative(normal_profile_path),
            "key_function_file": relative(key_path),
            "key_function_count": len(key_functions),
            "key_functions": jsonable(key_functions),
        },
        "source_match": {
            "source": str(Path(args.matcher)),
            "static_library": str(static_library),
            "bkFunctions_list": jsonable(bk_functions),
            "matched_knob": jsonable(matched_knob),
            "csv_func_to_knob": jsonable(csv_func_to_knob),
        },
        "profile": profile_summary(profile),
        "trigger": run_summary.get("anomaly", {}).get("trigger"),
        "warning": (
            "PostgreSQL uses the source-derived association snapshot recorded in "
            "function_parameter_association.provenance.json: 276 documented GUC records, "
            "including 231 numeric/enum/bool search dimensions; the five acquired PG records "
            "are retained as an explicitly labelled legacy subset."
            if profile.dbms == "postgresql"
            else "Using the original MySQL association library and profile files."
        ),
    }
    output_path = run_dir / "sysinsight_source_detection_result.json"
    output_path.write_text(
        json.dumps(jsonable(result), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"检测目录：{run_dir}")
    print(f"原始 SysInsight 异常函数：{len(key_functions)}")
    print(f"原始 SysInsight 匹配参数：{len(matched_knob)}")
    print(f"结果：{output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
