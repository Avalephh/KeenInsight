"""Runtime settings shared by DREAM entry points.

SysInsight exposes its OpenAI-compatible endpoint through the
``SYSINSIGHT_GPT_*`` environment variables.  DREAM used to overwrite those
values with empty strings from its example JSON, which made the two projects
behave differently.  Keep secrets out of checked-in configuration and resolve
the same environment variables at runtime.
"""

import os


DEFAULT_API_BASE = "http://35.212.195.134:28317/v1"
DEFAULT_MODEL = "gpt-5.6-sol"


def resolve_openai_settings(api_settings=None):
    """Return the effective API key, base URL, and model without logging them."""
    openai_config = (api_settings or {}).get("openai") or {}

    # Environment variables win over checked-in examples so SysInsight and
    # DREAM always use the same endpoint/model when they are launched from the
    # same service.  Keep both legacy SysInsight names for compatibility.
    api_key = (
        os.getenv("SYSINSIGHT_GPT_API_KEY")
        or os.getenv("SYSINSIGHT_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or openai_config.get("api_key")
        or ""
    )
    base_url = (
        os.getenv("SYSINSIGHT_GPT_BASE_URL")
        or os.getenv("SYSINSIGHT_API_BASE")
        or os.getenv("OPENAI_API_BASE")
        or os.getenv("OPENAI_BASE_URL")
        or openai_config.get("base_url")
        or DEFAULT_API_BASE
    )
    model = (
        os.getenv("SYSINSIGHT_GPT_MODEL")
        or os.getenv("SYSINSIGHT_API_MODEL")
        or os.getenv("OPENAI_MODEL")
        or openai_config.get("model")
        or DEFAULT_MODEL
    )
    return {"api_key": api_key, "base_url": base_url, "model": model}


def configure_openai(api_settings=None):
    """Configure the OpenAI-compatible client and return effective settings."""
    from agents._config import set_default_openai_api

    settings = resolve_openai_settings(api_settings)
    if settings["api_key"]:
        os.environ["OPENAI_API_KEY"] = settings["api_key"]
    if settings["base_url"]:
        os.environ["OPENAI_BASE_URL"] = settings["base_url"]
    set_default_openai_api("chat_completions")
    return settings
