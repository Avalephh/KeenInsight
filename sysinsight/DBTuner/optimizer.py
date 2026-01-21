import configparser
import argparse
import json
import os

from collections import defaultdict
import shutil
from DBTuner.database.mysqldb import MysqlDB
from DBTuner.database.postgresqldb import PostgresqlDB
from DBTuner.config import parse_args
from DBTuner.dbenv import DBEnv
# from DBTuner.utils.getFunction import read_function_names
from DBTuner.knobs import initialize_knobs
from DBTuner.utils.getStaticFunction import get_functions_for_knobs
from DBTuner.utils.matchFunctions import match_knob_functions,read_function_names,read_function_names_with_change,find_top_and_matched_functions
from DBTuner.utils.extractCode import extract_code_for_knob_from_json


def rewrite_cnf(config, init_cnf, target_cnf):
    # Step 2: Load existing configuration
    config_parser = configparser.ConfigParser()
    config_parser.read(init_cnf)

    # Step 3: Update config without conflicts
    for key, value in config.items():
        if not config_parser.has_option('mysqld', key):  # Only add if it doesn't exist
            config_parser.set('mysqld', key, str(value))

    # Step 4: Write the updated configuration back to my.cnf
    with open(target_cnf, 'w') as configfile:
        config_parser.write(configfile)

def return_default_cnf(init_cnf, target_cnf):
    if os.path.exists(init_cnf):
        shutil.copy2(init_cnf, target_cnf)
    else:
        print("No backup found to restore.")

def change_value(config):
    change_config = {}
    # for key, value in config.items():
    #     if key=='big_tables' and value == 1:
    #         change_config[key] = 'ON'
    #     elif key=='big_tables' and value == 0:
    #         change_config[key] = 'OFF'
    #     else:
    #         change_config[key] = int(value)
            
    return config


def DBTune(config_file,config):
    
    change_config = change_value(config)
    
    
    staticFile = "/home/sysinsight/DBTuner/utils/paramater_association_library.json"
    codeFoder = '/home/sysinsight/library/extractCode'
    
    # db_config = '/home/sysinsight/DBTuner/config_test.ini'
    args_db, args_tune = parse_args(config_file)

    if args_db['db'] == 'mysql':
        db = MysqlDB(args_db)
    elif args_db['db'] == 'postgresql':
        db = PostgresqlDB(args_db)

    env = DBEnv(args_db, args_tune, db)
    timeout, external_metrics, internal_metrics, resource, function_file,keyFunction_file = env.step_GP_sysinght(knobs=change_config, collect_resource=True)

    # 保留规则维护时需要的文件格式
    metric = {
        "tps": external_metrics[0],
        "lat": external_metrics[1],
        "qps": external_metrics[2],
        "tpsVar": external_metrics[3],
        "latVar": external_metrics[4],
        "qpsVar": external_metrics[5]
    }
    
    resource = {
        'cpu': resource[0],
        'readIO': resource[1],
        'writeIO': resource[2],
        'IO': resource[1] + resource[2],
        'virtualMem': resource[3],
        'physical': resource[4],
        'dirty': resource[5],
        'hit': resource[6],
        'data': resource[7],
    }
    
    result_data = {
        "configuration": change_config,
        "external_metrics": metric,
        "resource": resource,
        "function_file": function_file
    }
     # 定义保存文件的路径
    json_file = f"sysinsight_front/tune_results_{args_tune['task_id']}.json"
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
    
    if args_db['workload']=='sysbench' or args_db['workload']=='tpcc':
        metrics_dict = {
            "score": external_metrics[0],
            "generalization_score": external_metrics[1],
        }
    elif args_db['workload']=='tpch':
        metrics_dict = {
            "score": external_metrics[1],
            "generalization_score": external_metrics[1],
        }
    else:
        raise ValueError('Invalid workload name!')
    
    return config, metrics_dict, resource, keyFunction_file

