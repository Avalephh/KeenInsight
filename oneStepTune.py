import pandas as pd
import configparser
from llambo.llambo import LLAMBO
from DBTuner.utils.analyzeException import analyze_exception
from DBTuner.utils.predictMetric import llm_predict_performance_delta
import json
import os
import argparse
import numpy as np
# 使用命令
# python3 oneStepTune.py --config <配置文件路径> --task_name <任务名>

OUTPUT_DIR = "/home/sysinsight/OneStepTuning/" 
def to_json_safe(obj):
    if isinstance(obj, dict):
        return {k: to_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [to_json_safe(v) for v in obj]
    elif isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    else:
        return obj

def run_onestep_tuning(config_file: str, chat_engine: str = "gpt-4o-mini"):
    """
    一步调优（不压测），返回参数推荐和预期性能提升

    Returns:
        {
            "base_config": dict,
            "recommended_config": dict,
            "base_score": float,
            "predicted_score": float,
            "delta_score": float,
            "reasoning": str
        }
    """

    # ---------- 1. 读取配置 ----------
    config = configparser.ConfigParser()
    config.read(config_file)

    workload = config['database']['one_workload_type']
    dbms = config['database']['db']
    knob_file = config['database']['knob_init_file']
    metric = int(config['database']['db_performance_metric'])
    file = config['database']['current_function_file']
    normal_file = config['database']['base_function_file']
    resource_file = config['database']['current_db_resource']

    seed = 42

    # ---------- 2. 初始化参数 ----------
    def generate_initialization(n_samples):
        init_configs = pd.read_json(knob_file)
        init_configs = init_configs.to_dict(orient='records')
        assert len(init_configs) == n_samples
        return init_configs

    # ---------- 3. 异常函数分析 ----------
    metrics, task_context, output_file_path, db_resource = analyze_exception(
        workload, dbms, metric, file, normal_file, resource_file
    )

    # ---------- 4. One-step 评估函数（不压测） ----------
    def oneStepFunction(config_dict):
        metrics_dict = metrics
        resource = db_resource
        keyFunction_file = output_file_path
        return config_dict, metrics_dict, resource, keyFunction_file

    # ---------- 5. 运行 LLAMBO ----------
    llambo = LLAMBO(
        task_context,
        sm_mode='discriminative',
        n_candidates=10,
        n_templates=2,
        n_gens=10,
        alpha=0.1,
        n_initial_samples=1,
        n_trials=1,
        init_f=generate_initialization,
        bbox_eval_f=oneStepFunction,
        chat_engine=chat_engine
    )
    llambo.seed = seed

    configs, fvals = llambo.optimize()

    # ---------- 6. LLM 预测性能提升 ----------
    base_config = configs.iloc[0].to_dict()
    new_config = configs.iloc[1].to_dict()

    base_score = fvals.iloc[0]["score"]

    pred = llm_predict_performance_delta(
        base_config=base_config,
        new_config=new_config,
        base_score=base_score
    )
    print("Predicted performance delta:", pred)

    return {
        "base_config": base_config,
        "recommended_config": new_config,
        "base_score": base_score,
        "predicted_score": pred["predicted_score"],
        "delta_score": pred["delta_score"],
        "reasoning": pred["reasoning"]
    }
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="一步调优脚本")
    parser.add_argument("--config", "-c", required=True,
                        help="配置文件路径")
    parser.add_argument("--task_name", "-t", required=True,
                        help="任务名，用于输出结果文件")

    args = parser.parse_args()

    result = run_onestep_tuning(args.config)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(
        OUTPUT_DIR, f"{args.task_name}.json"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(to_json_safe({
            "base_config": result["base_config"],
            "recommended_config": result["recommended_config"],
            "base_score": result["base_score"],
            "predicted_score": result["predicted_score"],
            "delta_score": result["delta_score"],
            "reasoning": result["reasoning"]
        }), f, indent=2, ensure_ascii=False)
