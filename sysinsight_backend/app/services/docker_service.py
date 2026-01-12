import docker

client = docker.from_env()

def exec_in_container(container_name: str, cmd: str):
    container = client.containers.get(container_name)
    result = container.exec_run(cmd, privileged=True)
    if result.exit_code != 0:
        raise RuntimeError(result.output.decode())
    return result.output.decode()
