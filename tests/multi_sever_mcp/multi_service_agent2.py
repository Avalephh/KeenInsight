import asyncio
import os
import sys
from pathlib import Path

from agents import Agent, Runner
from agents._config import set_default_openai_api
from agents.mcp import MCPServerSse, MCPServerStdio

# 添加项目根目录到Python路径
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

# 在设置Python路径后导入config
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


# asyncio.gather的形式
async def start_servers():
    async def start_postgres():
        server = MCPServerStdio(
            name="Postgres",
            params={
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-postgres",
                    "postgresql://postgres:postgres@localhost:5432/postgres",
                ],
                "cwd": os.path.dirname(os.path.abspath(__file__)),
            },
        )
        await server.connect()
        return server

    async def start_math():
        server = MCPServerStdio(
            name="Math",
            params={
                "command": "python",
                "args": ["math_server.py"],
                "cwd": os.path.dirname(os.path.abspath(__file__)),
            },
        )
        await server.connect()
        return server

    async def start_weather():
        server = MCPServerSse(
            name="Weather",
            params={
                "command": "python",
                "args": ["weather_server.py"],
                "url": "http://localhost:8000/sse",
                "cwd": os.path.dirname(os.path.abspath(__file__)),
            },
        )
        await server.connect()
        return server

    pg_server, math_server, weather_server = await asyncio.gather(start_postgres(), start_math(), start_weather())
    return pg_server, math_server, weather_server


async def main():
    servers = await start_servers()
    try:
        await run_multi_service_example(*servers)
    finally:
        # Cleanup all servers
        for server in servers:
            try:
                await server.cleanup()
            except Exception as e:
                print(f"Error during cleanup: {e}")


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
