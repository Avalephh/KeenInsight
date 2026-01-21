import pandas as pd
from llambo.llambo import LLAMBO
import json
from DBTuner.optimizer import DBTune
import os
import configparser

db_name = 'dbtune'
para_name = "MySQL_Parameters"
seed = 42
chat_engine = 'gpt-4o-mini'

config = configparser.ConfigParser()
config.read('DBTuner/config_test.ini')
workload = config['database']['workload']
dbms = config['database']['db']

task_context = {}
with open(f'db_configurations/task/{db_name}.json', 'r') as f:
    task_context = json.load(f)

if workload == 'sysbench' :
    task_context['workload'] = workload
    task_context['task'] = ''
    task_context['lower_is_better'] = False
    task_context['dbms'] = dbms
elif workload == 'tpcc' :
    task_context['workload'] = workload
    task_context['task'] = ''
    task_context['lower_is_better'] = False
    task_context['dbms'] = dbms
elif workload == 'tpch' :
    task_context['workload'] = workload
    task_context['task'] = 'tpch'
    task_context['lower_is_better'] = True
    task_context['dbms'] = dbms

with open(f'db_configurations/{db_name}.json', 'r') as f:
    task_context['hyperparameter_constraints'] = json.load(f)[para_name]


with open(f'db_configurations/init/mysql_default_values.json', 'r') as f:
    task_context['hyperparameter_default'] = json.load(f)[0]

def generate_initialization(n_samples):
    init_configs = pd.read_json(f'db_configurations/init/{db_name}.json')
    init_configs = init_configs.to_dict(orient='records')
    assert len(init_configs) == n_samples
    return init_configs

bbox_eval_fiction = DBTune

# instantiate LLAMBO
llambo = LLAMBO(task_context, sm_mode='discriminative', n_candidates=10, n_templates=2, n_gens=10, 
                alpha=0.1, n_initial_samples=1, n_trials=20, 
                init_f=generate_initialization,
                bbox_eval_f=bbox_eval_fiction, 
                chat_engine=chat_engine)
llambo.seed = seed

# run optimization
configs, fvals = llambo.optimize()




