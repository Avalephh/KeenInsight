import csv
import json
import os
import pandas as pd
from collections import Counter

def load_json(json_file):
    with open(json_file, 'r') as f:
        return json.load(f)

def read_csv(csv_file_path):
    csv_functions = set()
    with open(csv_file_path, mode='r') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            csv_functions.add(row['Function'])
    return csv_functions

def read_function_names(file_path, num_lines=400):
    function_names = []
    with open(file_path, 'r', encoding='utf-8') as file:
        # 跳过文件的标题行
        next(file)
        
        for i, line in enumerate(file):
            if i >= num_lines:
                break
            # 提取每行的第一个字段，即函数名称
            function_name = line.split('\t')[0]
            function_names.append(function_name)
    
    return function_names

def read_function_names_with_change(file_path, num_lines=400):
    functions_with_change = []
    with open(file_path, 'r', encoding='utf-8') as file:
        # Skip the header line
        next(file)
        
        for i, line in enumerate(file):
            if i >= num_lines:
                break
            # Split the line into columns
            columns = line.strip().split('\t')
            if len(columns) >= 4:
                function_name = columns[0]
                change = int(columns[3])  # Convert change to integer
                # Store function name and change in a tuple or dictionary
                functions_with_change.append((function_name, change))
    
    return functions_with_change

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
    
    return sorted_functions[:num_functions]  # 返回前 num_functions 个函数

def match_functions(csv_functions, json_data_list):
    output_data_list = []
    for json_data in json_data_list:
        output_data = {
            "knob_name": json_data['knob_name'],
            "data_flow_functions_in_csv": [],
            "control_flow_functions_in_csv": [],
            "data_flow_functions_matched_num": 0,
            "control_flow_functions_matched_num": 0,
            "total_functions_matched_num": 0
            
        }
        for func in json_data['data_flow_functions']:
            if func in csv_functions:
                output_data['data_flow_functions_in_csv'].append(func)
                output_data['data_flow_functions_matched_num'] += 1

        for func in json_data['control_flow_functions']:
            if func in csv_functions:
                output_data['control_flow_functions_in_csv'].append(func)
                output_data['control_flow_functions_matched_num'] += 1
        
        output_data['total_functions_matched_num'] = output_data['data_flow_functions_matched_num'] + output_data['control_flow_functions_matched_num']
        output_data_list.append(output_data)
    
    return output_data_list

def match_functions_1(csv_functions, json_data_list):
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

def match_functions_2(csv_functions, json_data_list):
    output_data_list = []
    csv_func_to_knob = {func: [] for func in csv_functions}
    
    for json_data in json_data_list:
        data_flow_functions_in_csv = []
        control_flow_functions_in_csv = []
        data_flow_functions_matched_num = 0
        control_flow_functions_matched_num = 0
        
        knob_name = json_data['knob_name']

        for func in json_data['data_flow_functions']:
            if func in csv_functions:
                data_flow_functions_in_csv.append(func)
                data_flow_functions_matched_num += 1
                csv_func_to_knob[func].append(knob_name)

        for func in json_data['control_flow_functions']:
            if func in csv_functions:
                control_flow_functions_in_csv.append(func)
                control_flow_functions_matched_num += 1
                csv_func_to_knob[func].append(knob_name)

        total_functions_matched_num = data_flow_functions_matched_num + control_flow_functions_matched_num

        if total_functions_matched_num > 0:
            output_data = {
                "knob_name": knob_name,
                "data_flow_functions": data_flow_functions_in_csv,
                "control_flow_functions": control_flow_functions_in_csv
            }
            output_data_list.append(output_data)
    
    return output_data_list, csv_func_to_knob

def matchFunctions_knob(csv_functions, json_data_list):
    # 将 function_to_knobs 初始化为一个字典
    function_to_knobs = {}

    for json_data in json_data_list:
        knob_name = json_data["knob_name"]
        data_flow_functions = json_data["data_flow_functions"]
        control_flow_functions = json_data["control_flow_functions"]

        for function in csv_functions:
            if function in data_flow_functions or function in control_flow_functions:
                # 如果 function 不在字典中，初始化为一个空列表
                if function not in function_to_knobs:
                    function_to_knobs[function] = []
                function_to_knobs[function].append(knob_name)
    
    return function_to_knobs

# 如果匹配上的参数数量达到阈值，就停止匹配函数
def find_top_and_matched_functions(txt_file_path, static_lib_path):
    static_data = load_json(static_lib_path)
    all_functions = read_function_names_with_params(txt_file_path, num_functions=400)  # 获取排序后的所有函数
    top_functions = all_functions[:20]
    function_names = [func[0] for func in top_functions]
    
    matched_knob, csv_func_to_knob = match_functions_2(function_names, static_data)

    # 如果匹配函数少于 30 个，继续往下选函数尝试匹配
    idx = 20
    additional = []
    while len(matched_knob) < 30 and idx < len(all_functions):
        additional_functions = all_functions[idx:idx + 1]
        additional_function_names = [func[0] for func in additional_functions]

        additional_matched_knob, additional_func_to_knobs = match_functions_2(additional_function_names, static_data)
        
        if additional_matched_knob:
            additional += additional_functions
            matched_knob += additional_matched_knob

            # 合并函数与knob的映射
            for func, knobs in additional_func_to_knobs.items():
                if func not in csv_func_to_knob:
                    csv_func_to_knob[func] = knobs
                else:
                    csv_func_to_knob[func].extend(knobs)
        
        idx += 1

    bkFunctions_list = top_functions + additional
    bkFunctions_list = flatten_list(bkFunctions_list)  # 如果原始结构需要扁平化

    return bkFunctions_list, matched_knob, csv_func_to_knob

def flatten_list(nested_list):
    flat_list = []
    for item in nested_list:
        if isinstance(item, list):
            flat_list.extend(item)
        else:
            flat_list.append(item)
    return flat_list

def print_matched_functions(matched_data):
    if not matched_data:
        print("No matched functions found.")
        return
    uKnobs = []
    for item in matched_data:
        if item['total_functions_matched_num'] > 0:
            result = {
                "knob_name": item['knob_name'],
                "data_flow_functions": item['data_flow_functions_in_csv'],
                "control_flow_functions": item['control_flow_functions_in_csv']
            }
            uKnobs.append(result)
            # print(f"Knob: {item['knob_name']}")
            # print(f"Data flow functions matched: {item['data_flow_functions_in_csv']}")
            # print(f"Control flow functions matched: {item['control_flow_functions_in_csv']}")
            # print(f"Total functions matched: {item['total_functions_matched_num']}\n")
    return uKnobs

# keyFunctions匹配staticKnobs中的函数 和 参数
def match_knob_functions(keyFile,staticFile):
    keyFunctions_list = read_function_names(keyFile)
    static_functions = load_json(staticFile)
    matched_data = match_functions(keyFunctions_list, static_functions)
    uKnobs = print_matched_functions(matched_data)
    return uKnobs

# 从keyFunctions中获取函数名和对应的参数
def get_knob_in_keyFunctions(keyFile,staticFile):
    keyFunctions_list = read_function_names(keyFile)
    static_functions = load_json(staticFile)
    function_to_knob = matchFunctions_knob(keyFunctions_list, static_functions)
    return function_to_knob

# 投票机制，获取最多的参数
def getTopKnob(knob_list, top_k=5):
    knob_count = Counter()
    for params in knob_list.values():
        knob_count.update(params)
    top_knobs = knob_count.most_common(top_k)
    return top_knobs


    
if __name__ == '__main__':
    json_file = "/root/sysinsight-main/DBTuner/utils/staticKnobs.json"  # 原始存储参数和函数的 JSON 文件
    perf_file = "/root/AI4DB/function_sampling_148_default.csv"   # perf 文件
    output_file = "matched_functions.json"  # 保存匹配函数的输出 JSON 文件

    # 加载 JSON 文件和 perf 文件
    # json_data = load_json(json_file)
    # perf_df = read_csv(perf_file)
    file_path = '/root/AI4DB/hzt/perf_data/perf_1732953643_counts_bad_btFunctions.txt'
    # 匹配函数
    # uKnobs = match_knob_functions(file_path, json_file)
    # # print(uKnobs)
    # knob_names = [item['knob_name'] for item in uKnobs]
    # # print(knob_names)

    # keyFunctions_list = read_function_names(file_path)
    # # print(keyFunctions_list)
    # function_to_knob = get_knob_in_keyFunctions(file_path, json_file)
    # # print(function_to_knob)
    # top_knobs = getTopKnob(function_to_knob)
    # top_param_names = [param for param, count in top_knobs]
    # print(top_param_names)

    # ww, knob = find_top_and_matched_functions(file_path, json_file)
    # print(knob)
    # print(ww)
