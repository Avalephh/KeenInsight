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

def calculated_proportion(file1,file2):
    # 提取函数名
    data_flow_functions1, control_flow_functions1 = extract_functions_from_file(file1)
    # print(data_flow_functions1)
    # print(control_flow_functions1)
    
    file2_df = pd.read_csv(file2,delimiter='\t')

    file2_functions = file2_df['Function'].tolist()

    data_matched_functions = [func for func in data_flow_functions1 if func in file2_functions]

    data_matched_count = len(data_matched_functions)

    control_matched_functions = [func for func in control_flow_functions1 if func in file2_functions]

    control_matched_count = len(control_matched_functions)

    return data_matched_functions,data_matched_count, control_matched_functions,control_matched_count

def save_json(formatted_data, variable_name):
    result_file = 'result_'+ variable_name +'.json'
    if not os.path.exists(result_file):
        with open(result_file, 'w') as f:
            json.dump([], f)
    
    with open(result_file, 'r+') as f:
        data = json.load(f) 
        data.append(formatted_data)  
        f.seek(0)  
        json.dump(data, f, indent=4)  

def main():
    # TODO: 替换这个
    # file1 = '/root/LLVM/ConfTainter/src/test/tmp_table_size-ControlDependency-records.dat'

    # file2 = '/root/LLVM/countSim/perf_1_counts_normal.txt'
    
    read_path = '/root/LLVM/ConfTainter/src/test'
    write_path = '/root/LLVM/ConfTainter/src/test/countSim'
    # normal_file = '/root/LLVM/ConfTainter/src/test/countSim/perf_1_counts_normal.txt'
    normal_file = '/root/LLVM/ConfTainter/src/test/countSim/result_job/JOB_perf_function.txt'
    
    for file in os.listdir(read_path):
        if file.startswith("var-") and file.endswith("-ControlDependency-records.dat") :
            variable_name = file.split('var-')[1].split('-ControlDependency-records.dat')[0]
            print(variable_name)
            dat_file = os.path.join(read_path, file)
            
            data_matched_functions, data_matched_count, control_matched_functions, control_matched_count = calculated_proportion(dat_file, normal_file)
            # knob_name = file1.split('/')[-1].split('-')[0]

            os.chdir(write_path)
            result = {}
            result["knob_name"] = variable_name
            result['data_matched_functions'] = data_matched_functions
            result['data_matched_count'] = data_matched_count
            result['control_matched_functions'] = control_matched_functions
            result['control_matched_count'] = control_matched_count
            result['matched_count'] = data_matched_count+control_matched_count
            save_json(result, variable_name)
        
            print(f"Knob Name: {variable_name}")
            print(f"Data Flow Functions Matched: {data_matched_count}")
            for func in data_matched_functions:
                print(func)  
            
            print("----------------------------")
            print(f"Control Flow Functions Matched: {control_matched_count}")
            for func in control_matched_functions:
                print(func)

            print("----------------------------")
            print(f"Knob {variable_name} the matched functions nums is {data_matched_count+control_matched_count}")

    os.chdir('/root/LLVM/ConfTainter/src/test/countSim')
    os.system('python3 merge_json.py')

if __name__ == '__main__':
    main()