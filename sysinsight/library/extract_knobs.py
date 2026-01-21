import os
import openai
import time


# 获得参数关联库的上下文
def get_parameter():
    directory_path = "/root/AI4DB/hzt/library/knobs_library/test"
    
    variables = []
    functions = {}
    codes = {}
    flows = {}

    for file_name in os.listdir(directory_path):
        # 检查是否符合 'var-变量' 命名格式
        if file_name.startswith("var-") and not file_name.endswith("_function.txt") and not '-ControlDependency-records.dat' in file_name:
            # 提取变量名，例如 'var-变量_function.txt' 中的 '变量'
            var_name = file_name.split('-')[1].split('.')[0]
            if var_name not in variables:
                variables.append(var_name)

        # 处理函数文件
        if file_name.endswith("_function.txt"):
            file_path = os.path.join(directory_path, file_name)
            function_list = []
            with open(file_path, 'r', encoding='utf-8') as file:
                lines = file.readlines()
                for line in lines:
                    if 'Function: ' in line and 'not found' not in line:
                        # 提取函数名
                        function_name = line.split('Function: ')[1].strip()
                        function_list.append(function_name)
                # 将函数名字符串连接成一个字符串
                function_string = '\n'.join(function_list)
            # 将变量名和函数名字符串关联起来
            var_name = file_name.split('_function.txt')[0].split('-')[1]
            if var_name:
                functions[var_name] = function_string
                codes[var_name] = ''.join(lines)
                
        if file_name.endswith("-ControlDependency-records.dat"):
            # print("file_name: ", file_name)
            file_path = os.path.join(directory_path, file_name)
            # print("file_path: ", file_path)
            with open(file_path, 'r', encoding='utf-8') as file:
                lines = file.readlines() 
                flow = ''.join(lines)
            var_name = file_name.split('-ControlDependency-records.dat')[0].split('-')[1]
            # print("var_name: ", var_name)
            if var_name:
                flows[var_name] = flow

    

    return variables, functions, codes, flows

# # 创建 prompt
# def create_prompt(template, **kwargs):
#     for key, value in kwargs.items():
#         template = template.replace(f"{{{key}}}", value)
#     return template

# # 记录GPT的回答
# def record_response(variable, response):
#     # 记录生成的内容到文件
#     time_stamp = time.time()
#     write_path = f"/root/AI4DB/hzt/library/{variable}_response_{time_stamp}.txt"
#     with open(write_path, 'w') as f:
#         f.write(response)
        
        
# if __name__ == "__main__":
#     variables, functions, codes, flows = get_parameter()
    
#     # print("variables: ", variables)
#     # print("functions: ", functions)
#     # print("codes: ", codes)
#     # print("flows: ", flows)


question_template2 = '''
As a database parameter tuning expert, please provide optimization recommendations for the {variable} parameter in MySQL based on the following information:

1. Database Environment:
   - Database kernel: mysql Ver 8.0.36-0 ubuntu0.20.04.1 for Linux on x86_64 ((Ubuntu))
   - Hardware configuration: 36 vCPUs and 156 GiB RAM

2. Workload Characteristics:
   - Benchmark tool: sysbench oltp-read-write
   - Data scale: 100 tables, 6,000,000 rows each
   - Concurrent threads: 150 threads

3. Target Parameter: {variable}

4. Key Functions Affected by the Parameter:
   {function}

5. Relevant Code Snippet:
   {code}
   
6. Relevant Dataflow and Control Dependencies:
   {flow}

7. Optimization Goals:
   - Improve database performance
   - Optimize memory usage
   - Reduce disk I/O

Based on the information provided, please analyze the impact of the {variable} parameter on database performance and provide specific optimization recommendations. Your response should follow this structure:

1. Start with a single line containing only the recommended value for {variable}. This should be a number without any additional text.

2. Following the recommended value, provide a detailed explanation of your recommendation, including:
   a. Justification for the recommended value
   b. The reasonable range of values for {variable} given the current hardware configuration
   c. Potential impacts of setting the parameter too high or too low
   d. How the recommended value aligns with the given workload characteristics
   e. Other factors or related parameters to consider when adjusting this parameter

Please ensure your explanation is comprehensive, providing detailed rationale to support your optimization recommendation.
'''
