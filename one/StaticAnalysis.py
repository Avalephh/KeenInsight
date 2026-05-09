from __future__ import annotations

import json
from typing import Any, Dict, List, Set, Tuple


class StaticAnalysis:
    def __init__(self, static_lib_path: str):
        self.static_lib_path = static_lib_path
        self.static_data = self._load_json(static_lib_path)
        if not isinstance(self.static_data, list):
            raise ValueError("static_lib json root must be list")

    @staticmethod
    def _load_json(json_file: str) -> Any:
        with open(json_file, "r") as f:
            return json.load(f)

    @staticmethod
    def _normalize_function_name(func: Any) -> str:
        if func is None:
            return ""
        s = str(func).strip()
        if "(" in s:
            s = s.split("(", 1)[0].strip()
        return s

    def match_functions_2(
        self, function_names: List[str]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
        function_set: Set[str] = {
            self._normalize_function_name(x)
            for x in function_names
            if self._normalize_function_name(x)
        }

        func_to_knob: Dict[str, List[str]] = {func: [] for func in function_set}
        output_data_list: List[Dict[str, Any]] = []

        for json_data in self.static_data:
            if not isinstance(json_data, dict):
                continue

            knob_name = json_data.get("knob_name")
            data_flow_functions = json_data.get("data_flow_functions", [])
            control_flow_functions = json_data.get("control_flow_functions", [])

            if not isinstance(data_flow_functions, list):
                data_flow_functions = []
            if not isinstance(control_flow_functions, list):
                control_flow_functions = []

            data_flow_matched: List[str] = []
            control_flow_matched: List[str] = []

            for func in data_flow_functions:
                n = self._normalize_function_name(func)
                if n in function_set:
                    data_flow_matched.append(func)
                    func_to_knob.setdefault(n, []).append(knob_name)

            for func in control_flow_functions:
                n = self._normalize_function_name(func)
                if n in function_set:
                    control_flow_matched.append(func)
                    func_to_knob.setdefault(n, []).append(knob_name)

            total_functions_matched_num = len(data_flow_matched) + len(control_flow_matched)
            if total_functions_matched_num > 0:
                output_data_list.append(
                    {
                        "knob_name": knob_name,
                        "data_flow_functions": data_flow_matched,
                        "control_flow_functions": control_flow_matched,
                    }
                )

        return output_data_list, func_to_knob

    def analyze_functions(self, function_names: List[str]) -> Dict[str, Any]:
        matched_knobs, func_to_knob = self.match_functions_2(function_names)
        normalized = [self._normalize_function_name(x) for x in function_names if self._normalize_function_name(x)]
        unique_funcs = sorted(set(normalized))
        return {
            "functions": unique_funcs,
            "function_to_knobs": {k: v for k, v in func_to_knob.items() if v},
            "matched_knobs": matched_knobs,
        }
