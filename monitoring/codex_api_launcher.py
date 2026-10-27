#!/usr/bin/env python3
"""Launch KeenInsight with the operator's selected Codex provider and API key.

Credentials are read at process start and passed only in the child environment.
No credentials are printed, copied into config JSON, or passed on argv.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ImportError:
    import tomli as tomllib


def load_settings(directory):
    directory=Path(directory).resolve()
    with (directory/"config.toml").open("rb") as handle:
        config=tomllib.load(handle)
    auth=json.loads((directory/"auth.json").read_text(encoding="utf-8"))
    provider_name=config.get("model_provider") or "openai"
    provider=(config.get("model_providers") or {}).get(provider_name) or {}
    base=provider.get("base_url") or "https://api.openai.com/v1"
    model=config.get("model")
    key=auth.get("OPENAI_API_KEY")
    if not isinstance(key,str) or not key.strip():
        raise ValueError("Codex auth.json must contain an API key; OAuth login tokens are not used as API keys")
    if not isinstance(model,str) or not model.strip():
        raise ValueError("Codex config.toml has no selected model")
    parsed=urlsplit(base)
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Codex provider base_url must be an HTTP(S) URL without embedded credentials")
    return {"api_key":key.strip(),"base_url":base.rstrip("/"),"model":model,
            "provider":provider_name,"codex_wire_api":provider.get("wire_api"),
            "config_source":str(directory/"config.toml")}


def environment(settings, inherited=None):
    env=dict(os.environ if inherited is None else inherited)
    for name in ["SYSINSIGHT_GPT_API_KEY","SYSINSIGHT_API_KEY","OPENAI_API_KEY"]:
        env[name]=settings["api_key"]
    for name in ["SYSINSIGHT_GPT_BASE_URL","SYSINSIGHT_API_BASE","OPENAI_API_BASE","OPENAI_BASE_URL"]:
        env[name]=settings["base_url"]
    for name in ["SYSINSIGHT_GPT_MODEL","SYSINSIGHT_API_MODEL","OPENAI_MODEL"]:
        env[name]=settings["model"]
    env["SYSINSIGHT_API_CONFIG_SOURCE"]=settings["config_source"]
    return env


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir",required=True)
    parser.add_argument("--check",action="store_true")
    parser.add_argument("command",nargs=argparse.REMAINDER)
    args=parser.parse_args()
    try:
        settings=load_settings(args.config_dir)
    except (OSError,ValueError) as exc:
        parser.exit(2,"Cannot load Codex API configuration: {}\n".format(type(exc).__name__))
    if args.check:
        print(json.dumps({k:v for k,v in settings.items() if k!="api_key"},ensure_ascii=False))
        return 0
    command=args.command[1:] if args.command[:1]==["--"] else args.command
    if not command:
        parser.error("a child command is required")
    os.execvpe(command[0],command,environment(settings))


if __name__=="__main__":
    main()
