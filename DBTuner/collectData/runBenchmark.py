import os
import json
import argparse
from DBTuner.config import parse_args
from DBTuner.dbenv import DBEnv
from DBTuner.database.mysqldb import MysqlDB
import numpy as np

import sys
sys.path.append('/root/sysinsight-main')



def evaluate_configuration(env, knobs):
    # 运行一个配置并返回 tps
    print('Applying knobs: %s.' % (knobs))
    timeout, metrics, internal_metrics,function_file = env.step_GP_data(knobs=knobs, collect_resource=True)
    return metrics, internal_metrics,function_file
    # print('Objective value: %s.' % (metrics[0]))

def save_results(env, knobs, metrics, internal_metrics, function_file):

    # external_metrics 定义为字典而不是元组
    external_metrics = {
        'tps': metrics[0],
        'lat': metrics[1],
        'qps': metrics[2],
        'tpsVar': metrics[3],
        'latVar': metrics[4],
        'qpsVar': metrics[5],
    }

    # 判断 tps 的值，并根据范围记录参数
    DEFAULT_TPS = 1507.88
    # innodb_redo_log_capacity = 200M
    # DEFAULT_TPS = 6741.12
    FLOUCTUATION_PERCENTAGE = 0.05  # 5%
    LOW_THRESHOLD = DEFAULT_TPS * (1 - FLOUCTUATION_PERCENTAGE)  # 6461
    HIGH_THRESHOLD = DEFAULT_TPS * (1 + FLOUCTUATION_PERCENTAGE) # 7141

    if external_metrics['tps'] < LOW_THRESHOLD:
        result_category = 'bad_param_value'
    elif external_metrics['tps'] > HIGH_THRESHOLD:
        result_category = 'good_param_value'
    else:
        result_category = 'normal_param_value'

    # 构建结果数据
    result_data = {
        "knobs": knobs,
        "external_metrics": external_metrics,
        # "internal_metrics": list(internal_metrics),
        # "resource": resource_data,
        "function_file": function_file,
        "category": result_category
    }

    # 定义保存文件的路径
    json_file = 'evaluation_results.json'
    # 如果文件不存在，则创建新文件，并写入初始的列表结构
    if not os.path.exists(json_file):
        with open(json_file, 'w') as f:
            json.dump([], f)

    # 追加数据到文件中
    with open(json_file, 'r+') as f:
        data = json.load(f)  # 读取现有的内容
        data.append(result_data)  # 将新结果添加到列表中
        f.seek(0)  # 重置文件指针到文件开始
        json.dump(data, f, indent=4)  # 将数据写回文件，格式化输出

    

def run_benchmark(env, params):
    metrics, internal_metrics,flamegraph_name = evaluate_configuration(env, params)
    # 保存结果
    save_results(env, params, metrics, internal_metrics, flamegraph_name)
    # print(f"{params} -> tps: {tps}")
    # return tps

if __name__ == '__main__':
    # 解析命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default='/root/sysinsight-main/DBTuner/config_test.ini', help='config file')
    parser.add_argument('--knobs_file', type=str,default='/root/AI4DB/DBTune/scripts/knob_config/table_open_cache.json', help='JSON string of knobs and their values')
    opt = parser.parse_args()

    # 解析配置文件
    args_db, args_tune = parse_args(opt.config)

    if args_db['db'] == 'mysql':
        db = MysqlDB(args_db)
    
    # 创建数据库环境
    env = DBEnv(args_db, args_tune, db)

    # knobs = {
    #     'max_heap_table_size': 16777216, 'sort_buffer_size': 262144, 'tmp_table_size': 16777216
    # }
    knobs = {
        'innodb_spin_wait_delay': 6
    } 
    for i in range(5):
        run_benchmark(env, knobs)