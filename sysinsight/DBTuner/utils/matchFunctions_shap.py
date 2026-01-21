import pandas as pd
import numpy as np
import joblib
import json
import os
import re

# 加载参数关联库
def load_json(json_file):
    with open(json_file, 'r') as f:
        return json.load(f)

def extract_function_order(mapping_json_path):
    with open(mapping_json_path, "r") as f:
        mapping = json.load(f)

    # 创建 fun 编号 -> 原始函数名的反向映射
    reverse_mapping = {v: k for k, v in mapping.items()}

    # 创建以 fun 编号排序的元组列表 [(fun_index, function_name), ...]
    ordered = []
    for alias, func_name in reverse_mapping.items():
        match = re.match(r'fun(\d+)', alias)
        if match:
            index = int(match.group(1))
            ordered.append((index, func_name))

    ordered.sort(key=lambda x: x[0])
    function_order = [func_name for _, func_name in ordered]
    return function_order, reverse_mapping


def process_new_sample_and_get_shap_topk(
    model_path, json_path, txt_folder, function_order, reverse_mapping, top_k=5
):
    # 加载模型和解释器
    pipeline = joblib.load(model_path)
    model = pipeline['model']
    explainer = pipeline['explainer']
    feature_names = pipeline['feature_names']

    # 创建函数映射
    function_mapping = {func: f"fun{i+1}" for i, func in enumerate(function_order)}
    
    # 读取 JSON 数据（默认只处理第一个）
    with open(json_path, "r") as f:
        json_data = json.load(f)

    item = json_data[-1]  # 处理第一个样本
    raw_tps = item["external_metrics"]["tps"]
    file_name = os.path.basename(item["function_file"])
    benchmark_type = "sysbench" if "sysbench" in file_name.lower() else "tpcc"

    # 读取对应 txt 文件数据
    txt_path = os.path.join(txt_folder, file_name)
    if not os.path.exists(txt_path):
        raise FileNotFoundError(f"Function file {txt_path} not found")

    df = pd.read_csv(txt_path, sep='\t')
    func_rate_dict = {}
    for _, row in df.iterrows():
        function = row['Function']
        if function in function_order:
            rate = float(str(row['Sampling Rate (%)']).replace('%', ''))
            func_rate_dict.setdefault(function, []).append(rate)

    # 构造与训练集一致的特征向量
    feature_dict = {"benchmark_type": benchmark_type}
    for func in function_order:
        fname = function_mapping[func]
        feature_dict[fname] = np.mean(func_rate_dict.get(func, [0.0]))

    # 构建 DataFrame 并对齐列顺序
    input_df = pd.DataFrame([feature_dict])
    input_df = input_df.drop(columns=['benchmark_type'], errors='ignore')
    input_df = input_df.reindex(columns=feature_names, fill_value=0.0)

    # 获取 SHAP 值
    shap_values = explainer.shap_values(input_df)

    # 输出前 K 个重要特征及其值和 SHAP 值
    shap_df = pd.DataFrame({
        'feature': input_df.columns,
        'value': input_df.iloc[0].values,
        'shap': shap_values[0]
    }).sort_values(by='shap', ascending=False).head(top_k)

    # 添加映射回真实函数名
    shap_df['function_name'] = shap_df['feature'].map(reverse_mapping)

    return shap_df

def match_functions(csv_functions, json_data_list):
    output_data_list = []
    for json_data in json_data_list:
        data_flow_functions_in_csv = []
        control_flow_functions_in_csv = []
        data_flow_functions_matched_num = 0
        control_flow_functions_matched_num = 0
        
        for func in json_data['data_flow_functions']:
            if func in csv_functions:
                data_flow_functions_in_csv.append(func)
                data_flow_functions_matched_num += 1

        for func in json_data['control_flow_functions']:
            if func in csv_functions:
                control_flow_functions_in_csv.append(func)
                control_flow_functions_matched_num += 1
        
        total_functions_matched_num = data_flow_functions_matched_num + control_flow_functions_matched_num
        
        if total_functions_matched_num > 0:
            output_data = {
                "knob_name": json_data['knob_name'],
                "data_flow_functions": data_flow_functions_in_csv,
                "control_flow_functions": control_flow_functions_in_csv
            }
            output_data_list.append(output_data)
    
    return output_data_list

def getShapFuncKnobs(static_lib_path, txt_file_path, result_path):
    static_data = load_json(static_lib_path)
    # 获取shap选取的函数和其值
    model_path = "/root/RUC/DBTune/scripts/premodel/performance_model_lat.pkl"
    mapping_json_path = "/root/RUC/DBTune/scripts/premodel/function_mapping_latency.json"
    # model_path = "/root/RUC/DBTune/scripts/premodel/performance_model_tps_600.pkl"
    # mapping_json_path = "/root/RUC/DBTune/scripts/premodel/function_mapping.json"
    txt_folder = "/root/sysinsight-main/perf_data"
    
    function_order, reverse_mapping = extract_function_order(mapping_json_path)
    top_features = process_new_sample_and_get_shap_topk(
        model_path, result_path, txt_folder, function_order, reverse_mapping, top_k=30
    )
    # 获取函数列表
    function_name_list = top_features['function_name'].tolist()
    # print("ffff: ", function_name_list)
    # 函数匹配参数
    matched_knob = match_functions(function_name_list, static_data)
    # 返回函数值+shap
    # function_value_list = list(zip(top_features['function_name'], top_features['value']))
    # 返回函数对应的采样率+变化
    function_name_set = set(function_name_list)
    all_functions = read_function_names_with_params(txt_file_path, num_functions=2000)
    matched_functions = [item for item in all_functions if item[0] in function_name_set]
    
    print("ffff: ", matched_functions)
    
    return matched_functions, matched_knob
    

def read_function_names_with_params(file_path, num_functions=5):
    functions = []
    with open(file_path, 'r', encoding='utf-8') as file:
        # 跳过头部
        next(file)
        for line in file:
            columns = line.strip().split('\t')
            if len(columns) >= 4:
                function_name = columns[0]
                sample_rate = float(columns[1])  # 转换为浮点数
                diff_from_mean = float(columns[2])  # 转换为浮点数
                change = int(columns[3])  # 转换为整数
                functions.append((function_name, sample_rate, diff_from_mean, change))
    
    # 按变化绝对值（Diff From Mean）降序排序
    sorted_functions = sorted(functions, key=lambda x: x[2], reverse=True)
    
    return sorted_functions[:num_functions]  
    

if __name__ == "__main__":
    model_path = "performance_model.pkl"
    json_path = "/root/sysinsight-main/rule_collect_results_tpcc-benchbase_tpcc.json"
    txt_folder = "/root/sysinsight-main/perf_data_benchbase_tpcc"
    mapping_json_path = "function_mapping.json"

    # function_order, reverse_mapping = extract_function_order(mapping_json_path)
    # # print("Function order:", function_order)

    # top_features = process_new_sample_and_get_shap_topk(
    #     model_path, json_path, txt_folder, function_order, reverse_mapping, top_k=10
    # )

    # print("Top SHAP features for new sample:")
    # print(top_features[['feature', 'function_name','value','shap']])
    
    # static_json_file = "/root/sysinsight-main/DBTuner/utils/parameter_colle_library.json"
    # ww,uknobs = getShapFuncKnobs(static_json_file,json_path)
    # print(ww)
    # print("xxxxxxxxxx")
    # print(uknobs)
