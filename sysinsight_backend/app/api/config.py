from fastapi import APIRouter, HTTPException
from app.schemas.tune import SetConfigRequest
from app.services.docker_service import exec_in_container
import json
import base64

router = APIRouter()

CONTAINER = "ctt_sysinsight"
CONFIG_DIR = "/home/sysinsight/DBTuner/config/"
TEMPLATE_INI = f"{CONFIG_DIR}/config_template.ini"

@router.post("/api/tune/setConfig")
def set_config(req: SetConfigRequest):
    if not req.tune or not req.tune.tuneName:
        raise HTTPException(status_code=400, detail="tuneName is required")

    task_name = req.tune.tuneName
    task_ini = f"{CONFIG_DIR}/{task_name}.ini"

    try:
        # 检查是否已存在
        exec_in_container(CONTAINER, f"test ! -f {task_ini}")
        
        # 从模板复制
        exec_in_container(CONTAINER, f"cp {TEMPLATE_INI} {task_ini}")
        
        # 使用sed命令修改字段
        if req.tune.tuneName:
            exec_in_container(
                CONTAINER,
                f"sed -i 's/^task_id = .*/task_id = {req.tune.tuneName}/' {task_ini}"
            )
        
        if req.tune.rounds:
            exec_in_container(
                CONTAINER,
                f"sed -i 's/^max_runs = .*/max_runs = {req.tune.rounds}/' {task_ini}"
            )
        
        if req.tune.algorithm:
            exec_in_container(
                CONTAINER,
                f"sed -i 's/^optimize_method = .*/optimize_method = {req.tune.algorithm}/' {task_ini}"
            )
        
        if req.tune.metrics:
            # ['tps', '-lat'] 这种格式
            metrics_str = "[" + ", ".join(f"'{m}'" for m in req.tune.metrics) + "]"

            exec_in_container(
                CONTAINER,
                f'''sed -i "s/^performance_metric = .*/performance_metric = {metrics_str}/" {task_ini}'''
            )

        
        # 处理load部分
        if req.load:
            if req.load.loadType:
                # 修改workload字段
                exec_in_container(
                    CONTAINER,
                    f"sed -i 's/^workload = .*/workload = {req.load.loadType}/' {task_ini}"
                )
                
                # 根据负载类型设置对应的dbname
                if req.load.loadType == "sysbench":
                    dbname = "sbtest"
                elif req.load.loadType == "tpcc":
                    dbname = "benchbase"
                elif req.load.loadType == "tpch":
                    dbname = "tpch"
                elif req.load.loadType == "workload_zoo":
                    dbname = "workload_zoo_db"  # 根据需要调整
                elif req.load.loadType == "oltpbench_twitter":
                    dbname = "twitter"
                elif req.load.loadType == "oltpbench_ycsb":
                    dbname = "ycsb"
                else:
                    # 默认使用sbtest
                    dbname = "sbtest"
                
                # 修改dbname字段
                exec_in_container(
                    CONTAINER,
                    f"sed -i 's/^dbname = .*/dbname = {dbname}/' {task_ini}"
                )
            
            if req.load.duration:
                exec_in_container(
                    CONTAINER,
                    f"sed -i 's/^workload_time = .*/workload_time = {req.load.duration}/' {task_ini}"
                )
            
            if req.load.threadNum:
                exec_in_container(
                    CONTAINER,
                    f"sed -i 's/^thread_num = .*/thread_num = {req.load.threadNum}/' {task_ini}"
                )
        
        return {
            "code": 200,
            "msg": "配置创建成功",
            "task_id": task_name,
            "ini_file": f"{task_name}.ini"
        }

    except Exception as e:
        return {"code": 500, "msg": str(e)}