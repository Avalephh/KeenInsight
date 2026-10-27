#!/usr/bin/env python3
"""Fetch exact external DREAM/RCRank snapshots and apply reviewed patches.

Only run after reviewing upstream licensing. Never overwrites an existing,
unmarked checkout, downloads a model, touches PostgreSQL, or runs smoke data.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def git(destination,*args):
    return subprocess.check_output(["git","-C",str(destination),*args],text=True,timeout=120).strip()


def checkout(destination,record,transport="https"):
    if destination.exists():
        raise ValueError("Refusing to overwrite existing dependency: "+str(destination))
    destination.mkdir(parents=True)
    git(destination,"init","--quiet")
    repository=record["repository"]
    if transport=="ssh":
        if not repository.startswith("https://github.com/"):raise ValueError("SSH override is only supported for pinned GitHub sources")
        repository="git@github.com:"+repository[len("https://github.com/"):]
    git(destination,"remote","add","origin",repository)
    git(destination,"fetch","--quiet","--depth","1","origin",record["commit"])
    git(destination,"checkout","--quiet","--detach","FETCH_HEAD")
    if git(destination,"rev-parse","HEAD")!=record["commit"]:
        raise ValueError("Fetched source identity does not match lock")
    patch=ROOT/record["patch"]
    git(destination,"apply","--check",str(patch))
    git(destination,"apply",str(patch))


def portable_worker_defaults(destination):
    path=destination/"dream/runtime_config.py"
    text=path.read_text()
    for name,value in (("DEFAULT_API_BASE","https://api.example.com/v1"),
                       ("DEFAULT_MODEL","your-model-name")):
        text,count=re.subn(r"^"+name+r"\s*=.*$",name+" = "+json.dumps(value),text,flags=re.M)
        if count!=1:raise ValueError("Unexpected upstream runtime settings shape")
    path.write_text(text)
    # Existing config is not used: the runtime points to our portable template.
    # Rewrite the tool service description using paths from this checkout,
    # not the original workstation's absolute interpreter and working dir.
    tools=destination/"dream/agent/action/mcp_tools"
    config={"services":[{"name":"diagnose_tools","server_type":"stdio","params":{
        "command":str(destination/".venv/bin/python"),"args":[str(tools/"mcp_tools.py")],
        "cwd":str(tools)}}]}
    (tools/"action_server_config.yaml").write_text(json.dumps(config,indent=2)+"\n")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination",default=str(ROOT/"dream"))
    parser.add_argument("--transport",choices=["https","ssh"],default="https",
                        help="Explicit SSH alternative when HTTPS transport is unavailable")
    args=parser.parse_args()
    records=json.loads((ROOT/"integrations/sources.json").read_text())
    destination=Path(args.destination).absolute()
    marker=destination/".keeninsight-source.json"
    patch_hashes={name:hashlib.sha256((ROOT/item["patch"]).read_bytes()).hexdigest()
                  for name,item in records.items()}
    expected={"sources":records,"patchSha256":patch_hashes,"portabilityVersion":1}
    if destination.exists():
        if marker.is_file() and json.loads(marker.read_text())==expected:
            print("Accepted existing marked external integration:",destination);return
        raise ValueError("Refusing an existing unmarked or differently versioned dependency: "+str(destination))
    checkout(destination,records["dream"],args.transport)
    rcrank=destination/"dream/agent/plan/RCRank"
    if rcrank.exists():
        if any(rcrank.iterdir()):raise ValueError("Unexpected populated submodule directory")
        rcrank.rmdir()
    checkout(rcrank,records["rcrank"],args.transport)
    portable_worker_defaults(destination)
    marker.write_text(json.dumps(expected,indent=2)+"\n")
    print("Prepared pinned external integration (no models or data installed):",destination)


if __name__=="__main__":
    try:main()
    except (OSError,ValueError,subprocess.CalledProcessError) as error:
        print("Dependency setup failed: "+str(error),file=sys.stderr);raise SystemExit(1)
