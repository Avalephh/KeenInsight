import os
import re
import json
from DBTuner.utils.matchFunctions import match_knob_functions,read_function_names

def load_json(json_file):
    with open(json_file, 'r') as f:
        return json.load(f)
    
def extract_code_for_function(folder_path, file_name, function_name):
    # 构建完整的文件路径
    file_path = os.path.join(folder_path, file_name)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            lines = file.readlines()
    except FileNotFoundError:
        print(f"File not found: {file_path}")
        return None
    
    # 标记是否在目标函数部分
    in_function_section = False
    in_function_code = False
    code_lines = []

    # 正则模式匹配
    separator_pattern = re.compile(r'^-{5,}')  # 匹配分隔线
    function_name_pattern = re.compile(rf'^\s*Function:\s*{re.escape(function_name)}\b')
    file_marker_pattern = re.compile(r'^\s*File:|^-{5,}')  # 匹配新的文件或分隔线

    for line in lines:
        # 检测到分隔线时，标记进入函数部分
        if separator_pattern.search(line):
            in_function_section = True
            continue

        # 在函数部分内，检测到目标函数名时开始收集代码
        if in_function_section and function_name_pattern.search(line):
            in_function_code = True
            continue

        # 在函数代码部分内收集代码行
        if in_function_code:
            # 如果遇到分隔线或新文件标记，停止收集
            if file_marker_pattern.search(line):
                break
            code_lines.append(line)

    # 将收集的代码行组合成单个字符串
    return ''.join(code_lines).strip()

def extract_code_for_knob_from_json(data, folder_path, knob_name):
    # 读取 JSON 文件
    # with open(json_file_path, 'r', encoding='utf-8') as json_file:
    #     data = json.load(json_file)
    
    #TODO： 查找指定 knob_name 的数据流函数列表
    # result = {"knob": knob_name, "functions": []}
    
    # for item in data:
    #     if item.get("knob_name") == knob_name:
    #         data_flow_functions = item.get("data_flow_functions", [])
    #         for function_name in data_flow_functions:
    #             file_name = f"{knob_name}_code.txt"  # 假设每个 knob 有对应的代码文件
    #             code = extract_code_for_function(folder_path, file_name, function_name)
    #             if code:
    #                 result["functions"].append({
    #                     "function": function_name,
    #                     "code": code
    #                 })
    #         break  # 找到对应的 knob_name 后跳出循环

    # return result

    all_results = []

    for item in data:
        knob_name = item.get("knob_name")
        data_flow_functions = item.get("data_flow_functions", [])
        
        knob_result = {"knob_name": knob_name, "data_flow_functions_code": []}
        
        for function_name in data_flow_functions:
            file_name = f"{knob_name}_code.txt"
            code = extract_code_for_function(folder_path, file_name, function_name)
            if code:
                knob_result["data_flow_functions_code"].append({
                    "function": function_name,
                    "code": code
                })
        
        all_results.append(knob_result)
    
    return all_results

# 使用文件夹路径、JSON 文件路径和参数名称调用函数
# json_file_path = '/root/AI4DB/hzt/DBTuner/utils/cleaned_knob_names.json'
# folder_path = '/root/AI4DB/hzt/library/extractCode'
# knob_name = 'tmp_table_size'
# file_path = '/root/AI4DB/hzt/perf_data/perf_1731552962_counts_bad_keyFunctions.txt'

# # json_data = load_json(json_file_path)
# uKnobs = match_knob_functions(file_path, json_file_path)
# knob_names = [item['knob_name'] for item in uKnobs]
# # print(knob_names)

# result = extract_code_for_knob_from_json(uKnobs, folder_path, knob_name)
# print(result)   
# 输出结果
# for knob_result in result:
#     print(f"knob_name: {knob_result['knob_name']}")
#     for function in knob_result['data_flow_functions_code']:
#         print(f"function: {function['function']}")
#         print(f"code: {function ['code']}\n")   
