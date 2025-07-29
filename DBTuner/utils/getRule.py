#  检索规则

import re
import csv
from collections import defaultdict


def get_rules(file_path, params_to_match):
    matched_rules = []

    # 读取csv文件
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter=',')
        for row in reader:
            # 将整个行合并为一个字符串
            rule = ",".join(row).strip()
            
            # 如果规则中包含任何一个参数名
            if any(param in rule for param in params_to_match):
                # 将行以逗号分隔的格式重新加入匹配结果
                matched_rules.append(rule)
    
    return matched_rules

def parse_rule(rule_str):
    knob_part = re.search(r'knob (\w+) (up|down)', rule_str)
    if not knob_part:
        return None
    knob_name = knob_part.group(1)
    adjust_action = knob_part.group(2)
    support = re.search(r'支持度: (\d+\.\d+)', rule_str)
    support = float(support.group(1)) if support else 0.0
    return{
        'knob_name': knob_name,
        'adjust_action': adjust_action,
        'support':support,
        'rule_str':rule_str
    }
    

def group_rules_by_knob(matched_rules):
    """
    按 knob 参数和调整方向（up/down）分组，返回每组内支持度最高的规则
    """
    # 解析规则，提取 knob 名称、调整方向和支持度
    parsed_rules = []
    for r in matched_rules:
        parsed = parse_rule(r)
        if parsed:
            parsed_rules.append(parsed)
    # 按 knob 参数和调整方向分组
    grouped_rules = defaultdict(list)
    for rule in parsed_rules:
        key = (rule["knob_name"], rule["adjust_action"])
        grouped_rules[key].append(rule)
    
    # 每组内按支持度降序排序，取最高支持度的规则
    selected_rules = []
    for group_key, rules in grouped_rules.items():
        # 按支持度排序
        sorted_rules = sorted(rules, key=lambda x: x["support"], reverse=True)
        # 取第一条（支持度最高）
        best_rule = sorted_rules[0]["rule_str"]
        selected_rules.append(best_rule)
    
    return selected_rules

# # # 给定规则文件路径和参数列表
# file_path = '/root/sysinsight-main/HisRule/com10_update_rule_80_2.txt'  # 请替换为你的文件路径
# params_to_match = ['ut_delay']  # 示例参数列表

# # 获取匹配的规则
# matched_rules = get_rules(file_path, params_to_match)
# # print(matched_rules)

# # # 输出匹配的规则
# # for rule in matched_rules:
# #     # print(",".join(rule))
# #     print(rule)
    
# selected_rules = group_rules_by_knob(matched_rules)
# print(selected_rules)
# for rule_str in selected_rules:
#     print(rule_str)