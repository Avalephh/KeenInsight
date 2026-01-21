import os
import sys
import time
import argparse
import pandas as pd
import json
import random
from DBTuner.config import parse_args 
from DBTuner.database.mysqldb import MysqlDB 
from DBTuner.database.postgresqldb import PostgresqlDB 
from DBTuner.dbenv import DBEnv

def read_top_functions(file_path, top_n=10):
    """
    读取文件内容并返回前N个函数的信息，包括函数名、采样率和Change值。

    Args:
        file_path: 文件路径
        top_n: 需要获取的前N个函数，默认为10

    Returns:
        list: 包含前N个函数信息的字典列表
    """
    # 读取文件
    file_data = pd.read_csv(file_path, sep='\t')

    # 获取前N条记录
    top_functions = file_data.head(top_n)

    # 提取每个函数的信息
    top_functions_info = []
    for _, row in top_functions.iterrows():
        top_functions_info.append({
            'Function': row['Function'],
            'Sample Rate': row['Sample Rate(%)'],
            'Change': row['Change']
        })

    return top_functions_info

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

def getKeyFunction(file, normal_file):


def run_benchmark(config_file):
    print(f"加载配置文件: {config_file}")
    args_db, args_tune = parse_args(config_file)

    print(f"数据库类型: {args_db['db']}")
    print(f"负载类型: {args_db['workload']}")
    print(f"运行时间: {args_db['workload_time']}秒")

    if args_db['db'] == 'mysql':
        db = MysqlDB(args_db)
        print("创建MySQL数据库连接")
    elif args_db['db'] == 'postgresql':
        db = PostgresqlDB(args_db)
        print("创建PostgreSQL数据库连接")
    else:
        print(f"不支持的数据库类型: {args_db['db']}")
        return None

    env = DBEnv(args_db, args_tune, db)
    workload = args_db['workload']

    print(f"\n开始{workload.upper()}基准测试...")
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        if workload == 'sysbench':
            result = env.get_states_expe_sysbench(collect_resource=False)
            print("调用sysbench基准测试完成")
        elif workload == 'tpcc':
            result = env.get_states_expe_tpcc(collect_resource=True)
            print("调用TPC-C基准测试完成")
        elif workload == 'tpch':
            result = env.get_states_expe_tpch(collect_resource=True)
            print("调用TPC-H基准测试完成")
        else:
            print(f"不支持的负载类型: {workload}")
            return None

        timeout, external_metrics, internal_metrics, resource, function_range_name = result
        normal_file = f"/home/sysinsight/DBTuner/collectData/function_normal_{args_db['workload']}.csv"
        print(normal_file)
        out_of_function_file_sample_rate, sorted_out_of_range_functions_sample_rate = compare_file_sample_rate(function_range_name, normal_file)

        print(f"\n基准测试完成!")
        print(f"结束时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        print(f"\n性能指标:")
        print(f"  外部指标 (长度: {len(external_metrics)}): {external_metrics}")
        print(f"  内部指标 (长度: {len(internal_metrics)}): {internal_metrics[:5]}...")


        result = {
            'workload': workload,
            'external_metrics': external_metrics,
            'internal_metrics': internal_metrics,
            'resource': resource,
            'function_file': function_range_name,
            'key_function_file': out_of_function_file_sample_rate,
        }

        # 保存结果
        output_file = f"/home/sysinsight/FirstEvaluation/evaluation_{args_tune['task_id']}.json"
        with open(output_file, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        print(f"结果已保存到: {output_file}")
        
        return result

    except Exception as e:
        print(f"基准测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def main():
    parser = argparse.ArgumentParser(description="简化的数据库压测脚本")
    parser.add_argument("--config", "-c", required=True,
                       help="配置文件路径")

    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"配置文件不存在: {args.config}")
        return 1

    print("=" * 60)
    print("数据库基准测试工具")
    print("=" * 60)

    result = run_benchmark(args.config)

    if result:
        print(f"\n基准测试成功完成!")
        print(f"\n关键指标总结:")
        workload = result['workload']
        external = result['external_metrics']
        key_function_file = result['key_function_file']

        if workload == 'sysbench' and len(external) >= 3:
            print(f"  TPS: {external[0]:.2f}")
            print(f"  延迟: {external[1]:.2f} ms")
            print(f"  QPS: {external[2]:.2f}")
        elif workload == 'tpcc' and len(external) >= 1:
            print(f"  TPS: {external[0]:.2f}")
        elif workload == 'tpch' and len(external) >= 1:
            print(f"  总时间: {external[0]:.2f} 秒")

        print(f"\n前10个关键函数:")
        base_dir="/home/sysinsight"
        full_file_path = os.path.join(base_dir, key_function_file)
        # print(full_file_path)
        top_functions = read_top_functions(full_file_path, top_n=10)
        for func in top_functions:
            print(f"  {func['Function']}: 采样率 = {func['Sample Rate']:.2f}%, Change = {func['Change']}")
            
        
        return 0
    else:
        print("\n基准测试失败!")
        return 1

if __name__ == "__main__":
    main()
