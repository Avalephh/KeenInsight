import os
import csv

varlist = [
 'binlog_cache_size',
 'flush_time',
 'innodb_flush_log_at_trx_commit',
 'innodb_io_capacity',
 'innodb_thread_concurrency',
 'max_binlog_size',
 'max_relay_log_size',
 'innodb_buffer_pool_size',
 'table_cache_size',
 'max_connections',
 'max_binlog_cache_size',
 'innodb_buffer_pool_instances',
 'innodb_read_io_threads',
 'innodb_write_io_threads',
 'innodb_use_native_aio',
 'innodb_log_files_in_group',
 'innodb_log_file_size',
 'innodb_lock_wait_timeout',
 'tmp_table_size',
 'max_heap_table_size',
 'join_buffer_size',
 'sort_buffer_size',
 'innodb_adaptive_hash_index',
 'read_buffer_size',
 'write_buffer_size',
 'lock_wait_timeout',
 'max_sort_size',
 'net_buffer_length',
 'flush',
 'binlog_format',
 'max_join_size',
 'sql_mode'
]


def check_file_empty(file_path):
    """检查文件是否为空或不存在，为空返回 'yes' ，不为空返回 'no'。"""
    # print("check_file: ", file_path,"\n")
    # print("os.path.isfile(file_path): ", os.path.isfile(file_path), "\n")
    # print("os.path.getsize(file_path): ", os.path.getsize(file_path), "\n")
    return 'yes' if os.path.isfile(file_path) and os.path.getsize(file_path) > 0 else 'no'

def extract_variables_from_directory(directory_path):
    """从给定目录中提取所有变量名（从文件名中解析出变量）。"""
    variables = set()  # 使用集合避免重复变量名
    
    for file_name in os.listdir(directory_path):
        # 检查是否符合 'var-变量' 命名格式
        if file_name.startswith("var-") and ('_function.txt' not in file_name and '-ControlDependency-records.dat' not in file_name):
            # 提取变量名，例如 'var-变量_function.txt' 中的 '变量'
            var_name = file_name.split('-')[1].split('.')[0]
            if(var_name not in varlist):
                variables.add(var_name)
    
    # print(list(variables))
    return list(variables)  # 返回变量名的列表

def write_csv(variables, directory_path, output_csv):
    """根据变量和文件内容检查，生成CSV文件。"""
    with open(output_csv, mode='w', newline='') as file:
        writer = csv.writer(file)
        
        # 写入表头
        writer.writerow(["参数名称", "数据流控制流", "代码段", "备注"])
        
        # 遍历变量名称并检查相关文件
        for var in variables:
            # 构造文件路径
            function_file = os.path.join(directory_path, f"var-{var}_function.txt")
            control_file = os.path.join(directory_path, f"var-{var}-ControlDependency-records.dat")
            code_file = os.path.join(directory_path, f"var-{var}.txt")
            
            # 检查每个文件是否为空
            dataflow_control = check_file_empty(function_file)
            code_segment = dataflow_control
            
            # 写入变量信息及判断结果到CSV
            writer.writerow([var, dataflow_control, code_segment, ""])

# 示例文件夹路径
directory_path = '/root/LLVM/ConfTainter/src/test'

# 提取文件夹中的所有变量名
variables = extract_variables_from_directory(directory_path)

# 输出CSV文件路径
output_csv = 'output.csv'

# 调用函数创建并写入CSV文件
write_csv(variables, directory_path, output_csv)
