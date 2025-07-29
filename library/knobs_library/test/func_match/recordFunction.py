import json
import os
import re
import pandas as pd

def extract_functions_from_file(file_path):
    # 读取文件内容
    with open(file_path, 'r') as file:
        content = file.read()

    # 提取控制流函数
    control_flow_functions_set = set(re.findall(r"Controlled Function:\s+([\w:]+)", content))

    # 提取数据流函数，确保函数不在控制流中
    data_flow_functions_set = set(re.findall(r"Function:\s+([\w:]+)", content)) - control_flow_functions_set

    return data_flow_functions_set, control_flow_functions_set

def record(formatted_data, save_file):
    # 如果文件不存在，创建一个空的 JSON 文件
    if not os.path.exists(save_file):
        with open(save_file, 'w') as f:
            json.dump([], f)

    # 读取并更新 JSON 文件的内容
    with open(save_file, 'r+') as f:
        try:
            # 如果文件内容为空或无效，捕获异常并初始化为空列表
            data = json.load(f)
        except json.JSONDecodeError:
            data = []  # 文件为空时，初始化为一个空列表

        data.append(formatted_data)  # 添加新的数据
        f.seek(0)  # 重置文件指针
        json.dump(data, f, indent=4)  # 写入更新后的数据
        f.truncate()  # 清除文件中多余的内容

def main():
    # TODO: 替换这个
    # file1 = '/root/LLVM/ConfTainter/src/test/tmp_table_size-ControlDependency-records.dat'

    # file2 = '/root/LLVM/countSim/perf_1_counts_normal.txt'
    
    read_path = '/root/LLVM/ConfTainter/src/test'
    write_path = '/root/LLVM/ConfTainter/src/test/func_match'
    save_file = '/root/LLVM/ConfTainter/src/test/func_match/function_all.json'


    
    for file in os.listdir(read_path):
        if file.startswith("var-") and file.endswith("-ControlDependency-records.dat") :
            variable_name = file.split('var-')[1].split('-ControlDependency-records.dat')[0]
            print(variable_name)
            dat_file = os.path.join(read_path, file)
            
            data_flow_functions, control_flow_functions = extract_functions_from_file(file)
            knob_name = file.split('/')[-1].split('-')[0]

            # os.chdir(write_path)
            result = {}
            result['knob_name'] = variable_name
            result['data_flow_functions'] = list(data_flow_functions)
            result['control_flow_functions'] = list(control_flow_functions)
            result['data_flow_functions_num'] = len(data_flow_functions)
            result['control_flow_functions_num'] = len(control_flow_functions)
            result['total_functions_num'] = len(data_flow_functions) + len(control_flow_functions)
        
            record(result, save_file)

    # os.chdir('/root/LLVM/ConfTainter/src/test/countSim')
    # os.system('python3 merge_json.py')

if __name__ == '__main__':
    main()
