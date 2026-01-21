# app/api/tune_run.py

from fastapi import APIRouter, HTTPException
from app.schemas.tune import StartTuneRequest
from app.services.docker_service import exec_in_container,read_json_from_container
import threading
import os

router = APIRouter()

CONTAINER = "ctt_sysinsight"
CONFIG_DIR = "/home/sysinsight/DBTuner/config/"
RESULT_DIR = "/home/sysinsight/sysinsight_front"

TASK_STATUS = {}  # demo 级内存状态管理

def run_tune_task(task_name: str):
    ini_path = f"/home/sysinsight/DBTuner/config/{task_name}.ini"

    try:
        TASK_STATUS[task_name] = "running"

        cmd = (
            "bash -lc "
            f"'source ~/.zshrc && "
            f"cd /home/sysinsight && "
            f"conda activate sysinsight && "
            f"python3 main.py --config={ini_path}'"
        )


        exec_in_container("ctt_sysinsight", cmd)

        TASK_STATUS[task_name] = "success"

    except Exception as e:
        TASK_STATUS[task_name] = "failed"
        print(f"[ERROR] tune task {task_name} failed:", e)



@router.post("/api/tune/startTune")
def start_tune(req: StartTuneRequest):
    task_name = req.taskName

    if not task_name:
        raise HTTPException(status_code=400, detail="taskName is required")

    if TASK_STATUS.get(task_name) == "running":
        return {
            "code": 400,
            "msg": "任务正在运行中",
            "task_name": task_name,
            "status": "running"
        }

    # 后台线程启动
    thread = threading.Thread(
        target=run_tune_task,
        args=(task_name,),
        daemon=True
    )
    thread.start()

    TASK_STATUS[task_name] = "pending"

    return {
        "code": 200,
        "msg": "调优任务已启动",
        "task_name": task_name,
        "status": "running"
    }


@router.get("/api/tune/multiTuneStatus")
def tune_status(taskName: str):
    return {
        "code": 200,
        "task_name": taskName,
        "status": TASK_STATUS.get(taskName, "pending")
    }


@router.get("/api/tune/getMultiTuneResult")
def get_tune_result(taskName: str):
    result_file = f"{RESULT_DIR}/tune_results_{taskName}.json"

    results = read_json_from_container(CONTAINER, result_file)

    rounds = []
    round_params = {}

    base_config = results[0]["configuration"]
    base_metrics = results[0]["external_metrics"]

    for idx, item in enumerate(results):
        round_id = idx + 1
        metrics = item["external_metrics"]
        cur_config = item["configuration"]
        
        base_tps = base_metrics["tps"]
        base_lat = base_metrics["lat"]

        cur_tps = metrics["tps"]
        cur_lat = metrics["lat"]
        
        if idx == 0:
            improvement = 0
        else:
            tps_improve = (cur_tps - base_tps) / base_tps * 100
            lat_improve = (base_lat - cur_lat) / base_lat * 100

            improvement = round(max(0, tps_improve, lat_improve), 2)

        # ---------- 1. rounds 概览 ----------
        rounds.append({
            "round": round_id,
            "tps": cur_tps,
            "latency": round(cur_lat, 3),
            "improvement": improvement
        })

        params = []

        # ---------- 2. roundParams ----------
        if idx == 0:
            # ✅ 第一轮：展示全部参数
            for k, v in cur_config.items():
                params.append({
                    "paramName": k,
                    "currentValue": v,
                    "originalValue": v,
                    "changeType": "不变",
                    "changePercent": 0,
                    "desc": "",
                    "riskLevel": "low"
                })
        else:
            # ✅ 后续轮：只展示变化参数
            for k, cur_v in cur_config.items():
                base_v = base_config.get(k)

                if base_v is None or cur_v == base_v:
                    continue

                params.append({
                    "paramName": k,
                    "currentValue": cur_v,
                    "originalValue": base_v,
                    "changeType": "提升" if cur_v > base_v else "降低",
                    "changePercent": round(abs(cur_v - base_v) / base_v * 100, 2) if isinstance(cur_v, (int, float)) and isinstance(base_v, (int, float)) and base_v != 0 else 0,
                    "desc": "",
                    "riskLevel": "medium"
                })

        round_params[str(round_id)] = params

    return {
        "code": 200,
        "msg": "success",
        "data": {
            "rounds": rounds,
            "roundParams": round_params
        }
    }
