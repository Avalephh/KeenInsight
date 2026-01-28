import sys
from pathlib import Path

import openai

# 添加项目根目录到Python路径
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from config.base_config import API_SETTINGS

openai.api_key = API_SETTINGS["openai"]["api_key"]
openai.base_url = API_SETTINGS["openai"]["base_url"]
openai.default_headers = {"x-foo": "true"}


def get_root_cause_llm_instructions():
    """Get instructions for the root cause diagnosis LLM."""
    return """You are a database slow SQL root cause diagnosis expert.
    Only output the allowed root cause label(s) and their confidence in json format, nothing else."""


response = openai.chat.completions.create(
    model="gpt-4.1",
    messages=[
        {
            "role": "user",
            "content": "Hello world!",
        },
    ],
)
print(response.choices[0].message.content)

# 正常会输出结果：Hello there! How can I assist you today ?
