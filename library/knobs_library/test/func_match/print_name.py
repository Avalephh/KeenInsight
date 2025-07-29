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
            variable = line.split()[0]
            variables.append(variable)
    return variables

# 遍历JSON并统计knob_name的出现次数，并收集所有knob_name
def count_and_collect_knob_names(data, knob_count, values):
    if isinstance(data, dict):
        for key, value in data.items():
            if key == 'knob_name':
                knob_count[value] += 1
                values.append(value)
            else:
                count_and_collect_knob_names(value, knob_count, values)
    elif isinstance(data, list):
        for item in data:
            count_and_collect_knob_names(item, knob_count, values)

# 查找重复的knob_name
def find_duplicates(knob_count):
    return {knob: count for knob, count in knob_count.items() if count > 1}

# 查找A中不在B中的元素
def check_list_inclusion(A, B):
    missing_elements = [item for item in A if item not in B]
    
    if missing_elements:
        print("以下元素在mysql list中找不到:")
        for item in missing_elements:
            print(item)
        print(f"总共有 {len(missing_elements)} 个knob_name变量未找到")
    else:
        print("匹配正确")
        
    return missing_elements

# 主函数
def main():
    # 读取JSON文件和txt文件
    json_file = '/root/LLVM/ConfTainter/src/test/func_match/modified_knob_names.json'
    txt_file = '/root/LLVM/ConfTainter/src/test/func_match/mysql_var.txt'
    json_data = read_json(json_file)
    mysql_value_list = read_txt(txt_file)

    # 统计和收集knob_name
    knob_count = defaultdict(int)
    knob_values = []
    count_and_collect_knob_names(json_data, knob_count, knob_values)
    
    print(f"总共有 {len(knob_values)} 个knob_name变量")
    print(f"总共有 {len(mysql_value_list)} 个mysql变量")

    # 检查哪些knob_name不在mysql list中
    missing_lists = check_list_inclusion(knob_values, mysql_value_list)

    # 查找重复的knob_name
    duplicates = find_duplicates(knob_count)
    if duplicates:
        print("\n发现重复的knob_name:")
        for knob, count in duplicates.items():
            print(f"{knob}: {count} 次")
    else:
        print("\n没有发现重复的knob_name")

if __name__ == '__main__':
    main()
