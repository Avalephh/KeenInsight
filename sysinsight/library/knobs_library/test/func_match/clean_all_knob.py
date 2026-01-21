import json
from collections import defaultdict

# 读取JSON文件
def read_json(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

# 读取txt文件并提取变量名
def read_txt(file_path):
    variables = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            variable = line.split()[0]  # 提取变量名
            variables.append(variable)
    return variables

# 提取missing和remain数据
def extract_missing_and_remain(data, mysql_vars):
    missing_data = []
    remain_data = []
    
    if isinstance(data, list):
        for item in data:
            knob_name = item.get('knob_name')
            if knob_name in mysql_vars:
                remain_data.append(item)
            else:
                missing_data.append(item)
    
    return missing_data, remain_data

# 统计knob_name出现次数
def count_knob_names(data):
    knob_count = defaultdict(int)
    for item in data:
        knob_name = item.get('knob_name')
        if knob_name:
            knob_count[knob_name] += 1
    return knob_count

# 删除多余的重复knob_name及其数据，只保留其中一份
def remove_duplicates(data, knob_count):
    seen_knobs = set()
    cleaned_data = []
    
    for item in data:
        knob_name = item.get('knob_name')
        if knob_name:
            if knob_name not in seen_knobs:  # 保留第一个出现的
                cleaned_data.append(item)
                seen_knobs.add(knob_name)
            elif knob_count[knob_name] > 1:
                knob_count[knob_name] -= 1  # 删除多余的重复项
        else:
            cleaned_data.append(item)  # 非 knob_name 数据也保留
    return cleaned_data

# 写入JSON文件
def write_json(data, file_path):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# 主函数
def main():
    # 读取JSON文件和mysql.txt文件
    json_file = '/root/LLVM/ConfTainter/src/test/func_match/modified_knob_names.json'
    txt_file = '/root/LLVM/ConfTainter/src/test/func_match/mysql_var.txt'
    json_data = read_json(json_file)
    mysql_vars = read_txt(txt_file)
    
    # 提取不在mysql.txt中的变量（missing_data）和剩下的数据（remain_data）
    missing_data, remain_data = extract_missing_and_remain(json_data, mysql_vars)
    
    # 将missing_data写入JSON文件
    if missing_data:
        missing_file = 'missing_knob_names.json'
        write_json(missing_data, missing_file)
        print(f"不匹配的knob_name及其数据已保存为: {missing_file}")
    else:
        print("没有不匹配的数据")
    
    # 处理remain_data，删除重复的knob_name
    knob_count = count_knob_names(remain_data)
    cleaned_data = remove_duplicates(remain_data, knob_count)
    
    # 将cleaned_data写入JSON文件
    cleaned_file = '/root/LLVM/ConfTainter/src/test/func_match/cleaned_knob_names.json'
    write_json(cleaned_data, cleaned_file)
    print(f"重复的knob_name已删除，结果保存到: {cleaned_file}")

if __name__ == '__main__':
    main()
