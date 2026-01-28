import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from agents import Agent, Runner
from agents._config import set_default_openai_api
from agents.mcp import MCPServerSse, MCPServerStdio

# 添加项目根目录到Python路径
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from config.base_config import API_SETTINGS

os.environ["OPENAI_API_KEY"] = API_SETTINGS["openai"]["api_key"]
os.environ["OPENAI_BASE_URL"] = API_SETTINGS["openai"]["base_url"]

# 设置使用 chat_completions API
set_default_openai_api("chat_completions")


async def run_multi_service_example(pg_server, math_server, weather_server):
    agent = Agent(
        name="MultiServiceAgent",
        model="gpt-4o-mini",
        instructions="""
        你是一个多功能助手，可以：
        1. 访问PostgreSQL数据库并执行SQL查询
        2. 执行数学计算（加法和乘法）
        3. 获取天气信息
        
        使用提供的工具来完成用户的请求。对于数据库操作：
        - 不要编造不存在的表或列
        - 只使用数据库中实际存在的表和列
        - 始终使用参数化查询来防止SQL注入
        """,
        mcp_servers=[pg_server, math_server, weather_server],
    )

    # 示例1：数据库查询
    message = "列出数据库中所有的表"
    print(f"\n执行操作: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(f"回答:\n{result.final_output}")

    # 示例2：数学计算
    message = "计算 23 + 45 的结果"
    print(f"\n执行操作: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(f"回答:\n{result.final_output}")

    # 示例3：天气查询
    message = "查询北京的天气"
    print(f"\n执行操作: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(f"回答:\n{result.final_output}")

    # 示例4：列出所有可用工具
    message = "列出你拥有的所有工具"
    print(f"\n执行操作: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(f"回答:\n{result.final_output}")


class ServerConfig:
    def __init__(self, name, server_type, params):
        self.name = name
        self.server_type = server_type
        self.params = params

    @classmethod
    def from_dict(cls, config):
        server_type = MCPServerStdio if config["server_type"] == "stdio" else MCPServerSse
        # 处理相对路径
        if "cwd" in config["params"] and config["params"]["cwd"] == ".":
            config["params"]["cwd"] = os.path.dirname(os.path.abspath(__file__))
        return cls(name=config["name"], server_type=server_type, params=config["params"])


class MultiServerManager:
    def __init__(self, configs):
        self.configs = configs
        self.servers = {}

    @asynccontextmanager
    async def manage_servers(self):
        try:
            for config in self.configs:
                self.servers[config.name] = await config.server_type(name=config.name, params=config.params).__aenter__()
            yield list(self.servers.values())
        finally:
            for server in self.servers.values():
                await server.__aexit__(None, None, None)


async def main():
    # 读取配置文件
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f)

    configs = [ServerConfig.from_dict(service_config) for service_config in config_data["services"]]

    manager = MultiServerManager(configs)
    async with manager.manage_servers() as servers:
        await run_multi_service_example(*servers)


if __name__ == "__main__":

    def delete_all_loggers():
        import logging

        loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
        for logger in loggers:
            handlers = logger.handlers[:]
            for handler in handlers:
                logger.removeHandler(handler)
            logger.propagate = True
            logger.setLevel(logging.CRITICAL)

    delete_all_loggers()
    asyncio.run(main())
