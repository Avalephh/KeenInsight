import logging

from mcp.server.fastmcp import FastMCP

# 配置日志记录器
logging.basicConfig(
    level=logging.INFO,  # 设置日志级别为 INFO
    format="%(asctime)s - %(levelname)s - %(message)s",  # 日志格式
)
logger = logging.getLogger(__name__)

mcp = FastMCP("Weather")


@mcp.tool()
async def get_weather(location: str) -> str:
    """Get weather for location."""
    logger.info("The get_weather method is called: location=%s", location)
    return "天气阳光明媚，晴空万里。"


if __name__ == "__main__":
    logger.info("Start weather server through MCP")  # 记录服务启动日志
    mcp.run(transport="sse")
