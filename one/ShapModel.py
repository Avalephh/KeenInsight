from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple, Optional

import joblib
import numpy as np
import pandas as pd


def load_json(json_path: str) -> Any:
    with open(json_path, "r") as f:
        return json.load(f)


def extract_function_order(mapping_json_path: str) -> Tuple[List[str], Dict[str, str]]:
    data = load_json(mapping_json_path)

    function_order: List[str] = []
    reverse_mapping: Dict[str, str] = {}

    if isinstance(data, dict):
        if data and all(isinstance(k, str) for k in data.keys()) and all(isinstance(v, str) for v in data.values()):
            if all(re.fullmatch(r"fun\d+", str(v)) for v in data.values()):
                reverse_mapping = {str(v): str(k) for k, v in data.items()}
                ordered: List[Tuple[int, str]] = []
                for alias, func_name in reverse_mapping.items():
                    m = re.fullmatch(r"fun(\d+)", alias)
                    if m:
                        ordered.append((int(m.group(1)), func_name))
                ordered.sort(key=lambda x: x[0])
                function_order = [func for _, func in ordered]
                return function_order, reverse_mapping

        if isinstance(data.get("function_order"), list):
            function_order = [str(x) for x in data["function_order"]]
        elif isinstance(data.get("functions"), list):
            function_order = [str(x) for x in data["functions"]]

        rm = data.get("reverse_mapping")
        if isinstance(rm, dict):
            reverse_mapping = {str(k): str(v) for k, v in rm.items()}

        fm = data.get("function_mapping")
        if not reverse_mapping and isinstance(fm, dict):
            reverse_mapping = {str(v): str(k) for k, v in fm.items()}
            if not function_order:
                tmp = [(str(v), str(k)) for k, v in fm.items()]
                tmp.sort(key=lambda x: x[0])
                function_order = [t[1] for t in tmp]
    elif isinstance(data, list):
        function_order = [str(x) for x in data]

    if not function_order and reverse_mapping:
        tmp = [(k, v) for k, v in reverse_mapping.items()]
        tmp.sort(key=lambda x: x[0])
        function_order = [v for _, v in tmp]

    if function_order and not reverse_mapping:
        reverse_mapping = {f"fun{i+1}": func for i, func in enumerate(function_order)}

    return function_order, reverse_mapping


class SHAPModel:
    def __init__(
        self,
        model_path: str,
        mapping_json_path: str,
        static_lib_path: str,
        txt_folder: str,
    ):
        if not os.path.exists(model_path):
            raise FileNotFoundError(model_path)
        if not os.path.exists(mapping_json_path):
            raise FileNotFoundError(mapping_json_path)
        if not os.path.exists(static_lib_path):
            raise FileNotFoundError(static_lib_path)
        if not os.path.exists(txt_folder):
            raise FileNotFoundError(txt_folder)

        self.pipeline = joblib.load(model_path)
        self.model = self.pipeline['model']
        self.explainer = self.pipeline['explainer']
        self.feature_names = self.pipeline['feature_names']

        self.function_order, self.reverse_mapping = extract_function_order(mapping_json_path)
        self.static_data = load_json(static_lib_path)
        self.txt_folder = txt_folder
        self._normalized_function_order = [self._normalize_function_name(x) for x in self.function_order]
        self._norm_to_index = {
            norm: i for i, norm in enumerate(self._normalized_function_order) if norm
        }
        self._feature_mode = self._detect_feature_mode(self.feature_names)

    def _build_feature(self, json_path: str):
        """构造模型输入"""
        with open(json_path, "r") as f:
            json_data = json.load(f)

        item = self._get_latest_item(json_data)
        function_file = item.get("function_file")
        if function_file is None:
            raise ValueError(f"Missing 'function_file' in json record. keys={list(item.keys())}")
        if not isinstance(function_file, str):
            raise ValueError(f"'function_file' must be str, got {type(function_file)}")

        file_name = os.path.basename(function_file)

        txt_path = os.path.join(self.txt_folder, file_name)
        if not os.path.exists(txt_path):
            raise FileNotFoundError(txt_path)
        df = pd.read_csv(txt_path, sep='\t')
        if 'Function' not in df.columns or 'Sampling Rate (%)' not in df.columns:
            raise ValueError(f"Invalid txt format: missing columns. got={df.columns.tolist()}")

        func_rate_dict_by_index: Dict[int, List[float]] = {}
        func_rate_dict_by_name: Dict[str, List[float]] = {}
        for _, row in df.iterrows():
            raw_func = row['Function']
            norm = self._normalize_function_name(raw_func)
            rate = self._parse_rate(row['Sampling Rate (%)'])
            if rate is None:
                continue

            idx = self._norm_to_index.get(norm)
            if idx is not None:
                func_rate_dict_by_index.setdefault(idx, []).append(rate)
                continue

            if self._feature_mode == "function_name":
                if isinstance(raw_func, str):
                    func_rate_dict_by_name.setdefault(raw_func, []).append(rate)
                elif raw_func is not None:
                    func_rate_dict_by_name.setdefault(str(raw_func), []).append(rate)

        if self._feature_mode == "fun_index":
            feature_dict: Dict[str, float] = {}
            for i in range(len(self.function_order)):
                fname = f"fun{i+1}"
                rates = func_rate_dict_by_index.get(i)
                feature_dict[fname] = float(np.mean(rates)) if rates else 0.0
            input_df = pd.DataFrame([feature_dict])
            input_df = input_df.reindex(columns=self.feature_names, fill_value=0.0)
        else:
            feature_dict = {}
            for fname in self.feature_names:
                rates = func_rate_dict_by_name.get(fname)
                feature_dict[fname] = float(np.mean(rates)) if rates else 0.0
            input_df = pd.DataFrame([feature_dict])

        if float(np.sum(input_df.iloc[0].values)) == 0.0:
            raise ValueError(
                "All-zero feature vector. Usually means perf Function names do not match mapping/model features."
            )

        return input_df

    @staticmethod
    def _get_latest_item(json_data: Any) -> Dict[str, Any]:
        if isinstance(json_data, list):
            if not json_data:
                raise ValueError("json list is empty")
            item = json_data[-1]
        elif isinstance(json_data, dict):
            item = None
            for key in ("history", "records", "data", "items", "profiles", "profile"):
                v = json_data.get(key)
                if isinstance(v, list) and v:
                    item = v[-1]
                    break
            if item is None:
                item = json_data
        else:
            raise ValueError(f"Unsupported json root type: {type(json_data)}")

        if not isinstance(item, dict):
            raise ValueError(f"Latest record must be dict, got {type(item)}")
        return item

    @staticmethod
    def _detect_feature_mode(feature_names: Any) -> str:
        if not isinstance(feature_names, (list, tuple)):
            return "fun_index"
        if any(isinstance(x, str) and re.fullmatch(r"fun\d+", x) for x in feature_names):
            return "fun_index"
        return "function_name"

    @staticmethod
    def _normalize_function_name(func: Any) -> str:
        if func is None:
            return ""
        s = str(func).strip()
        s = re.sub(r"\s+", " ", s)
        if "(" in s:
            s = s.split("(", 1)[0].strip()
        return s

    @staticmethod
    def _parse_rate(v: Any) -> Optional[float]:
        if v is None:
            return None
        if isinstance(v, (int, float, np.floating)):
            try:
                return float(v)
            except Exception:
                return None
        s = str(v).strip()
        if not s:
            return None
        s = s.replace("%", "")
        try:
            return float(s)
        except Exception:
            return None

    def explain_system_features(self, model_input: dict) -> list[dict]:
        """
        model_input:
        {
            "json_path": "...",
            "top_k": 20
        }
        """
        json_path = model_input["json_path"]
        top_k = model_input.get("top_k", 20)

        input_df = self._build_feature(json_path)
        shap_values = self.explainer.shap_values(input_df)

        shap_df = pd.DataFrame({
            'feature': input_df.columns,
            'value': input_df.iloc[0].values,
            'shap': shap_values[0]
        }).sort_values(by='shap', ascending=False).head(top_k)

        shap_df['function_name'] = shap_df['feature'].map(self.reverse_mapping)
        if shap_df['function_name'].isna().any():
            shap_df['function_name'] = shap_df.apply(
                lambda r: self._fallback_map_function_name(str(r['feature'])), axis=1
            )

        return shap_df.to_dict(orient="records")
    
    
    @staticmethod
    def match_functions(function_list, static_data):
        """
        function_list: ["funcA", "funcB", ...]
        """
        output_data_list = []

        for json_data in static_data:
            matched_data_flow = []
            matched_control_flow = []

            for func in json_data['data_flow_functions']:
                if func in function_list:
                    matched_data_flow.append(func)

            for func in json_data['control_flow_functions']:
                if func in function_list:
                    matched_control_flow.append(func)

            if matched_data_flow or matched_control_flow:
                output_data_list.append({
                    "knob_name": json_data['knob_name'],
                    "data_flow_functions": matched_data_flow,
                    "control_flow_functions": matched_control_flow
                })

        return output_data_list

    def explain_knob_features(self, model_input: dict) -> list[dict]:
        # Step1: 获取带SHAP的函数
        system_features = self.explain_system_features(model_input)

        # Step2: 只提取函数名（丢弃 shap）
        function_list = [
            item['function_name']
            for item in system_features
            if item['function_name'] is not None
        ]

        # Step3: 匹配参数
        matched_knobs = self.match_functions(
            function_list,
            self.static_data
        )

        return matched_knobs

    def _fallback_map_function_name(self, feature_name: str) -> Any:
        if feature_name in self.reverse_mapping:
            return self.reverse_mapping[feature_name]
        m = re.fullmatch(r"fun(\d+)", str(feature_name))
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(self.function_order):
                return self.function_order[idx]
            if m.group(1) in self.reverse_mapping:
                return self.reverse_mapping[m.group(1)]
        return np.nan
    

if __name__ == "__main__":
    from config import MODEL_OLTP, MAPPING_OLTP, STATIC_LIB_FILE, PERF_OUTPUT_DIR, HISTORY_PERF_SYSBENCH

    model = SHAPModel(
        model_path=MODEL_OLTP,
        mapping_json_path=MAPPING_OLTP,
        static_lib_path=STATIC_LIB_FILE,
        txt_folder=PERF_OUTPUT_DIR,
    )

    # 系统级解释（函数）
    system_features = model.explain_system_features({
        "json_path": HISTORY_PERF_SYSBENCH,
        "top_k": 20,
    })
    print("System-level SHAP features:")
    for feature in system_features:
        print(feature)

    # # 参数级解释（knob）
    # sql_features = model.explain_knob_features({
    #     "json_path": "xxx.json",
    # })
