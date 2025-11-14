import argparse
import json
import math
import os
import random
import re
# from DBTuner.config import parse_args
# from DBTuner.dbenv import DBEnv
# from DBTuner.database.mysqldb import MysqlDB
import numpy as np
import pandas as pd
import time

# 规则挖掘数据总数
TOTAL_NUM = 2080
DEFAULT_KNOB_VALUES_PATH = "/root/sysinsight-main/DBTuner/knobspace/gptuner_target_knobs.json"  

MEMORY_KNOBS = [
    'tmp_table_size', 'max_heap_table_size', 'query_prealloc_size',
    'sort_buffer_size', 'innodb_buffer_pool_size', 
    'innodb_online_alter_log_max_size', 'join_buffer_size',
    'table_open_cache', 'thread_cache_size', 
    'range_optimizer_max_mem_size', 'stored_program_definition_cache',
    'tablespace_definition_cache', 'temptable_max_ram',
    'key_cache_block_size', 'max_relay_log_size'
]

# === 读取主机总内存（KB）并缓存 ===
_total_mem_kb_cache = None
def get_total_mem_kb():
    global _total_mem_kb_cache
    if _total_mem_kb_cache is not None:
        return _total_mem_kb_cache
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    # MemTotal:  65843016 kB
                    _total_mem_kb_cache = int(parts[1])
                    return _total_mem_kb_cache
    except Exception:
        pass
    try:
        out = subprocess.check_output(["free", "-k"]).decode()
        # parse second line
        for line in out.splitlines():
            if line.lower().startswith("mem:"):
                cols = line.split()
                _total_mem_kb_cache = int(cols[1])
                return _total_mem_kb_cache
    except Exception:
        pass

    raise RuntimeError("无法读取主机总内存，请确保在 Linux 环境下运行或手动提供总内存。")

# === 把字符串或数字解析为 KB（启发式） ===
def parse_size_to_kb(val, knob_info=None):
    """
    将 MySQL 参数值统一转换为 KB（千字节）
    假设：
      - 所有数值均为字节（Bytes）
      - 不再依赖 knob_info["unit"]
    支持输入：
      - 数字类型（int / float）
      - 字符串带单位（如 "512M", "2G", "128K"）
      - 字符串数字（如 "1048576"）
    返回：
      - 转换后的 KB 整数
      - 解析失败时返回 None
    """
    if val is None:
        return None

    # --- 数值类型 ---
    if isinstance(val, (int, float, np.number)):
        # 默认全部视为字节
        return int(float(val) / 1024)

    # --- 字符串类型 ---
    if isinstance(val, str):
        s = val.strip().lower()
        # 匹配带单位或纯数字
        m = re.match(r"^([0-9\.]+)\s*([kmgtp]?b?)?$", s)
        if not m:
            # 去掉逗号后再匹配
            s2 = s.replace(",", "")
            m = re.match(r"^([0-9\.]+)\s*([kmgtp]?b?)?$", s2)
            if not m:
                return None

        num = float(m.group(1))
        unit = (m.group(2) or "").lower()

        # --- 按常见单位转换为 KB ---
        if unit in ("b", "byte", "bytes", ""):
            return int(num / 1024)
        elif unit in ("k", "kb", "kib"):
            return int(num)
        elif unit in ("m", "mb", "mib"):
            return int(num * 1024)
        elif unit in ("g", "gb", "gib"):
            return int(num * 1024 * 1024)
        elif unit in ("t", "tb", "tib"):
            return int(num * 1024 * 1024 * 1024)
        else:
            return None

    return None

# === 将单个 knob 的原值转换为占总内存的百分比（0-100） ===
def knob_value_to_mem_percent(knob_name, raw_value):
    """
    如果能把 raw_value 解析为 KB，则返回 (raw_kb / total_mem_kb) * 100
    否则返回 None（表示不能以内存百分比表示）
    """
    # 加载对应参数的配置文件
    with open(DEFAULT_KNOB_VALUES_PATH, "r") as file:
        data = json.load(file)

    if knob_name not in data:
        # 如果配置中没有该 knob，直接返回 None
        return None

    knob_info = data[knob_name]
    
    total_kb = get_total_mem_kb()
    # print("total_kb:",total_kb)
    kb = parse_size_to_kb(raw_value, knob_info)
    if kb is None:
        return None
    pct = (kb / total_kb) * 100.0
    return pct

def mem_percent_to_knob_value(knob_name, mem_percent):
    """
    将内存百分比转换回原始的 knob 值（字节单位）
    
    参数:
    - knob_name: 参数名
    - mem_percent: 内存百分比 (0-100)
    
    返回:
    - 转换后的 knob 值（字节单位的整数）
    """
    # 获取总内存
    total_kb = get_total_mem_kb()
    
    # 计算所需的内存大小 (KB)
    required_kb = (mem_percent / 100.0) * total_kb
    
    # 将 KB 转换为字节
    required_bytes = int(required_kb * 1024)
    
    return required_bytes

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

def process_rule_catagory(rule):
    processed_rule = {}
    rule_parts = rule.split("=>")
    if len(rule_parts) != 2:
        raise ValueError("规则格式错误，无法解析: {}".format(rule))

    # 解析规则左侧部分（前件，包括参数和函数）
    left_part = rule_parts[0].strip()
    knobs, functions = [], []

    # 新的正则表达式模式，匹配新的规则格式
    # 匹配 knob 条件：参数名 + 方向 + 模式 + 范围
    # 示例: "innodb_spin_wait_delay down change 0.00~0.47"
    knob_pattern = re.compile(r"(\w+)\s+(up|down)\s+(change|percentage|end)\s+([><~\.\d]+)")
    
    # 匹配 function 条件：函数名 + above/below + 阈值
    # 示例: "do_command above 85.79"
    function_pattern = re.compile(r"(\w+)\s+(above|below)\s+([\d\.]+)")

    # 解析 knob 条件
    for match in knob_pattern.finditer(left_part):
        knob_name = match.group(1)
        direction = match.group(2)
        mode = match.group(3)
        range_str = match.group(4)
        
        knob_info = parse_knob_or_function(f"{knob_name} {direction} {mode} {range_str}", "knob")
        knobs.append(knob_info)

    # 解析 function 条件
    for match in function_pattern.finditer(left_part):
        func_name = match.group(1)
        comparison = match.group(2)  # above 或 below
        threshold = float(match.group(3))
        
        func_info = parse_knob_or_function(f"{func_name} {comparison} {threshold}", "function")
        functions.append(func_info)

    # 解析规则右侧部分（后件，包括 TPS 和统计信息）
    right_part = rule_parts[1].strip()
    
    # 解析统计信息
    support_confidence_lift_pattern = re.compile(r"支持度:\s*(\d+\.\d+),\s*置信度:\s*(\d+\.\d+),\s*提升度:\s*(\d+\.\d+),\s*数据总数:\s*(\d+)")
    match = support_confidence_lift_pattern.search(right_part)
    if match:
        support = float(match.group(1))
        confidence = float(match.group(2))
        lift = float(match.group(3))
        total_num = int(match.group(4))
    
    # 解析 TPS 性能指标
    performance = None
    tps_pattern = re.compile(r"tps improve\s+(\d+(?:\.\d+)?)(?:~(\d+(?:\.\d+)?))?\s*%?")
    tps_match = tps_pattern.search(right_part)
    
    if tps_match:
        lower_bound = float(tps_match.group(1))
        upper_bound = float(tps_match.group(2)) if tps_match.group(2) else lower_bound
        
        performance = {
            "type": "tps",
            "lower_bound": lower_bound,
            "upper_bound": 1000,
            "unit": "percentage"
        }

    # 如果没有找到性能指标，尝试其他模式
    if not performance:
        # 可以在这里添加其他性能指标（如延迟）的解析
        raise ValueError("未找到有效的性能指标信息")

    # 整合结果
    processed_rule["function"] = functions
    processed_rule["knob"] = knobs
    processed_rule["performance"] = performance
    processed_rule["support"] = support
    processed_rule["confidence"] = confidence
    processed_rule["lift"] = lift
    processed_rule["total_num"] = total_num
    
    return processed_rule

def parse_knob_or_function(item, item_type):
    if item_type == "knob":
        # 解析新的 knob 格式: "innodb_spin_wait_delay down change 0.00~0.47"
        # 或者: "innodb_spin_wait_delay down 16.19~97.15 percentage"
        # 或者: "innodb_thread_concurrency up to end 0.00~0.12"
        
        # 匹配模式: 参数名 + 方向 + 模式 + 范围
        knob_match = re.match(r"(\w+)\s+(up|down)\s+(change|percentage|end)\s+([><~\.\d]+)", item)
        if knob_match:
            name = knob_match.group(1)
            direction = knob_match.group(2)
            mode = knob_match.group(3)
            range_str = knob_match.group(4)
            
            # 解析范围
            if "~" in range_str:
                # 范围格式: "0.00~0.47"
                lower, upper = range_str.split("~")
                lower_bound = float(lower)
                upper_bound = float(upper)
            elif ">" in range_str:
                # 大于格式: ">0.47"
                lower_bound = float(range_str.replace(">", ""))
                upper_bound = float("inf")
            elif "<" in range_str:
                # 小于格式: "<0.47"
                lower_bound = -float("inf")
                upper_bound = float(range_str.replace("<", ""))
            else:
                # 单个值
                lower_bound = float(range_str)
                upper_bound = float(range_str)
            
            # 根据方向调整边界
            if direction == "down":
                lower_bound, upper_bound = -upper_bound, -lower_bound
            
            return {
                "name": name,
                "direction": direction,
                "mode": mode,
                "range": range_str,
                "lower_bound": lower_bound,
                "upper_bound": upper_bound
            }

    elif item_type == "function":
        # 解析新的 function 格式: "do_command above 85.79"
        # 匹配模式: 函数名 + above/below + 阈值
        func_match = re.match(r"(\w+)\s+(above|below)\s+([\d\.]+)", item)
        if func_match:
            name = func_match.group(1)
            comparison = func_match.group(2)
            threshold = float(func_match.group(3))
            
            if comparison == "above":
                return {
                    "name": name,
                    "lower_bound": threshold,
                    "upper_bound": float("inf")
                }
            elif comparison == "below":
                return {
                    "name": name,
                    "lower_bound": -float("inf"),
                    "upper_bound": threshold
                }

    raise ValueError("无法解析项: {}".format(item))

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
        # config = json.load(f)["data"]
        config = json.load(f)
        
    for i, base_item in enumerate(config):
        base_knobs = base_item["configuration"]
        base_tps = base_item["external_metrics"]["tps"]
        function_file = os.path.basename(base_item["function_file"])
         
        print(f"function_file: {function_file}, perf_file: {perf_file}")
        
        if function_file == perf_file:
            knob_names  = list(base_knobs.keys())
            knobs = {}
            for knob_name in knob_names:
                knobs[knob_name] = base_knobs[knob_name]
            tps = base_tps
        else:
            continue
    return knobs, tps


#  不用标准化版
def match_rule(default_file, rule, knobs, file_path):
    # 判断规则是否符合要求
    if rule == 0:
        print("support or confidence less than 0.5")
        return None
    support = rule['support']
    confidence = rule['confidence']
    lift = rule['lift']
    
    # 1. 获取规则中的函数，对perf文件中对应函数
    function_name_list = [func_dict['name'] for func_dict in rule['function']]
    sampling_rates = get_function_value(function_name_list, file_path)
    
    # 2. 判断函数采样率是否在规则范围内
    if not check_function_rates(sampling_rates, rule):
        print("Function rates not in range.")
        return None
    
    # 3. 对存在于规则中的参数，进行标准化处理
    knobs_name_list = [knob_dict['name'] for knob_dict in rule['knob']]
    updated_knobs = {}
    
    for knob_info in rule['knob']:
        knob_name = knob_info['name']
        # print(knob_info)
        if knob_name in knobs:
            # 获取当前参数值
            current_value = knobs[knob_name]
            
            # 根据规则模式采取不同的行动
            mode = knob_info.get('mode', 'change')  # 默认为change模式
            direction = knob_info.get('direction', 'up')  # 默认为up方向
            
            if mode == "change":
                print("------change---------------")
                # change模式：表示变化的绝对值
                # 内存参数--百分比转换；其他参数标准化
                if knob_name in MEMORY_KNOBS:
                    x = knob_value_to_mem_percent(knob_name, current_value)
                else:
                    x = knob_normalize(default_file, knob_name, current_value)
                
                # 获取变化范围
                lower_bound = knob_info['lower_bound']
                upper_bound = knob_info['upper_bound']
                
                # 计算变化量
                if direction == "down":
                    # 对于down方向，变化量应该是负的
                    if lower_bound == -float("inf"):
                        # 无下限，变化量取负的最大值
                        if upper_bound - 0.5 > -1:
                            ran_change = (upper_bound - 0.5)
                        else:
                            ran_change = upper_bound
                    elif upper_bound == float("inf"):
                        # 无上限，变化量取负的最小值
                        if lower_bound + 0.5 < 1:
                            ran_change = (lower_bound + 0.5)
                        else:
                            ran_change = lower_bound
                    else:
                        # 有上下限，变化量取负的中间值
                        ran_change = (lower_bound + upper_bound) / 2
                        print(ran_change)
                else:  # up方向
                    if lower_bound == -float("inf"):
                        if upper_bound - 0.5 > 0:
                            ran_change = upper_bound - 0.5
                        else:
                            ran_change = upper_bound
                    elif upper_bound == float("inf"):
                        if lower_bound + 0.5 < 1:
                            ran_change = lower_bound + 0.5
                        else:
                            ran_change = lower_bound
                    else:
                        ran_change = (lower_bound + upper_bound) / 2
                
                # 应用变化
                x = x + ran_change
                
                # 反标准化
                if knob_name in MEMORY_KNOBS:
                    updated_knobs[knob_name] = mem_percent_to_knob_value(knob_name, x)
                else:
                    updated_knobs[knob_name] = round(knob_denormalize(default_file, knob_name, x))
                    print("updated_knobs1:",updated_knobs)
            
            elif mode == "percentage":
                print("------percentage---------------")
                # percentage模式：表示变化的百分比
                if knob_name in MEMORY_KNOBS:
                    x = knob_value_to_mem_percent(knob_name, current_value)
                else:
                    x = knob_normalize(default_file, knob_name, current_value)
                
                # 获取百分比变化范围
                lower_bound = knob_info['lower_bound']
                upper_bound = knob_info['upper_bound']
            
                # 计算百分比变化量
                if lower_bound == -float("inf"):
                    # 无下限，取上限减去一个偏移量
                    if upper_bound - 50 > -100:  # 百分比不能低于-100%
                        percentage_change = upper_bound - 50
                    else:
                        percentage_change = upper_bound
                elif upper_bound == float("inf"):
                    # 无上限，取下限加上一个偏移量
                    if lower_bound + 50 < 100:  # 百分比不能超过100%
                        percentage_change = lower_bound + 50
                    else:
                        percentage_change = lower_bound
                else:
                    # 有上下限，取中间值
                    percentage_change = (lower_bound + upper_bound) / 2
                
                # 应用百分比变化
                if direction == "up":
                    new_value = x * (1 + percentage_change / 100)
                else:  # down
                    new_value = x * (1 - percentage_change / 100)
                
                # 直接使用计算后的值
                # 反标准化
                if knob_name in MEMORY_KNOBS:
                    updated_knobs[knob_name] = mem_percent_to_knob_value(knob_name, new_value)
                else:
                    updated_knobs[knob_name] = round(knob_denormalize(default_file, knob_name, new_value))
                print("updated_knobs2:",updated_knobs)
            
            elif mode == "end":
                print("------end---------------")
                # end模式：表示变化后的终值
                # 获取终值范围
                if knob_name in MEMORY_KNOBS:
                    x = knob_value_to_mem_percent(knob_name, current_value)
                else:
                    x = knob_normalize(default_file, knob_name, current_value)
                    
                lower_bound = knob_info['lower_bound']
                upper_bound = knob_info['upper_bound']
                
                # 计算终值
                if lower_bound == -float("inf"):
                    # 无下限，取上限减去一个偏移量
                    end_value = upper_bound - 0.5
                elif upper_bound == float("inf"):
                    # 无上限，取下限加上一个偏移量
                    end_value = lower_bound + 0.5
                else:
                    # 有上下限，取中间值
                    end_value = (lower_bound + upper_bound) / 2
                    
                if knob_name in MEMORY_KNOBS:
                    updated_knobs[knob_name] = mem_percent_to_knob_value(knob_name, end_value)
                else:
                    updated_knobs[knob_name] = round(knob_denormalize(default_file, knob_name, end_value))

    # print("updated_knobs:",updated_knobs)
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
      
def update_rule_file(rules, res_rules, output_file):
    updated_rules = []
    print(len(rules))
    print(len(res_rules))
    print("xxxxxxxxxxxxxxx")
    print(res_rules)
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

# 不标准化版  
def searchRule(defaultknob_file,rules,knobs,perf_file):
    # print("searchrule ing...")
    processed_rule = process_rule_catagory(rules)
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
    folder_path = "/root/sysinsight-main/perf_data"
    # 规则候选集文件
    rule_file = "/root/sysinsight-main/DBTuner/utils/mysql_sysbench_update_rule_revision.txt"
    # 配置评估文件
    evaluation_file = "/root/sysinsight-main/rule_collect_results_sysbench_test.json"
    # 函数标准化范围文件
    function_range_path = "/root/RUC/DBTune/scripts/revision_expe/average_gptuner_mysql_sysbench_smac_lhs_collect.csv"
    # 读取规则候选集
    rules = read_rules_from_file(rule_file)
    
    # sysbench_files = [f for f in os.listdir(folder_path) if f.endswith("counts_sysbench.txt")]
    # sysbench_files.sort()
    # for i, filename in enumerate(os.listdir(folder_path)):
    #     if filename.endswith(".txt"):
    #         # 跳过第一个文件
    #         if i == 0:
    #             continue
    #         if "btFunctions" in filename:
    #             continue
    #         perf_file = os.path.join(folder_path,filename)
    #         perf_file_name = os.path.basename(perf_file)
    #         print(f"Processing {perf_file_name}...")
    #         # 读取perf文件，匹配规则候选集
    #         knobs, tps = read_config(evaluation_file, perf_file_name)
    #         updated_rules = []
    #         for rule in rules:
    #             processed_rule = process_rule_catagory(rule)
    #             # print(processed_rule)
    #             update_knob = match_rule(DEFAULT_KNOB_VALUES_PATH,processed_rule,knobs,perf_file)
    #             if update_knob is not None:
    #                 updated_rules.append(processed_rule)
            
    #         output_file = 'updated_rules_test.txt'
    #         update_rule_file(rules, updated_rules, output_file)
    #         print(f"规则文件已更新：{output_file}")

    # for rule in rules:
    #     processed_rule = process_rule_catagory(rule)
    #     print(processed_rule)
    
    # data1 = {'tps': {'lower_bound': 0.0, 'upper_bound': 20.0}, 'function': [], 'knob': [{'name': 'innodb_spin_wait_delay', 'lower_bound': -0.4, 'upper_bound': -0.04}], 'support': 0.36, 'confidence': 1.0, 'lift': 0.74, 'total_num': 4951}
    # data2 = "knob innodb_spin_wait_delay down 0.04~0.4 => tps improve 0~20(支持度: 0.36, 置信度: 1.00, 提升度: 2.81, 数据总数: 4950)"
    # pro = process_rule_catagory(data2)
    # result = is_matching(data1, pro)
    # print(result)
        

    
    
        
    