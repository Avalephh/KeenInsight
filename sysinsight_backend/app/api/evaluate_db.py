from fastapi import APIRouter, HTTPException
import json
import os
import threading
import pandas as pd
from app.services.docker_service import exec_in_container

router = APIRouter()

CONTAINER = "ctt_sysinsight"
CONFIG_DIR = "/home/sysinsight/DBTuner/config/"
RESULT_DIR = "/home/sysinsight/FirstEvaluation/"  # 存放结果的目录

TASK_STATUS = {}  # demo 级内存状态管理


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


def run_tune_task(task_name: str):
    ini_path = f"/home/sysinsight/DBTuner/config/{task_name}.ini"
    
    try:
        TASK_STATUS[task_name] = "running"

        cmd = (
            "bash -lc "
            f"'source ~/.zshrc && "
            f"cd /home/sysinsight && "
            f"conda activate sysinsight && "
            f"python3 evaluation.py --config={ini_path}'"
        )

        # 执行压测命令
        output = exec_in_container(CONTAINER, cmd)

        # 将执行结果返回
        return output

    except Exception as e:
        TASK_STATUS[task_name] = "failed"
        print(f"[ERROR] tune task {task_name} failed:", e)


@router.post("/api/tune/firstEvaluate")
def start_tune_task(task_name: str):
    """
    启动评估任务并返回任务状态
    """
    if TASK_STATUS.get(task_name) == "running":
        return {"code": 400, "msg": "任务正在运行中", "task_id": task_name, "status": "running"}

    # 后台线程启动任务
    task_thread = threading.Thread(target=run_tune_task, args=(task_name,))
    task_thread.start()

    TASK_STATUS[task_name] = "pending"  # 标记为待处理状态

    return {"code": 200, "msg": "调优任务已启动", "task_id": task_name, "status": "running"}


@router.get("/api/tune/firstEvaluateResults")
def get_tune_results(task_name: str):
    """
    查询基准测试结果
    """
    result_file_path = os.path.join(RESULT_DIR, f"evaluation_{task_name}.json")
    
    # 判断文件是否存在
    if os.path.exists(result_file_path):
        with open(result_file_path, 'r') as f:
            result = json.load(f)
        
        # 提取所需的字段
        workload = result.get('workload', 'unknown')
        external = result.get('external_metrics', [])
        internal = result.get('internal_metrics', [])
        key_function_file = result.get('key_function_file', '')

        # 提取TPS、QPS、Latency
        tps = qps = latency = None
        if workload == 'sysbench' and len(external) >= 3:
            tps = external[0]  # 假设是TPS
            latency = external[1]  # 假设是延迟
            qps = external[2]  # 假设是QPS
        elif workload == 'tpcc' and len(external) >= 1:
            tps = external[0]  # 假设是TPS
        elif workload == 'tpch' and len(external) >= 1:
            latency = external[0]  # 假设是总时间

        # 获取前10个关键函数的信息
        top_functions_info = []
        if key_function_file:
            full_file_path = os.path.join(RESULT_DIR, key_function_file)
            if os.path.exists(full_file_path):
                top_functions_info = read_top_functions(full_file_path, top_n=10)

        result_data = {
            "workload": workload,
            "tps": tps,
            "qps": qps,
            "latency": latency,
            "top_functions": top_functions_info
        }

        return {
            "code": 200,
            "data": result_data
        }

    else:
        raise HTTPException(status_code=404, detail="结果文件未找到")
