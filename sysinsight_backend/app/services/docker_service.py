import docker
import json
import os

client = docker.from_env()

def exec_in_container(container_name: str, cmd: str):
    """在容器内执行命令"""
    container = client.containers.get(container_name)
    result = container.exec_run(cmd, privileged=True)
    if result.exit_code != 0:
        raise RuntimeError(result.output.decode())
    return result.output.decode()

def read_file_from_container(container_name: str, file_path: str):
    """读取容器内的文件内容"""
    try:
        # 使用 cat 命令读取文件内容
        cmd = f"cat {file_path}"
        content = exec_in_container(container_name, cmd)
        return content
    except Exception as e:
        raise RuntimeError(f"读取容器文件失败: {str(e)}")

def read_json_from_container(container_name: str, file_path: str):
    """读取容器内的JSON文件"""
    content = read_file_from_container(container_name, file_path)
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"解析JSON失败: {str(e)}")

def file_exists_in_container(container_name: str, file_path: str):
    """检查容器内文件是否存在 - 改进版"""
    try:
        # 使用 ls 命令检查文件是否存在
        cmd = f"ls {file_path} 2>/dev/null && echo 'exists' || echo 'not exists'"
        result = exec_in_container(container_name, cmd).strip()
        return result == "exists"
    except Exception as e:
        print(f"检查文件存在性失败: {e}")
        return False

def list_directory_in_container(container_name: str, dir_path: str):
    """列出容器内目录内容 - 用于调试"""
    try:
        cmd = f"ls -la {dir_path} 2>/dev/null || echo '目录不存在'"
        return exec_in_container(container_name, cmd)
    except Exception as e:
        return f"列出目录失败: {e}"