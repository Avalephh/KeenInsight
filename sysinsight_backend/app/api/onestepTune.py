from fastapi import APIRouter, HTTPException
import os
import json
import threading
import time
from app.services.docker_service import exec_in_container, read_json_from_container, file_exists_in_container, read_file_from_container

router = APIRouter()

CONTAINER = "ctt_sysinsight"
CONTAINER_CONFIG_DIR = "/home/sysinsight/DBTuner/config/"
CONTAINER_OUTPUT_DIR = "/home/sysinsight/OneStepTuning/"  
CONTAINER_HOME = "/home/sysinsight/"

# 使用简单的字符串状态
TASK_STATUS = {}

@router.get("/api/tune/oneStepStatus")
def get_tune_status(taskName: str):
    """获取调优任务状态"""
    # 如果任务不存在，检查是否有结果文件
    if taskName not in TASK_STATUS:
        # 检查容器内是否有结果文件
        output_path = os.path.join(CONTAINER_OUTPUT_DIR, f"{taskName}.json")
        try:
            if file_exists_in_container(CONTAINER, output_path):
                TASK_STATUS[taskName] = "success"
            else:
                TASK_STATUS[taskName] = "idle"
        except:
            TASK_STATUS[taskName] = "idle"
    
    status = TASK_STATUS.get(taskName, "idle")
    
    return {
        "code": 200,
        "msg": "success",
        "status": status,
        "task_id": taskName
    }

def run_onesteptune_task(task_name: str):
    """在后台运行一步调优任务"""
    ini_path = f"/home/sysinsight/DBTuner/config/{task_name}.ini"
    
    # 设置为运行中
    TASK_STATUS[task_name] = "running"
    print(f"[INFO] 任务 {task_name} 开始执行")
    
    try:
        cmd = (
            "bash -lc "
            f"'source ~/.zshrc && "
            f"cd /home/sysinsight && "
            f"conda activate sysinsight && "
            f"python3 oneStepTune.py "
            f"--config={ini_path} "
            f"--task_name={task_name}'"
        )
        
        # 执行命令
        output = exec_in_container(CONTAINER, cmd)
        print(f"[INFO] 任务 {task_name} 执行完成")
        print(f"[DEBUG] 输出: {output[:500]}...")
        
        # # 验证结果文件是否存在
        # output_path = os.path.join(CONTAINER_OUTPUT_DIR, f"{task_name}.json")
        # if file_exists_in_container(CONTAINER, output_path):
        #     TASK_STATUS[task_name] = "success"
        #     print(f"[INFO] 任务 {task_name} 成功完成")
        # else:
        #     TASK_STATUS[task_name] = "failed"
        #     print(f"[ERROR] 任务 {task_name} 完成但未找到结果文件")
        TASK_STATUS[task_name] = "success"
        print(f"[INFO] 任务 {task_name} 成功完成")
            
    except Exception as e:
        TASK_STATUS[task_name] = "failed"
        print(f"[ERROR] 任务 {task_name} 执行失败: {str(e)}")

@router.get("/api/tune/oneStepResult")
def one_step_tune(task_name: str):
    """
    获取一步调优结果
    """
    try:
        # 检查任务状态
        task_status = TASK_STATUS.get(task_name)
        
        # 如果任务状态是字符串，直接比较
        if isinstance(task_status, str):
            if task_status != "success":
                raise HTTPException(
                    status_code=400,
                    detail="调优任务尚未完成或已失败"
                )
        # 如果是字典（之前的格式）
        elif isinstance(task_status, dict):
            if task_status.get("status") != "success":
                raise HTTPException(
                    status_code=400,
                    detail="调优任务尚未完成或已失败"
                )
        # 如果没有状态记录
        else:
            # 检查结果文件是否存在
            # output_path = os.path.join(CONTAINER_OUTPUT_DIR, f"{task_name}.json")
            # if not file_exists_in_container(CONTAINER, output_path):
            #     raise HTTPException(
            #         status_code=400,
            #         detail="调优任务未找到"
            #     )
            # 如果文件存在，更新状态
            TASK_STATUS[task_name] = "success"

        # 读取结果文件
        output_path = os.path.join(CONTAINER_OUTPUT_DIR, f"{task_name}.json")
        result = read_json_from_container(CONTAINER, output_path)

        return {
            "code": 200,
            "msg": "一步调优完成",
            "data": {
                "records": result
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] 获取调优结果失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"获取调优结果失败: {str(e)}"
        )


# 修改异常函数读取接口
@router.get("/api/tune/exception-functions")
def get_exception_functions(taskName: str, topN: int = 10):
    """
    1. 在容器内执行 analyzeException.py
    2. 从 stdout 中解析 [RESULT_FILE]
    3. 读取 *_btFunctions.txt
    4. 解析并返回 TopN 异常函数
    """

    # ---------- 1. ini 文件路径（容器内） ----------
    ini_path = f"/home/sysinsight/DBTuner/config/{taskName}.ini"

    # if not file_exists_in_container(CONTAINER, ini_path):
    #     raise HTTPException(
    #         status_code=404,
    #         detail=f"配置文件不存在: {ini_path}"
    #     )

    # ---------- 2. 执行容器内分析脚本 ----------http://10.77.110.147:5173/exceptionanalysis?&taskName=onestep
    cmd = (
        "bash -lc "
        f"'source ~/.zshrc && "
        f"cd /home/sysinsight/ && "
        f"conda activate sysinsight && "
        f"python3 analyzeException.py --config={ini_path}'"
    )

    try:
        output = exec_in_container(CONTAINER, cmd)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"容器内执行 analyzeException 失败: {str(e)}"
        )

    # ---------- 3. 从 stdout 中解析结果文件路径 ----------
    # result_file = None
    # for line in output.splitlines():
    #     if line.startswith("[RESULT_FILE]"):
    #         result_file = line.replace("[RESULT_FILE]", "").strip()
    #         break

    # if not result_file:
    #     raise HTTPException(
    #         status_code=500,
    #         detail="未从 analyzeException 输出中获取到结果文件路径"
    #     )
    result_file = None
    function_to_knob = {}

    for line in output.splitlines():
        if line.startswith("[RESULT_FILE]"):
            result_file = line.replace("[RESULT_FILE]", "").strip()

        elif line.startswith("[FUNCTION_TO_KNOB]"):
            json_str = line.replace("[FUNCTION_TO_KNOB]", "").strip()
            try:
                function_to_knob = json.loads(json_str)
            except Exception:
                function_to_knob = {}
    if not result_file:
        raise HTTPException(
            status_code=500,
            detail="未从 analyzeException 输出中获取到结果文件路径"
        )

    # ---------- 4. 读取结果文件 ----------
    try:
        raw_text = read_file_from_container(CONTAINER, result_file)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"读取结果文件失败: {str(e)}"
        )

    lines = raw_text.strip().splitlines()
    if len(lines) <= 1:
        return {
            "taskName": taskName,
            "resultFile": result_file,
            "count": 0,
            "data": []
        }

    # ---------- 5. 解析 btFunctions.txt ----------
    # 约定格式：
    # Function\tSample Rate(%)\tDiff From Mean\tChange
    header = lines[0].split("\t")
    records = []

    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != len(header):
            continue

        record = {}
        for k, v in zip(header, parts):
            # 尝试数值化
            try:
                record[k] = float(v)
            except ValueError:
                record[k] = v

        records.append(record)

    # ---------- 6. TopN 截断 ----------
    if topN > 0:
        records = records[:topN]

    # ---------- 7. 返回 ----------
    return {
        "code": 200,
        "msg": "success",
        "data": [
            {
                "funcName": r.get("Function"),
                "sampleRate": r.get("Sample Rate(%)"),
                "diffFromMean": r.get("Diff From Mean"),
                "change": int(r.get("Change", 0)),
                "relatedKnobs": function_to_knob.get(
                    r.get("Function"), []
                )
            }
            for r in records
        ]
    }



@router.post("/api/tune/oneStep")
def start_tune(req: dict):
    """启动一步调优任务"""
    task_name = req.get("taskName", "")
    
    if not task_name:
        raise HTTPException(status_code=400, detail="taskName is required")
    
    # 检查当前状态
    current_status = TASK_STATUS.get(task_name, "idle")
    
    if current_status == "running":
        return {
            "code": 400,
            "msg": "任务正在运行中",
            "task_id": task_name,
            "status": "running"
        }
    
    # 如果之前已经成功，可以重新运行
    if current_status == "success":
        print(f"[INFO] 任务 {task_name} 之前已成功，重新运行")
    
    # 启动后台任务
    thread = threading.Thread(
        target=run_onesteptune_task,
        args=(task_name,),
        daemon=True
    )
    thread.start()
    
    # 注意：这里要等待一下，确保线程开始执行并设置了状态
    import time
    time.sleep(0.1)
    
    return {
        "code": 200,
        "msg": "调优任务已启动",
        "task_id": task_name,
        "status": "running"
    }
    

# @router.get("/api/tune/status")
# def get_tune_status(taskName: str):
#     task = TASK_STATUS.get(taskName)

#     if not task:
#         return {
#             "code": 200,
#             "msg": "success",
#             "status": "idle",
#             "task_id": taskName
#         }

#     return {
#         "code": 200,
#         "msg": "success",
#         "status": task["status"],
#         "task_id": taskName,
#         "error": task.get("error")
#     }



# # 原有的 run_onesteptune_task 函数
# def run_onesteptune_task(task_name: str):
#     ini_path = f"/home/sysinsight/DBTuner/config/{task_name}.ini"

#     TASK_STATUS[task_name] = {
#         "status": "running",
#         "error": None
#     }

#     try:
#         cmd = (
#             "bash -lc "
#             f"'set -e; "
#             f"source ~/.zshrc && "
#             f"cd /home/sysinsight && "
#             f"conda activate sysinsight && "
#             f"python3 oneStepTune.py "
#             f"--config={ini_path} "
#             f"--task_name={task_name}'"
#         )

#         exec_in_container(CONTAINER, cmd)

#         TASK_STATUS[task_name]["status"] = "success"

#     except Exception as e:
#         TASK_STATUS[task_name]["status"] = "failed"
#         TASK_STATUS[task_name]["error"] = str(e)
#         print(f"[ERROR] one-step tune failed: {task_name}", e)


# @router.post("/api/tune/oneStep")
# def start_tune(req: dict):
#     """
#     启动一步调优任务
#     """
#     task_name = req.get("taskName", "")
    
#     if not task_name:
#         raise HTTPException(status_code=400, detail="taskName is required")

#     if TASK_STATUS.get(task_name) == "running":
#         return {
#             "code": 400,
#             "msg": "任务正在运行中",
#             "task_id": task_name,
#             "status": "running"
#         }

#     # 导入 threading
#     import threading
    
#     # 后台线程启动
#     thread = threading.Thread(
#         target=run_onesteptune_task,
#         args=(task_name,),
#         daemon=True
#     )
#     thread.start()

#     TASK_STATUS[task_name] = "pending"

#     return {
#         "code": 200,
#         "msg": "调优任务已启动",
#         "task_id": task_name,
#         "status": "running"
#     }