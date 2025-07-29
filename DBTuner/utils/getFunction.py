
def read_function_names(file_path, num_lines=200):
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

def read_function_names_with_change(file_path, num_lines=200):
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


# 使用文件路径调用该函数
# file_path = '/root/AI4DB/hzt/perf_data/perf_1730726837_counts_normal_keyFunctions.txt'
# function_list = read_function_names(file_path)
# print(function_list)

# file_path = '/root/AI4DB/hzt/perf_data/perf_1731554967_counts_bad_keyFunctions.txt'
# top_200_functions = read_function_names_with_change(file_path)
# for function, change in top_200_functions:
#     print(f"Function: {function}, Change: {change}")
