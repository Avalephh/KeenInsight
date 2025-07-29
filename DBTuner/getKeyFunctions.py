import argparse
import os
import json
from DBTuner.knobs import initialize_knobs
from DBTuner.config import parse_args
from DBTuner.database.mysqldb import MysqlDB
from DBTuner.dbenv import DBEnv

def initial_knobs(knob_config,num):
    knob_details = initialize_knobs(knob_config,num)
    start_knobs = {}
    # print(knob_details)
    for name, value in knob_details.items():
        if not value['type'] == "combination":
            start_knobs[name] = value['startValue']
        else:
            knobL = name.strip().split('|')
            valueL = value['default'].strip().split('|')
            for i in range(0, len(knobL)):
                start_knobs[knobL[i]] = int(valueL[i])
    return start_knobs

def evaluate_configuration(env, knobs): 
    print('Applying knobs: %s.' % (knobs))
    timeout, metrics, internal_metrics,function_file,keyFunction_file = env.step_GP_sysinght(knobs=knobs, collect_resource=True)
    # return metrics, internal_metrics, resource,function_file
    return metrics, function_file,keyFunction_file

def save_results(env, knobs, metrics, function_file,keyFunction_file):
    external_metrics = {
        'tps': metrics[0],
        'lat': metrics[1],
        'qps': metrics[2],
        'tpsVar': metrics[3],
        'latVar': metrics[4],
        'qpsVar': metrics[5],
    }

    # 判断 tps 的值，并根据范围记录参数
    DEFAULT_TPS = 1904.10
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
        "keyFunction_file":keyFunction_file,
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
    # metrics, internal_metrics, resource,flamegraph_name = evaluate_configuration(env, params)
    metrics, flamegraph_name,keyFunction_file = evaluate_configuration(env, params)
    # 保存结果
    save_results(env, params, metrics, flamegraph_name,keyFunction_file)

if __name__ == '__main__':
       # 解析命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default='/root/AI4DB/hzt/DBTuner/config_test.ini', help='config file')
    parser.add_argument('--knobs_file', type=str,default='/root/AI4DB/hzt/DBTuner/knobspace/innodb_spin_wait_delay.json', help='JSON string of knobs and their values')
    opt = parser.parse_args()

    args_db, args_tune = parse_args(opt.config)

    if args_db['db'] == 'mysql':
        db = MysqlDB(args_db)
  
    env = DBEnv(args_db, args_tune, db)

    # knobs = {
    #     'lock_wait_timeout': 3000000
    # } 
    knobs = initial_knobs(args_db['knob_config_file'], int(args_db['knob_num']))
    run_benchmark(env, knobs)
