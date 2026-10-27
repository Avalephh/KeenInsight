#!/usr/bin/env python3
"""Process birth-token checks for this checkout's PID files; no PID-only kills."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import time


def identity(pid):
    try:
        base = Path("/proc") / str(int(pid))
        raw = (base / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        command = (base / "cmdline").read_bytes()
        return {"pid": int(pid), "birth": fields[19],
                "commandSha256": hashlib.sha256(command).hexdigest()}
    except (OSError, ValueError, IndexError):
        return None


def matches(pid_file):
    try:
        path = Path(pid_file)
        saved = json.loads(path.with_suffix(".identity.json").read_text())
        pid = int(path.read_text().strip())
        return saved if saved == identity(pid) else None
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stamp", "check", "stop"])
    parser.add_argument("pid_file")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    path = Path(args.pid_file)
    if args.action == "stamp":
        value = identity(args.pid)
        if not value:
            return 1
        path.write_text(str(args.pid) + "\n")
        path.with_suffix(".identity.json").write_text(json.dumps(value))
        return 0
    value = matches(path)
    if args.action == "check":
        return 0 if value else 1
    if not value:
        print("No verified live process for " + path.stem)
        return 0
    os.kill(value["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if identity(value["pid"]) != value:
            print("Stopped " + path.stem)
            return 0
        time.sleep(.2)
    print("Process did not stop; no forced kill: " + path.stem)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
