# app/api/tune_run.py

from fastapi import APIRouter, HTTPException
from app.schemas.tune import StartTuneRequest
from app.services.docker_service import exec_in_container
import threading

router = APIRouter()

CONTAINER = "ctt_sysinsight"
CONFIG_DIR = "/home/sysinsight/DBTuner/config/"

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
            "task_id": task_name,
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
        "task_id": task_name,
        "status": "running"
    }


@router.get("/api/tune/status")
def tune_status(taskName: str):
    return {
        "code": 200,
        "task_id": taskName,
        "status": TASK_STATUS.get(taskName, "pending")
    }
