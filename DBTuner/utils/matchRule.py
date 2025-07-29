import argparse
import json
import math
import os
import random
import re
from DBTuner.config import parse_args
from DBTuner.dbenv import DBEnv
from DBTuner.database.mysqldb import MysqlDB
import numpy as np
import pandas as pd
import time

# 规则挖掘数据总数
TOTAL_NUM = 4950

# 参数值去标准化
def knob_denormalize(default_file, knob_name, normalized_value):
    """
    参数值去标准化
    """
    epsilon = 1e-9

    # 加载参数配置文件
    with open(default_file, "r") as file:
        data = json.load(file)

    # 获取参数信息
    knob_info = data[knob_name]
    knob_type = knob_info["type"]

    if knob_type == 'integer':
        max_val = knob_info["max"]
        min_val = knob_info["min"]

        # 保障 normalized_value 在 [0, 1] 范围
        normalized_value = max(0.0, min(1.0, normalized_value))

        if (max_val - min_val) > (2 ** 15):  # 使用 log 标准化
            if min_val + epsilon <= 0:
                min_val = 1e-4

            log_max = math.log(max_val + epsilon)
            log_min = math.log(min_val + epsilon)
            log_value = log_min + normalized_value * (log_max - log_min)
            real_value = math.exp(log_value) - epsilon
            return int(round(real_value))
        else:
            real_value = min_val + normalized_value * (max_val - min_val)
            return int(round(real_value))

    elif knob_type == 'enum':
        possible_values = knob_info["enum_values"]
        index = int(round(normalized_value * (len(possible_values) - 1)))
        index = max(0, min(len(possible_values) - 1, index))  # 保证索引合法
        return possible_values[index]

    else:
        return None 

# 参数标准化
def knob_normalize(default_file, knob_name, value):
    """
    标准化参数值
    - 如果参数的 max 和 min 差值大于 2 的 10 次方（即 1024 倍）则采用 log 标准化
    - 否则使用线性标准化
    """
    epsilon = 1e-9 
    # 加载对应参数的配置文件
    # with open(os.path.join(DEFAULT_KNOB_VALUES_PATH, f"{knob_name}.json"), "r") as file:
    #     data = json.load(file)
    # 修改为一个json文件中，但有多个参数的情况
    with open(default_file, "r") as file:
        data = json.load(file)
    
    # print(f"knob_name: {knob_name}, value: {value}")    

    # 获取参数的类型和范围
    knob_info = data[knob_name]
    # print(f"knob_info: {knob_info}")
    knob_type = knob_info["type"]

    if knob_type == 'integer':
        max_val = knob_info["max"]
        min_val = knob_info["min"]
        # 判断是否需要 log 标准化
        if (max_val - min_val) > (2 ** 15):  # 差值大于 2^15
            if min_val + epsilon <= 0:
                min_val = 1e-4 
            if value + epsilon <= 0:
                value = 1e-4 
            log_max = math.log(max_val + epsilon)
            log_min = math.log(min_val + epsilon)
            log_value = math.log(value + epsilon)
            return (log_value - log_min) / (log_max - log_min)  
        else:
            # 差值较小，使用线性标准化
            return (value - min_val) / (max_val - min_val)
    elif knob_type == 'enum':
        # 离散型的标准化（枚举类型）
        possible_values = knob_info["enum_values"]
        if value in possible_values:
            return possible_values.index(value) / (len(possible_values) - 1)
        else:
            raise ValueError(f"Value '{value}' for knob '{knob_name}' is not in the list of possible values.")
    else:
        # 非整数类型的参数暂不处理
        # print(f"The type of knob '{knob_name}' is not 'integer'. Skipping normalization.")
        return None

# 判断函数的值是否在规则范围内
def check_function_rates(rates_dict, rule_dict):
    not_in_range = []
    for func_info in rule_dict['function']:
        func_name = func_info['name']
        lower_bound = func_info['lower_bound']
        upper_bound = func_info['upper_bound']
        if func_name in rates_dict:
            rate = rates_dict[func_name]
            if not (lower_bound <= rate <= upper_bound):
                not_in_range.append(func_name)
    if not_in_range:
        # print(f"Functions {not_in_range} not in range.")
        return False
    return True

# 处理规则格式
def process_rule(rule):
    processed_rule = {}
    parts = rule.split("===>")
    left_part = parts[0]
    right_part = parts[1].split(' ')[0]  # 提取tps_diff相关部分
    
    support = float(parts[1].split(' ')[1])
    confidence = float(parts[1].split(' ')[2])
    count = int(parts[1].split(' ')[3])
    processed_rule["count"] = count
    # print(f"support: {support}, confidence: {confidence}")
    # 不处理support和confidence小于0.5的规则
    if support < 0.5 or confidence < 0.5:
        return 0

    # TODO: 记得到时候根据不同的参数传knob_names
    knob_names = [
        "binlog_cache_size", "innodb_buffer_pool_size", "innodb_io_capacity",
        "innodb_io_capacity_max", "innodb_lru_scan_depth", "innodb_purge_batch_size",
        "innodb_spin_wait_delay", "join_buffer_size", "max_heap_table_size", "tmp_table_size"
    ]

    # 提取规则前件中的函数相关项及对应的区间
    function_items = []
    param_items = []
    for item in left_part[left_part.find('[') + 1:left_part.find(']')].split(','):
        item = item.strip()
        parts = item.split('_')
        if any(knob_name in item for knob_name in knob_names):
            # 如果包含在knob_names中，就是参数相关项
            param_name = '_'.join(parts[:-2]).strip("'")  # 处理包含 _ 的参数名情况，取前面部分作为参数名
            lower_bound = float(parts[-2].strip("'"))  # 去除可能存在的单引号后转换为浮点数
            upper_bound = float(parts[-1].strip("'"))
            param_items.append({
                "name": param_name,
                "lower_bound": lower_bound,
                "upper_bound": upper_bound
            })
        else:
            # 处理函数相关项，取除最后两个元素外的部分作为函数名
            function_name = '_'.join(parts[:-2]).strip("'")
            lower_bound = float(parts[-2].strip("'"))
            upper_bound = float(parts[-1].strip("'"))
            function_items.append({
                "name": function_name,
                "lower_bound": lower_bound,
                "upper_bound": upper_bound
            })

    # 提取tps相关的区间
    right_part_str = right_part.strip('[\']')
    # print(right_part_str)
    # 如果为tps_diff_A, 则lower_bound=0，upper_bound=20
    
    tps_pattern = re.compile(r"tps_diff_(.*)")
    match = tps_pattern.match(right_part_str)
    if match:
        tps_category = match.group(1).strip("'")
        if tps_category == 'A':
            tps_lower_bound = 0
            tps_upper_bound = 20
        elif tps_category == 'B':
            tps_lower_bound = 20
            tps_upper_bound = 50
        elif tps_category == 'C':
            tps_lower_bound = 50
            tps_upper_bound = float("inf")
        else:
            raise ValueError(f"Unknown tps category: {tps_category}")
        
        processed_rule["tps"] = {
            "lower_bound": tps_lower_bound,
            "upper_bound": tps_upper_bound
        }
    else:
        return processed_rule
    # tps_pattern = re.compile(r"tps_diff_(.*)_(.*)")
    # match = tps_pattern.match(right_part_str)
    # if match:
    #     tps_lower_bound = float(match.group(1).strip("'"))
    #     tps_upper_bound = float(match.group(2).strip("'"))
    #     processed_rule["tps"] = {
    #         "lower_bound": tps_lower_bound,
    #         "upper_bound": tps_upper_bound
    #     }
    # else:
    #     return processed_rule

    processed_rule["function"] = function_items
    processed_rule["knob"] = param_items

    return processed_rule

def process_rule_catagory(rule):
    
    processed_rule = {}
    rule_parts = rule.split("=>")
    if len(rule_parts) != 2:
        raise ValueError("规则格式错误，无法解析: {}".format(rule))

    # 解析规则左侧部分（前件，包括参数和函数）
    left_part = rule_parts[0].strip()
    knobs, functions = [], []

    # 匹配参数 (knob) 和函数 (function)
    # 优化 knob_pattern 以匹配包含 'knob' 前缀及逗号分隔的规则
    knob_pattern = re.compile(r"knob\s(\w+)\s(down|up)\s(?:(lt|mt)\s)?(\d+(?:\.\d+)?)(?:~(\d+(?:\.\d+)?))?")
    function_pattern = re.compile(r"(\w+)\s(?:(above|below)\s(\d+(?:\.\d+)?)(?:~(\d+(?:\.\d+)?))?|(\d+(?:\.\d+)?) to (\d+(?:\.\d+)?))")

    for match in knob_pattern.finditer(left_part):
        knob = match.group()
        knob = knob.replace("knob ", "")
        knobs.append(parse_knob_or_function(knob, "knob"))

    for match in function_pattern.finditer(left_part):
        function = match.group()
        functions.append(parse_knob_or_function(function, "function"))

    # 解析规则右侧部分（后件，包括 TPS）
    right_part = rule_parts[1].strip()
    # print(f"right_part: {right_part}")
    support_confidence_lift_pattern = re.compile(r"支持度:\s(\d+\.\d+),\s置信度:\s(\d+\.\d+),\s提升度:\s(\d+\.\d+),\s数据总数:\s(\d+)")
    match = support_confidence_lift_pattern.search(right_part)
    if match:
        support = float(match.group(1))
        confidence = float(match.group(2))
        lift = float(match.group(3))
        total_num = int(match.group(4))
        # print(f"support: {support}, confidence: {confidence}, lift: {lift},total_num:{total_num}")
    
    # 解析 TPS 或 Latency，只存储一个
    performance = None

    tps_pattern = re.compile(r"tps improve (\d+(?:\.\d+)?)(?:[~～](\d+(?:\.\d+)?))?\s*(above|below)?")
    tps_match = tps_pattern.search(right_part)
    if tps_match:
        lower_bound = float(tps_match.group(1))
        upper_bound = float(tps_match.group(2)) if tps_match.group(2) else lower_bound

        if tps_match.group(3) == "above":
            upper_bound = float('inf')  # 无上限
        elif tps_match.group(3) == "below":
            lower_bound = -float('inf')  # 无下限

        performance = {
            "type": "tps",
            "lower_bound": lower_bound,
            "upper_bound": upper_bound
        }


    # TODO 修改正则表达式，完全匹配
    lat_pattern = re.compile(r"lat decrease (\d+(?:\.\d+)?)(?:[~～](\d+(?:\.\d+)?))?(?:\s+above)?")
    lat_match = lat_pattern.search(right_part)
    if lat_match:
        lower_bound = -float(lat_match.group(2)) if lat_match.group(2) else float('-inf')  # 最小值 -∞
        upper_bound = -float(lat_match.group(1))
        performance = {
            "type": "lat",
            "lower_bound": lower_bound,
            "upper_bound": upper_bound
        }

    if not performance:
        raise ValueError("未找到有效的 TPS 或 Latency 区间信息")
    

    # 整合结果
    processed_rule["function"] = functions
    processed_rule["knob"] = knobs
    processed_rule["performance"] = performance
    processed_rule["support"] = support
    processed_rule["confidence"] = confidence
    processed_rule["lift"] = lift
    processed_rule["total_num"] = total_num
    
    return processed_rule

# 分类型
def parse_knob_or_function(item, item_type):
    if item_type == "knob":
        # 匹配 up x1~x2
        knob_match = re.match(r"(\w+)\sup\s(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": float(knob_match.group(2))-0.001,
                "upper_bound": float(knob_match.group(3)),
            }

        # # 匹配 up x
        # knob_match = re.match(r"(\w+)\sup\s(\d+(?:\.\d+)?)", item)
        # if knob_match:
        #     return {
        #         "name": knob_match.group(1),
        #         "lower_bound": float(knob_match.group(2)),
        #         "upper_bound": float("inf"),
        #     }

        # 匹配 down x1~x2
        knob_match = re.match(r"(\w+)\sdown\s(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": -(float(knob_match.group(3))+0.001),
                "upper_bound": -float(knob_match.group(2)),
            }

        # 匹配 down x
        # knob_match = re.match(r"(\w+)\sdown\s(\d+(?:\.\d+)?)", item)
        # if knob_match:
        #     return {
        #         "name": knob_match.group(1),
        #         "lower_bound": -float("inf"),
        #         "upper_bound": -float(knob_match.group(2)),
        #     }

        # 匹配 up lt x
        knob_match = re.match(r"(\w+)\sup\slt\s(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": 0,
                "upper_bound": float(knob_match.group(2)),
            }

        # 匹配 down lt x
        knob_match = re.match(r"(\w+)\sdown\slt\s(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": -(float(knob_match.group(2))+0.001),
                "upper_bound": 0,
            }

        # 匹配 up mt x
        knob_match = re.match(r"(\w+)\sup\smt\s(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": float(knob_match.group(2))+0.001,
                "upper_bound": float("inf"),
            }

        # 匹配 down mt x
        knob_match = re.match(r"(\w+)\sdown\smt\s(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": -float("inf"),
                "upper_bound": -float(knob_match.group(2)),
            }
        
        # 匹配 change lt x
        knob_match = re.match(r"(\w+)\schange\slt\s(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": 0,
                "upper_bound": float(knob_match.group(2)),
            }
            
        # 匹配 change mt x
        knob_match = re.match(r"(\w+)\schange\smt\s(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": float(knob_match.group(2))+0.001,
                "upper_bound": float("inf"),
            }
            
        # 匹配 change x1~x2
        knob_match = re.match(r"(\w+)\schange\s(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)", item)
        if knob_match:
            return {
                "name": knob_match.group(1),
                "lower_bound": float(knob_match.group(3))+0.001,
                "upper_bound": float(knob_match.group(2)),
            }
        

    elif item_type == "function":
        # 匹配 above x1~x2
        func_match = re.match(r"(\w+)\sabove\s(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)", item)
        if func_match:
            return {
                "name": func_match.group(1),
                "lower_bound": float(func_match.group(2)),
                "upper_bound": float(func_match.group(3)),
            }

        # 匹配 above x
        func_match = re.match(r"(\w+)\sabove\s(\d+(?:\.\d+)?)", item)
        if func_match:
            return {
                "name": func_match.group(1),
                "lower_bound": float(func_match.group(2)),
                "upper_bound": float("inf"),
            }
        
        # 匹配 x1 to x2
        func_match = re.match(r"(\w+)\s+(\d+(?:\.\d+)?) to (\d+(?:\.\d+))", item)
        if func_match:
            return {
                "name": func_match.group(1),
                "lower_bound": float(func_match.group(2)),
                "upper_bound": float(func_match.group(3)),
            }

        # 匹配 below x1~x2
        func_match = re.match(r"(\w+)\sbelow\s(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)", item)
        if func_match:
            return {
                "name": func_match.group(1),
                "lower_bound": float(func_match.group(2)),
                "upper_bound": float(func_match.group(3)),
            }

        # 匹配 below x
        func_match = re.match(r"(\w+)\sbelow\s(\d+(?:\.\d+)?)", item)
        if func_match:
            return {
                "name": func_match.group(1),
                "lower_bound": float(func_match.group(2)),
                "upper_bound": float("inf"),
            }

    raise ValueError("无法解析项: {}".format(item))

# 函数标准化
def get_function_sampling(function_list,file_path,function_range_path):
    
    csv_df = pd.read_csv(function_range_path)
    function_limits = {}
    for _, row in csv_df.iterrows():
        function_limits[row['Function']] = {
            'min': row['Min Sampling Rate (%)'],
            'max': row['Max Sampling Rate (%)']
        }
    
    # 读取文件内容
    with open(file_path, 'r') as file:
        lines = file.readlines()
        
    normalized_rates = {}
    for line in lines[1:]:  # 跳过标题行
        parts = line.split()
        function_name = parts[0]
        
        if function_name in function_list:
            sampling_rate = float(parts[1])
            if function_name in function_limits:
                min_rate = function_limits[function_name]['min']
                max_rate = function_limits[function_name]['max']
                normalized_rate = (sampling_rate - min_rate) / (max_rate - min_rate)
                
                # 确保标准化值在 0 到 1 之间
                if normalized_rate < 0:
                    normalized_rate = 0
                elif normalized_rate > 1:
                    normalized_rate = 1
                
                normalized_rates[function_name] = normalized_rate
            else:
                print(f"Function {function_name} not found in CSV limits.")
    
    return normalized_rates

# 函数不标准化，直接提取函数值
def get_function_value(function_list,file_path):
    
    # 读取文件内容
    with open(file_path, 'r') as file:
        lines = file.readlines()
        
    value_rate = {}
    for line in lines[1:]:  # 跳过标题行
        parts = line.split()
        function_name = parts[0]
        
        if function_name in function_list:
            sampling_rate = float(parts[1])    
            value_rate[function_name] = sampling_rate
    
    return value_rate

# 运行负载，获取性能数据
def evaluate_configuration(env, knobs):
    # 运行一个配置并返回 tps
    print('Applying knobs: %s.' % (knobs))
    timeout, metrics, internal_metrics, resource,function_file = env.step_GP_data(knobs=knobs, collect_resource=True)
    return metrics, internal_metrics, resource, function_file

# 读取perf文件，匹配规则候选集
def read_config(config_file, perf_file):
    knobs = {}
    tps = None  # 设为 None 或一个默认值，比如 0
    with open(config_file, 'r') as f:
        config = json.load(f)["data"]
        
    for i, base_item in enumerate(config):
        base_knobs = base_item["configuration"]
        base_tps = base_item["external_metrics"]["tps"]
        function_file = os.path.basename(base_item["function_file"])
         
        # print(f"function_file: {function_file}, perf_file: {perf_file}")
        
        if function_file == perf_file:
            knob_names  = list(base_knobs.keys())
            knobs = {}
            for knob_name in knob_names:
                knobs[knob_name] = base_knobs[knob_name]
            tps = base_tps
        else:
            continue
    return knobs, tps


# def match_rule(env, default_file, rule,knobs,tps,file_path,function_range_path):
#     # 判断规则是否符合要求
#     if(rule == 0):
#         print("support or confidence less than 0.5")
#         return None
#     support = rule['support']
#     confidence = rule['confidence']
#     lift = rule['lift']
    
#     # 1. 获取规则中的函数，对perf文件中对应函数进行标准化处理
#     function_name_list = [func_dict['name'] for func_dict in rule['function']]
#     normalized_rates = get_function_sampling(function_name_list,file_path,function_range_path)
    
#     # 2. 判断标准化后的函数采样率是否在规则范围内
#     if not check_function_rates(normalized_rates, rule):
#         print("Function rates not in range.")
#         return None
#     # 3. 对存在于规则中的参数，进行标准化处理
#     knobs_name_list = [func_dict['name'] for func_dict in rule['knob']]
#     # updated_knobs = {}
#     print(f"Original knobs: {knobs}")
#     for knob in knobs_name_list:
#         for knob_info in rule['knob']:
#             knob = knob_info['name']
#             if knob in knobs:
#                 x = knob_normalize(default_file,knob, knobs[knob])
#                 # 4. 按照规则做出决策
#                 lower_bound = knob_info['lower_bound']
#                 upper_bound = knob_info['upper_bound']
#                 # print(f"knob: {knob},lower_bound: {lower_bound}, upper_bound: {upper_bound}")
#                 if(lower_bound == -float("inf")):
#                     ran_change = upper_bound
#                 elif(upper_bound == float("inf")):
#                     ran_change = lower_bound
#                 else :
#                     # 从lower_bound 和 upper_bound的范围内中取随机值
#                     # ran_change = np.random.uniform(lower_bound, upper_bound)
#                     ran_change = (lower_bound + upper_bound) / 2
#                 x = x + ran_change
#                 # 反标准化
#                 knobs[knob] = round(knob_denormalize(default_file,knob, x))       
#     print(f"Ajusted knobs: {knobs}")
#     # 5. 修改参数，执行程序，获取性能数据
#     metrics, internal_metrics, resource,flamegraph_name = evaluate_configuration(env, knobs)
#     if metrics is None:
#         return None
#     # 6. 比较性能是否按照规则中提升
#     s_A_B = support * rule["total_num"]
#     s_A = s_A_B / confidence
#     s_B = s_A_B / (lift * s_A)
#     rule["total_num"] += 1
#     change = (metrics[0] - tps) / tps * 100
#     if rule['tps']['lower_bound'] <= change <= rule['tps']['upper_bound']:
#         # 规则有效，修改support和confidence和lift
#         print("Rule matched.")
#         rule["support"] = round(s_A_B / rule["total_num"],2)
#         rule["confidence"] = round(s_A_B / (s_A + 1),2)
#         rule["lift"] = round(s_A_B / (s_B * (s_A + 1)),2)
#         print("11111: ",rule)
#     else:
#         # 规则无效，修改support和confidence和lift,保留2位小数
#         rule["support"] = round(s_A_B / rule["total_num"],2)
#         rule["confidence"] = round(s_A_B / (s_A + 1),2)
#         rule["lift"] = round(s_A_B / (s_B * (s_A + 1)),2)
#         print("Rule not matched.")
#         print("1111: " ,rule)
         
#     return rule

#  标准化版
# def match_rule(default_file,rule,knobs,file_path,function_range_path):
#     # 判断规则是否符合要求
#     if(rule == 0):
#         print("support or confidence less than 0.5")
#         return None
#     support = rule['support']
#     confidence = rule['confidence']
#     lift = rule['lift']
    
#     # 1. 获取规则中的函数，对perf文件中对应函数进行标准化处理
#     function_name_list = [func_dict['name'] for func_dict in rule['function']]
#     normalized_rates = get_function_sampling(function_name_list,file_path,function_range_path)
#     # print("219219normalized_rates:",normalized_rates)
    
#     # 2. 判断标准化后的函数采样率是否在规则范围内
#     if not check_function_rates(normalized_rates, rule):
#         print("Function rates not in range.")
#         return None
#     # 3. 对存在于规则中的参数，进行标准化处理
#     knobs_name_list = [func_dict['name'] for func_dict in rule['knob']]
#     updated_knobs = {}
#     # print(f"Original knobs: {knobs}")
#     for knob in knobs_name_list:
#         for knob_info in rule['knob']:
#             knob = knob_info['name']
#             if knob in knobs:
#                 x = knob_normalize(default_file,knob, knobs[knob])
#                 # 4. 按照规则做出决策
#                 lower_bound = knob_info['lower_bound']
#                 upper_bound = knob_info['upper_bound']
#                 # print(f"knob: {knob},lower_bound: {lower_bound}, upper_bound: {upper_bound}")
#                 if(lower_bound == -float("inf")):
#                     ran_change = upper_bound
#                 elif(upper_bound == float("inf")):
#                     ran_change = lower_bound
#                 else :
#                     # 从lower_bound 和 upper_bound的范围内中取随机值
#                     # ran_change = np.random.uniform(lower_bound, upper_bound)
#                     ran_change = (lower_bound + upper_bound) / 2
#                 x = x + ran_change
#                 # 反标准化
#                 updated_knobs[knob] = round(knob_denormalize(default_file,knob, x))       
#     # print(f"Ajusted knobs: {knobs}")
#     return updated_knobs

#  不用标准化版
def match_rule(default_file,rule,knobs,file_path):
    # 判断规则是否符合要求
    if(rule == 0):
        print("support or confidence less than 0.5")
        return None
    support = rule['support']
    confidence = rule['confidence']
    lift = rule['lift']
    
    # 1. 获取规则中的函数，对perf文件中对应函数
    function_name_list = [func_dict['name'] for func_dict in rule['function']]
    # normalized_rates = get_function_sampling(function_name_list,file_path,function_range_path)
    sampling_rates = get_function_value(function_name_list,file_path)
    
    # 2. 判断标准化后的函数采样率是否在规则范围内
    if not check_function_rates(sampling_rates, rule):
        print("Function rates not in range.")
        return None
    # 3. 对存在于规则中的参数，进行标准化处理
    knobs_name_list = [func_dict['name'] for func_dict in rule['knob']]
    updated_knobs = {}
    # print(f"Original knobs: {knobs}")
    for knob in knobs_name_list:
        for knob_info in rule['knob']:
            knob = knob_info['name']
            if knob in knobs:
                x = knob_normalize(default_file,knob, knobs[knob])
                # 4. 按照规则做出决策
                lower_bound = knob_info['lower_bound']
                upper_bound = knob_info['upper_bound']
                # print(f"knob: {knob},lower_bound: {lower_bound}, upper_bound: {upper_bound}")
                if(lower_bound == -float("inf")):
                    # ran_change = upper_bound - 0.5
                    # ran_change = np.random.uniform(-1,upper_bound)
                    if upper_bound - 0.5 > -1:
                        ran_change = upper_bound - 0.5
                    else:
                        ran_change = upper_bound
                elif(upper_bound == float("inf")):
                    if(lower_bound + 0.5 < 1):
                        ran_change = lower_bound + 0.5
                    else:
                        ran_change = lower_bound
                    # ran_change = np.random.uniform(lower_bound, 1)
                else :
                    # 从lower_bound 和 upper_bound的范围内中取随机值
                    # ran_change = np.random.uniform(lower_bound, upper_bound)
                    ran_change = (lower_bound + upper_bound) / 2
                # print("change333: ",ran_change)
                x = x + ran_change
                # 反标准化
                updated_knobs[knob] = round(knob_denormalize(default_file,knob, x))       
    # print(f"Ajusted knobs: {knobs}")
    return updated_knobs


def read_txt_files(folder_path):
    """
    读取文件夹中的所有 txt 文件
    """
    txt_files = []
    for filename in os.listdir(folder_path):
        if filename.endswith(".txt"):
            txt_files.append(os.path.join(folder_path, filename))
    return txt_files   
  
# 候选集读取规则
def read_rules_from_file(file_path):
    with open(file_path, 'r') as file:
        rules = file.readlines()
    return [rule.strip() for rule in rules]
      
# def update_rule_file(rules, res_rules, output_file):
#     updated_rules = []
#     for rule, res_rule in zip(rules, res_rules):
#         support = res_rule['support']
#         confidence = res_rule['confidence']
#         lift = res_rule['lift']

#         # 匹配规则中的括号部分
#         pattern = r'\(支持度: [\d.]+, 置信度: [\d.]+, 提升度: [\d.]+\)'
#         if re.search(pattern, rule):
#             # 如果规则中已经有括号部分，更新括号内的内容
#             new_rule = re.sub(pattern, f'(支持度: {support:.2f}, 置信度: {confidence:.2f}, 提升度: {lift:.2f})', rule)
#         else:
#             # 如果规则中没有括号部分，添加括号和内容
#             new_rule = f'{rule}(支持度: {support:.2f}, 置信度: {confidence:.2f}, 提升度: {lift:.2f})'

#         updated_rules.append(new_rule)

#     # 将更新后的规则写入文件
#     with open(output_file, 'w', encoding='utf-8') as f:
#         for rule in updated_rules:
#             f.write(rule + '\n')

# def update_rule_file(rules, res_rules, output_file):
#     updated_rules = []
#     for rule, res_rule in zip(rules, res_rules):
#         support = res_rule['support']
#         confidence = res_rule['confidence']
#         lift = res_rule['lift']
#         total_num = res_rule['total_num']  # 新增：获取数据总数

#         # 匹配规则中的括号部分，同时考虑数据总数
#         pattern = r'\(支持度: [\d.]+, 置信度: [\d.]+, 提升度: [\d.]+, 数据总数: [\d.]+\)'
#         if re.search(pattern, rule):
#             # 如果规则中已经有括号部分，更新括号内的内容
#             new_rule = re.sub(pattern, f'(支持度: {support:.2f}, 置信度: {confidence:.2f}, 提升度: {lift:.2f}, 数据总数: {total_num:.0f})', rule)
#         else:
#             # 如果规则中没有括号部分，添加括号和内容
#             new_rule = f'{rule}(支持度: {support:.2f}, 置信度: {confidence:.2f}, 提升度: {lift:.2f}, 数据总数: {total_num:.0f})'

#         updated_rules.append(new_rule)

#     # 将更新后的规则写入文件
#     with open(output_file, 'w', encoding='utf-8') as f:
#         for rule in updated_rules:
#             f.write(rule + '\n')
def update_rule_file(rules, res_rules, output_file):
    updated_rules = []
    print(len(rules))
    print(len(res_rules))
    print("xxxxxxxxxxxxxxx")
    # 遍历所有 rules
    for rule in rules:
        origin_pro_rule = process_rule_catagory(rule)
        for res_rule in res_rules:
            support = res_rule['support']
            confidence = res_rule['confidence']
            lift = res_rule['lift']
            total_num = res_rule['total_num'] 
            if is_matching(res_rule,origin_pro_rule):
                # 匹配到后，更新括号内的内容
                pattern = r'\(支持度: [\d.]+, 置信度: [\d.]+, 提升度: [\d.]+, 数据总数: [\d.]+\)'
                if re.search(pattern, rule):
                    # 如果规则中已经有括号部分，更新括号内的内容
                    new_rule = re.sub(pattern, f'(支持度: {support:.2f}, 置信度: {confidence:.2f}, 提升度: {lift:.2f}, 数据总数: {total_num:.0f})', rule)
                else:
                    # 如果规则中没有括号部分，添加括号和内容
                    new_rule = f'{rule}(支持度: {support:.2f}, 置信度: {confidence:.2f}, 提升度: {lift:.2f}, 数据总数: {total_num:.0f})'

                # 将更新后的规则添加到更新后的列表中
                updated_rules.append(new_rule)
                break  # 找到匹配的规则后，跳出当前循环，避免重复添加
        else:
            # 如果 res_rule 在 rules 中找不到匹配，保持原样
            updated_rules.append(rule)

    # 将更新后的规则写入文件
    with open(output_file, 'w', encoding='utf-8') as f:
        for rule in updated_rules:
            f.write(rule + '\n')


def is_matching(data1, data2):
    # 比较 tps 字段
    perf1 = data1['performance']
    perf2 = data2['performance']
    perf_match = (perf1['lower_bound'] == perf2['lower_bound']) and (perf1['upper_bound'] == perf2['upper_bound'])

    # 比较 function 字段
    functions1 = data1['function']
    functions2 = data2['function']
    if len(functions1) != len(functions2):
        function_match = False
    else:
        # 对 function 列表进行排序，确保顺序不影响比较结果
        sorted_functions1 = sorted(functions1, key=lambda x: (x['name'], x['lower_bound'], x['upper_bound']))
        sorted_functions2 = sorted(functions2, key=lambda x: (x['name'], x['lower_bound'], x['upper_bound']))
        function_match = all(
            func1['name'] == func2['name'] and
            func1['lower_bound'] == func2['lower_bound'] and
            (math.isinf(func1['upper_bound']) and math.isinf(func2['upper_bound']) or func1['upper_bound'] == func2['upper_bound'])
            for func1, func2 in zip(sorted_functions1, sorted_functions2)
        )

    # 比较 knob 字段
    knobs1 = data1['knob']
    knobs2 = data2['knob']
    if len(knobs1) != len(knobs2):
        knob_match = False
    else:
        # 对 knob 列表进行排序，确保顺序不影响比较结果
        sorted_knobs1 = sorted(knobs1, key=lambda x: (x['name'], x['lower_bound'], x['upper_bound']))
        sorted_knobs2 = sorted(knobs2, key=lambda x: (x['name'], x['lower_bound'], x['upper_bound']))
        knob_match = all(
            knob1['name'] == knob2['name'] and
            knob1['lower_bound'] == knob2['lower_bound'] and
            knob1['upper_bound'] == knob2['upper_bound']
            for knob1, knob2 in zip(sorted_knobs1, sorted_knobs2)
        )

    # 综合判断
    return perf_match and function_match and knob_match

# def searchRule(defaultknob_file,rules,knobs,perf_file,function_range_path):
#     # for rule in rules:
#     # print("searchrule ing...")
#     processed_rule = process_rule_catagory(rules)
#     update_knob = match_rule(defaultknob_file,processed_rule,knobs,perf_file,function_range_path)
#     if update_knob is not None:
#         return update_knob, processed_rule
#     else:
#         return [],{}
# 不标准化版  
def searchRule(defaultknob_file,rules,knobs,perf_file):
    # for rule in rules:
    # print("searchrule ing...")
    processed_rule = process_rule_catagory(rules)
    # update_knob = match_rule(defaultknob_file,processed_rule,knobs,perf_file,function_range_path)
    update_knob = match_rule(defaultknob_file,processed_rule,knobs,perf_file)
    if update_knob is not None:
        return update_knob, processed_rule
    else:
        return [],{}
    

def updateMetric_useless(rule):
    # 规则无效
    support = rule['support']
    confidence = rule['confidence']
    lift = rule['lift']
    s_A_B = support * rule["total_num"]
    s_A = s_A_B / confidence
    s_B = s_A_B / (lift * s_A)
    rule["total_num"] += 1
    rule["support"] = round(s_A_B / rule["total_num"],2)
    rule["confidence"] = round(s_A_B / (s_A + 1),2)
    rule["lift"] = round(s_A_B / (s_B * (s_A + 1)),2)
    return rule

def updateMetric_useful(rule):
    # 规则有效
    support = rule['support']
    confidence = rule['confidence']
    lift = rule['lift']
    s_A_B = support * rule["total_num"]
    s_A = s_A_B / confidence
    s_B = s_A_B / (lift * s_A)
    rule["total_num"] += 1
    rule["support"] = round(s_A_B / rule["total_num"],2)
    rule["confidence"] = round((s_A_B + 1) / (s_A + 1),2)
    rule["lift"] = round(s_A_B / ((s_B+1) * (s_A + 1)),2)
    return rule
    



if __name__ == '__main__':
    # 解析命令行参数
    # parser = argparse.ArgumentParser()
    # parser.add_argument('--config', type=str, default='/root/AI4DB/DBTune/scripts/config_test.ini', help='config file')
    # parser.add_argument('--knobs_file', type=str,default='/root/AI4DB/DBTune/scripts/knob_config/config10.json', help='JSON string of knobs and their values')
    # opt = parser.parse_args()
    
    #  # 解析配置文件
    # args_db, args_tune = parse_args(opt.config)

    # if args_db['db'] == 'mysql':
    #     db = MysqlDB(args_db)
    
    # # 创建数据库环境
    # env = DBEnv(args_db, args_tune, db)
    
    
    # 历史数据文件
    folder_path = "/root/AI4DB/DBTune/scripts/perf_data_test"
    # 规则候选集文件
    rule_file = "/root/sysinsight-main/HisRule/com10_update_rule_26_4.txt"
    # 配置评估文件
    evaluation_file = "/root/AI4DB/DBTune/scripts/DBTune_history/history_performance_test.json"
    # 函数标准化范围文件
    function_range_path = "/root/sysinsight-main/HisRule/com10_update_rule_26_4.txt"
    # 读取规则候选集
    rules = read_rules_from_file(rule_file)
    # TODO：给原始规则候选集增加有效度
    # new_rule_file = "/root/AI4DB/Experiment/FuzzyRule/offline/utils/filtered_rules_1_a.txt"
    # processed_rules = add_rule_validity(rules)
    # save_rules(new_rule_file, processed_rules)
    
    # for i, filename in enumerate(os.listdir(folder_path)):
    #     if filename.endswith(".txt"):
    #         # 跳过第一个文件
    #         if i == 0:
    #             continue
    #         perf_file = os.path.join(folder_path,filename)
    #         perf_file_name = os.path.basename(perf_file)
    #         print(f"Processing {perf_file_name}...")
    #         # 读取perf文件，匹配规则候选集
    #         knobs, tps = read_config(evaluation_file, perf_file_name)
    #         updated_rules = []
    #         for rule in rules:
    #             processed_rule = process_rule_catagory(rule)
    #             res_rule = match_rule(opt.knobs_file,processed_rule,knobs,perf_file,function_range_path)
    #             if res_rule is not None:
    #                 updated_rules.append(res_rule)
            
            # output_file = 'updated_rules.txt'
            # update_rule_file(rules, updated_rules, output_file)
            # print(f"规则文件已更新：{output_file}")
    # for rule in rules:
    #     processed_rule = process_rule_catagory(rule)
    #     print(processed_rule)
    
    # data1 = {'tps': {'lower_bound': 0.0, 'upper_bound': 20.0}, 'function': [], 'knob': [{'name': 'innodb_spin_wait_delay', 'lower_bound': -0.4, 'upper_bound': -0.04}], 'support': 0.36, 'confidence': 1.0, 'lift': 0.74, 'total_num': 4951}
    # data2 = "knob innodb_spin_wait_delay down 0.04~0.4 => tps improve 0~20(支持度: 0.36, 置信度: 1.00, 提升度: 2.81, 数据总数: 4950)"
    # pro = process_rule_catagory(data2)
    # result = is_matching(data1, pro)
    # print(result)
        

    
    
        
    