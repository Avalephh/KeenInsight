import logging

from mcp.server.fastmcp import FastMCP

# 配置日志记录器
logging.basicConfig(
    level=logging.INFO,  # 设置日志级别为 INFO
    format="%(asctime)s - %(levelname)s - %(message)s",  # 日志格式
)
logger = logging.getLogger(__name__)

# 创建 FastMCP 实例
mcp = FastMCP("Math")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers"""
    logger.info("The add method is called: a=%d, b=%d", a, b)  # 记录加法调用日志
    return a + b


@mcp.tool()
def multiply(a: int, b: int) -> int:
    """Multiply two numbers"""
    logger.info("The multiply method is called: a=%d, b=%d", a, b)  # 记录乘法调用日志
    return a * b


if __name__ == "__main__":
    logger.info("Start math server through MCP")  # 记录服务启动日志
    mcp.run(transport="stdio")  # 启动服务并使用标准输入输出通信
