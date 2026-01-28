import asyncio
import os

from agents import Agent, Runner
from agents._config import set_default_openai_api
from pydantic import BaseModel

# 设置大模型API
os.environ["OPENAI_API_KEY"] = ""
os.environ["OPENAI_BASE_URL"] = ""
set_default_openai_api("chat_completions")


class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


class FinalResult(BaseModel):
    answer: str
    temperatures: float
    question_is_weather_related: bool


agent = Agent(
    name="weather助手",
    model="gpt-4o-mini",
    instructions="You are a helpful agent.",
    output_type=FinalResult,
    tools=[get_weather],
)


async def main():
    result = await Runner.run(agent, input="What's the weather in Tokyo?")
    print(result.final_output)
    print(type(result.final_output))

    result = await Runner.run(agent, input="非常简洁地介绍一下秦始皇")
    print(result.final_output)
    print(type(result.final_output))  # 返回的 final_output 的type is FinalResult
    # The weather in Tokyo is sunny.


if __name__ == "__main__":
    asyncio.run(main())
