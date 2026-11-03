"""Feed real eBPF stacks through the ORIGINAL function and differential code."""
import ast
import csv
import importlib.util
import os
from pathlib import Path
import statistics

from .collector import atomic_json


def original_root():
    path = Path(os.environ["SYSINSIGHT_SOURCE_ROOT"])
    if not (path / "DBTuner/utils/analyzeException.py").is_file():
        raise ValueError("original SysInsight source is required, no replacement comparator")
    return path


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def function_counts(folded):
    path = original_root() / "DBTuner/dbenv.py"
    tree = ast.parse(path.read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "get_perf_function_range")
    # Execute the original method only, without importing database services.
    namespace = {"os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    result = namespace["get_perf_function_range"](None, None, 2, str(folded))
    if not result:
        raise RuntimeError("original sampled-function conversion failed")
    return Path(result)


def rates(counts):
    with Path(counts).open() as handle:
        return {row["Function"]: float(row["Sampling Rate (%)"].rstrip("%"))
                for row in csv.DictReader(handle, delimiter="\t")}


def normal_profile(count_files, output):
    measurements = [rates(path) for path in count_files]
    if not measurements:
        raise ValueError("at least one genuine normal eBPF profile is required")
    names = set().union(*(set(value) for value in measurements))
    with Path(output).open("w") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Function", "Min Sampling Rate (%)", "Max Sampling Rate (%)",
                         "Average Sampling Rate (%)"])
        for name in sorted(names):
            values = [row.get(name, 0) for row in measurements]
            writer.writerow([name, min(values), max(values), statistics.mean(values)])


def compare(anomaly_counts, normal, directory, profile, normal_windows=None):
    root = original_root()
    comparator = load(root / "DBTuner/utils/analyzeException.py", "ebpf_original_difference")
    matcher = load(root / "DBTuner/utils/matchFunctions.py", "ebpf_original_match")
    key_path, functions = comparator.compare_file_sample_rate(str(anomaly_counts), str(normal))
    names, knobs, mapping = matcher.find_top_and_matched_functions(
        str(key_path), str(profile.path("association")))
    # Preserve original comparator semantics: anomaly-only functions are NOT
    # silently given a fabricated zero baseline. Audit this coverage separately.
    with Path(normal).open() as handle:
        normal_names = {row["Function"] for row in csv.DictReader(handle)}
    omitted = sorted(set(rates(anomaly_counts)) - normal_names)
    result = {"status": "completed",
              "collection": {"type": "ebpf", "semantics": "CPU stack sample rates"},
              "source_compare": {"source": str(root / "DBTuner/utils/analyzeException.py"),
                                 "normal_profile": str(normal), "input_counts": str(anomaly_counts),
                                 "key_function_file": str(key_path), "key_function_count": len(functions),
                                 "key_functions": functions, "normal_capture_count": normal_windows,
                                 "anomaly_only_functions_ignored_by_original": omitted},
              "source_match": {"source": str(root / "DBTuner/utils/matchFunctions.py"),
                               "bkFunctions_list": names, "matched_knob": knobs,
                               "csv_func_to_knob": mapping},
              "algorithm": "unmodified original compare_file_sample_rate + find_top_and_matched_functions",
              "not_claimed": "stack sample rates prove parameter causality or equal tuning gain"}
    atomic_json(Path(directory) / "source_detection.json", result)
    return result
