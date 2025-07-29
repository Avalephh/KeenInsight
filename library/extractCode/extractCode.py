import re
import os

def extract_function_code(file_path, function_name):
    try:
        with open(file_path, 'r') as file:
            lines = file.readlines()
    except FileNotFoundError:
        print(f"Error: File not found - {file_path}")
        return None

    function_code = []
    in_function = False
    brace_count = 0

    # 匹配函数开始的正则表达式，支持返回类型和函数名
    # TODO: 根据不同的函数定义，选择不同的正则表达式，主要是3和6
    # function_pattern = re.compile(rf'\s*\w+\s+{function_name}\s*\(.*')
    # function_pattern = re.compile(rf'\s*(?:\w+\s+)*{function_name}\s*\(.*\)')
    # function_pattern = re.compile(rf'(\s*\w+\s+{function_name}\s*\(.*)|(\s*{function_name}\s*\(.*\))|(\s*(?:\w+\s+)*{function_name}\s*\([^)]*\)\s*{{)|(\s*(?:static\s+|inline\s+|virtual\s+|const\s+|constexpr\s+|extern\s+)?\w+\s+\**{function_name}\s*\([^)]*\)\s*{{)|(\s*(?:static\s+|inline\s+|virtual\s+|const\s+|constexpr\s+|extern\s+)?\w+\s+\**{function_name}\s*\([^)]*\)\s*{{)|(\s*(?:\w+\s+)*{function_name}\s*\(.*\))')
    # function_pattern = re.compile(rf'\s*(?:static\s+|inline\s+|virtual\s+|const\s+|constexpr\s+|extern\s+)?\w+\s+\**(?:\w+::)?{function_name}\s*\([^)]*\)\s*{{')
    # function_pattern = re.compile(rf'\s*(?:static\s+|inline\s+|virtual\s+|const\s+|constexpr\s+|extern\s+)?(?:\w+\s+)*(?:\w+::)?{function_name}\s*\([^)]*\)\s*{{')
    function_pattern = re.compile(rf'\s*(?:static\s+|inline\s+|virtual\s+|const\s+|constexpr\s+|extern\s+)?(?:\w+\s+)*{function_name}\s*\([^)]*\)\s*{{')
    

    for line in lines:
        if not in_function:
            if function_pattern.match(line):
                in_function = True
                brace_count += line.count('{') - line.count('}')
                function_code.append(line)
                continue
        else:
            function_code.append(line)
            brace_count += line.count('{')
            brace_count -= line.count('}')
            if brace_count == 0:
                break

    return ''.join(function_code) if in_function else None

def extract_functions_and_files(trace_file_path):
    function_info = {}
    with open(trace_file_path, 'r') as file:
        for line in file:
            # 跳过包含 Controlled Function 的行
            if 'Controlled Function:' in line:
                continue
            
            # function_match = re.search(r'Function:\s*(\w+)', line)
            function_match = re.search(r'Function:\s*([\w:]+)', line)
            location_match = re.search(r'Location:\s*(.*?):\d+:\d+', line)

            # 只处理有有效 Location 的行
            if function_match and location_match:
                function_name = function_match.group(1)
                file_path = location_match.group(1).strip()
                if os.path.isfile(file_path):
                    function_info.setdefault(file_path, []).append(function_name)
                    # print(file_path)
                else:
                    print(f"Warning: File path does not exist - {file_path}")

            elif function_match:
                print(f"Skipping entry without valid location: {line.strip()}")

    return function_info

def main(trace_file_path, output_file_path):
    function_info = extract_functions_and_files(trace_file_path)
    extracted_functions = set()  # 存储已提取的函数
    
    with open(output_file_path, 'w') as output_file:
        for file_path, function_names in function_info.items():
            for function_name in function_names:
                if function_name not in extracted_functions:
                    code_segment = extract_function_code(file_path, function_name)
                    if code_segment:
                        output_file.write(f"-------------------------------------------------------------------------------------------\n")  # 添加分隔线
                        output_file.write(f"File: {file_path}\n")
                        output_file.write(f"Function: {function_name}\n")
                        output_file.write(code_segment + "\n\n")
                        extracted_functions.add(function_name)  # 添加到已提取集合
                    else:
                        output_file.write(f"-------------------------------------------------------------------------------------------\n")  # 添加分隔线
                        output_file.write(f"File: {file_path}\n")
                        output_file.write(f"Function: {function_name} not found.\n\n")

trace_file_path = '/root/LLVM/ConfTainter/src/test/sql_mode-ControlDependency-records.dat'  # 输入你的数据流文件路径
output_file_path = 'sql_mode_code.txt'  # 输出文件路径

main(trace_file_path, output_file_path)
