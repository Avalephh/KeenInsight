import json
import openai

openai.api_key = "sk-56iJn1M0lHeFNs6KFcB732EdD3A149479218D7456e3cB3C3"

openai.base_url = "https://api.gpt.ge/v1/"
openai.default_headers = {"x-foo": "true"}

def extract_json_from_llm(content: str) -> str:
    content = content.strip()

    if content.startswith("{") and content.endswith("}"):
        return content

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    brace_match = re.search(r"(\{.*\})", content, re.DOTALL)
    if brace_match:
        return brace_match.group(1).strip()

    raise ValueError(f"Cannot extract JSON from LLM output:\n{content}")

def llm_predict_performance_delta(
    base_config: dict,
    new_config: dict,
    base_score: float,
    model: str = "gpt-4o-mini"
):
    prompt = f"""
You are an expert in database performance tuning.

We have a baseline database configuration and its measured performance score.
The new configuration has NOT been executed yet.

Baseline configuration:
{json.dumps(base_config, indent=2)}

Baseline performance score:
{base_score}

New configuration:
{json.dumps(new_config, indent=2)}

Respond ONLY with a JSON object:
{{
  "delta_score": <float>,
  "reasoning": "<short technical explanation>"
}}
"""

    completion = openai.ChatCompletion.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )

    raw_content = completion.choices[0].message.content
    clean_content = extract_json_from_llm(raw_content)

    try:
        result = json.loads(clean_content)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"JSON parse failed.\n"
            f"Raw:\n{raw_content}\n\n"
            f"Extracted:\n{clean_content}"
        ) from e

    if "delta_score" not in result:
        raise ValueError(f"Missing delta_score in LLM output: {result}")

    delta = float(result["delta_score"])
    predicted_score = base_score + delta

    return {
        "delta_score": float(delta),
        "predicted_score": float(predicted_score),
        "reasoning": str(result.get("reasoning", ""))
    }