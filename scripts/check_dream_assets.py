#!/usr/bin/env python3
"""Read-only verification of separately obtained, trusted DREAM model assets."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def verify(source,manifest):
    failures=[]
    for item in manifest["files"]:
        path=source/item["path"]
        if not path.is_file():
            failures.append(item["path"]+": missing");continue
        digest=hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda:stream.read(1024*1024),b""):digest.update(block)
        if path.stat().st_size!=item["bytes"] or digest.hexdigest()!=item["sha256"]:
            failures.append(item["path"]+": size/hash mismatch")
    return failures


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",default=str(ROOT/"dream"))
    args=parser.parse_args()
    manifest=json.loads((ROOT/"integrations/dream/assets.json").read_text())
    failures=verify(Path(args.source).resolve(),manifest)
    for message in failures:print("FAIL",message)
    if failures:raise SystemExit(1)
    print("Verified genuine external assets:",len(manifest["files"]))
    print("Integrity is not a license grant or a defense against malicious serialized objects.")


if __name__=="__main__":main()
