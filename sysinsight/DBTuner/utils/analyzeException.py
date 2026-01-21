# 根据基准文件，分析异常函数
import pandas as pd
import os
import json

def compare_file_sample_rate(file, normal_file):
        file_data = pd.read_csv(file, sep='\t')
        normal_file_data = pd.read_csv(normal_file)
        # 转换采样率为数值格式（去掉百分号并转为浮点数）
        file_data['Sampling Rate (%)'] = file_data['Sampling Rate (%)'].str.replace('%', '').astype(float)
        normal_file_data['Min Sampling Rate (%)'] = normal_file_data['Min Sampling Rate (%)'].astype(float)
        normal_file_data['Max Sampling Rate (%)'] = normal_file_data['Max Sampling Rate (%)'].astype(float)
        normal_file_data['Average Sampling Rate (%)'] = normal_file_data['Average Sampling Rate (%)'].astype(float)

        out_of_range_functions = []

        for index, row in file_data.iterrows():
            function_name = row['Function']
            # absolute_count = row['Absolute Count']
            sample_rate = row['Sampling Rate (%)']

            csv_row = normal_file_data[normal_file_data['Function'] == function_name]
            if not csv_row.empty:
                min_value = csv_row['Min Sampling Rate (%)'].values[0]
                max_value = csv_row['Max Sampling Rate (%)'].values[0]
                mean_value = csv_row['Average Sampling Rate (%)'].values[0]

                # Check if the count is out of range
                if sample_rate < min_value or sample_rate > max_value:
                    # Calculate absolute difference from mean
                    diff_from_mean = abs(sample_rate - mean_value)
                    
                    # Determine if the count increased (1) or decreased (0)
                    change = 1 if sample_rate > mean_value else 0
                    
                    out_of_range_functions.append({
                        'Function': function_name,
                        'Sample Rate': sample_rate,
                        'Diff From Mean': diff_from_mean,
                        'Change': change  # 1 for increased, 0 for decreased
                    })

        sorted_out_of_range_functions = sorted(out_of_range_functions, key=lambda x: x['Diff From Mean'], reverse=True)

        # Define output file path
        base, ext = os.path.splitext(file)
        output_file_path = f"{base}_btFunctions.txt"

        # Write the sorted results to the output file
        with open(output_file_path, 'w') as f:
            f.write('Function\tSample Rate(%)\tDiff From Mean\tChange\n')
            for item in sorted_out_of_range_functions:
                f.write(f"{item['Function']}\t{item['Sample Rate']}\t{item['Diff From Mean']}\t{item['Change']}\n")

        print(f"Results written to {output_file_path}")

        return output_file_path, sorted_out_of_range_functions
    

def get_key_functions(file, normal_file):
    output_file_path, sorted_out_of_range_functions = compare_file_sample_rate(file, normal_file)
    return output_file_path
    

def analyze_exception(workload,dbms,metric,file,normal_file,resource_file):
    # 获取参数文件
    # init_configs = pd.read_json(knob_file)
    # init_configs = init_configs.to_dict(orient='records')
    # 获取负载类型，dbms类型，优化指标
    task_context = {}
    with open(f'db_configurations/task/dbtune.json', 'r') as f:
        task_context = json.load(f)
    # 不是benchmark的话，需要指定是ap还是tp型，确保优化指标正确
    if workload == 'oltp' :
        task_context['workload_type'] = workload
        task_context['task'] = ''
        task_context['lower_is_better'] = False
        task_context['dbms'] = dbms
    elif workload == 'olap' :
        task_context['workload_type'] = workload
        task_context['task'] = 'tpch'
        task_context['lower_is_better'] = True
        task_context['dbms'] = dbms

    with open(f'db_configurations/dbtune.json', 'r') as f:
        task_context['hyperparameter_constraints'] = json.load(f)["MySQL_Parameters"]
    with open(f'db_configurations/init/mysql_default_values.json', 'r') as f:
        task_context['hyperparameter_default'] = json.load(f)[0]
    
    # 读取当前配置对应的metric
    metrics_dict = {
        "score": metric,
        "generalization_score": metric,
    }
    # 读取基准函数，比较两者差异，返回异常函数文件
    output_file_path, sorted_out_of_range_functions = compare_file_sample_rate(file, normal_file)

    # 读取资源使用情况
    with open(resource_file, "r", encoding="utf-8") as f:
        json_data = json.load(f)
    raw_resource = json_data[0]["resource"]
    resource = {
        'cpu': list(raw_resource.values())[0],        # 索引0 → cpu
        'readIO': list(raw_resource.values())[1],     # 索引1 → readIO
        'writeIO': list(raw_resource.values())[2],    # 索引2 → writeIO
        'IO': list(raw_resource.values())[1] + list(raw_resource.values())[2],  # IO = readIO+writeIO
        'virtualMem': list(raw_resource.values())[3], # 索引3 → virtualMem
        'physical': list(raw_resource.values())[4],   # 索引4 → physical
        'hit': list(raw_resource.values())[5],        # 索引6 → hit
    }
    
    
    return metrics_dict,task_context,output_file_path,resource
