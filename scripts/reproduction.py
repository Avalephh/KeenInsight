#!/usr/bin/env python3
"""Prepare, verify and install a portable, allowlisted offline dependency cache.

No database, system package manager, service or credential configuration is
modified. Cache hashes detect corruption, not an untrusted cache publisher.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "keeninsight.offline-cache.v1"
MANIFEST = "manifest.json"
LOCKS = (
    "external-sources.lock", "postgresql-source.lock", "integrations/sources.json",
    "integrations/reproduction-sources.json", "integrations/dream/assets.json",
    "integrations/dream/local-integration.patch",
    "integrations/dream/rcrank-compatibility.patch",
    "requirements-sysinsight.txt", "requirements-dream.lock.txt",
    "requirements-dev.txt", "monitoring/versions.lock",
)
WHEEL_GROUPS = ("sysinsight", "dream", "dev")


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def relative_path(text):
    if not isinstance(text, str):
        raise ValueError("Unsafe relative path")
    path = PurePosixPath(text)
    if (not text or path.is_absolute()
            or any(p in {"", ".", ".."} for p in text.split("/"))
            or "\\" in text or "\x00" in text):
        raise ValueError("Unsafe relative path")
    return Path(*path.parts)


def within(path, base):
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def beneath(base, text):
    base = Path(base).resolve()
    path = base / relative_path(text)
    for ancestor in (path, *path.parents):
        if ancestor == base:
            break
        if ancestor.is_symlink():
            raise ValueError("Symlinks are not accepted: " + text)
    if not within(path.resolve(), base):
        raise ValueError("Path escapes its root")
    return path


def lock_identity():
    return {name: digest(ROOT / name) for name in LOCKS}


def specs():
    result = {}

    def source(name, repository, commit, destination, patch=None):
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Source must have an exact commit: " + name)
        if not repository.startswith("https://github.com/"):
            raise ValueError("Only explicit pinned GitHub repositories are supported")
        slug = repository[len("https://github.com/"):]
        if slug.endswith(".git"):
            slug = slug[:-4]
        result[name] = {
            "kind": "source", "path": "sources/" + name + ".tar.gz",
            "url": "https://codeload.github.com/" + slug + "/tar.gz/" + commit,
            "commit": commit, "destination": destination, "patch": patch,
            "archiveRoot": slug.split("/")[-1] + "-" + commit,
        }

    for line in (ROOT / "external-sources.lock").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        branch, commit = line.split()
        source("sysinsight-" + branch, "https://github.com/Avalephh/KeenInsight",
               commit, "third_party/sysinsight/" + branch)
    records = json.loads((ROOT / "integrations/sources.json").read_text())
    for name, record in records.items():
        source(name, record["repository"], record["commit"],
               "dream" if name == "dream" else "dream/dream/agent/plan/RCRank",
               record["patch"])
    for name, record in json.loads(
            (ROOT / "integrations/reproduction-sources.json").read_text())["sources"].items():
        source(name, record["repository"], record["commit"], record["destination"])
    for line in (ROOT / "postgresql-source.lock").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, ref, commit, archive, url, sha = line.split()
            result[name] = {
                "kind": "source", "path": "sources/" + archive, "url": url,
                "sha256": sha, "commit": commit, "patch": None,
                "destination": "third_party/postgresql-12.22",
                "archiveRoot": "postgresql-12.22",
            }
    for line in (ROOT / "monitoring/versions.lock").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, version, archive, url, sha = line.split()
            result["monitoring-" + name] = {
                "kind": "monitoring", "path": "monitoring/" + archive,
                "url": url, "sha256": sha,
                "destination": "monitoring/downloads/" + archive,
            }
    return result


def models():
    return json.loads((ROOT / "integrations/dream/assets.json").read_text())["files"]


def save(cache, manifest):
    temporary = cache / ".manifest.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary.replace(cache / MANIFEST)


def load(cache, create=False):
    if cache.is_symlink():
        raise ValueError("Refusing a symlink cache")
    path = cache / MANIFEST
    if path.is_file():
        value = json.loads(path.read_text())
        if value.get("schema") != FORMAT or value.get("locks") != lock_identity():
            raise ValueError("Cache format/lock identity differs from this checkout")
        if not isinstance(value.get("files"), list):
            raise ValueError("Invalid cache file list")
        return value
    if not create:
        raise ValueError("No cache manifest")
    if cache.exists() and any(cache.iterdir()):
        raise ValueError("Refusing a nonempty unmarked cache")
    cache.mkdir(parents=True, exist_ok=True)
    value = {"schema": FORMAT, "locks": lock_identity(), "files": []}
    save(cache, value)
    return value


def record(cache, manifest, name, kind, path):
    entry = {"name": name, "kind": kind, "path": path.relative_to(cache).as_posix(),
             "bytes": path.stat().st_size, "sha256": digest(path)}
    manifest["files"] = [e for e in manifest["files"] if e["path"] != entry["path"]]
    manifest["files"].append(entry)
    save(cache, manifest)


def copy_verified(source, target, expected=None):
    if source.is_symlink() or not source.is_file():
        raise ValueError("Expected a regular trusted input file: " + str(source))
    sha = digest(source)
    if expected and sha != expected:
        raise ValueError("Input SHA256 mismatch: " + source.name)
    if target.exists():
        if not target.is_symlink() and digest(target) == sha:
            return
        raise ValueError("Refusing to replace a different existing file: " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as inp, target.open("xb") as out:
        shutil.copyfileobj(inp, out, 1024 * 1024)
    if digest(target) != sha:
        raise ValueError("Copy integrity failure")


def download_file(url, target, expected=None):
    if target.exists():
        if target.is_symlink() or (expected and digest(target) != expected):
            raise ValueError("Existing download hash/type mismatch; inspect/move it first")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    if temporary.exists():
        raise ValueError("Incomplete download exists; inspect/move it first: " + str(temporary))
    # HTTPS verification is never disabled. Failed transfers retain .partial.
    request = Request(url, headers={"User-Agent": "KeenInsight-reproduction/1"})
    with urlopen(request, timeout=60) as inp, temporary.open("xb") as out:
        shutil.copyfileobj(inp, out, 1024 * 1024)
    if expected and digest(temporary) != expected:
        raise ValueError("Download SHA256 mismatch: " + target.name)
    temporary.replace(target)


def download(args):
    cache = Path(args.directory).absolute()
    manifest = load(cache, create=True)
    kinds = {"source", "monitoring"} if args.group == "all" else {args.group}
    for name, spec in specs().items():
        if spec["kind"] not in kinds:
            continue
        target = beneath(cache, spec["path"])
        print("Preparing", name, flush=True)
        existing = next((e for e in manifest["files"] if e["path"] == spec["path"]), None)
        download_file(spec["url"], target, spec.get("sha256") or
                      (existing["sha256"] if existing else None))
        # Check archive readability before sealing it, not merely HTTP 200.
        validate_archive(target, spec)
        record(cache, manifest, name, spec["kind"], target)
    print("Cache prepared; models, wheelhouses and OS packages are separate inputs:", cache)


def validate_archive(path, spec):
    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        roots = {PurePosixPath(m.name).parts[0] for m in members if PurePosixPath(m.name).parts}
        if spec.get("archiveRoot") and roots != {spec["archiveRoot"]}:
            raise ValueError("Archive root differs from pinned upstream commit/version")


def import_archive(args):
    cache = Path(args.directory).absolute()
    manifest = load(cache, create=True)
    known = specs()
    if args.name not in known:
        raise ValueError("Unknown pinned artifact name")
    spec = known[args.name]
    source = Path(args.from_file).absolute()
    validate_archive(source, spec)
    target = beneath(cache, spec["path"])
    copy_verified(source, target, spec.get("sha256"))
    record(cache, manifest, args.name, spec["kind"], target)
    print("Imported pinned artifact:", args.name)
    print("Trust the archive provider; local hashes alone do not authenticate it.")


def add_models(args):
    cache = Path(args.directory).absolute()
    manifest = load(cache, create=True)
    source = Path(args.from_dir).resolve()
    for item in models():
        exact = beneath(source, item["path"])
        candidates = [exact] if exact.is_file() else list(source.rglob(Path(item["path"]).name))
        found = None
        for path in candidates:
            if (not path.is_symlink() and within(path.resolve(), source)
                    and path.is_file() and path.stat().st_size == item["bytes"]
                    and digest(path) == item["sha256"]):
                found = path
                break
        if found is None:
            raise ValueError("Missing genuine model asset: " + item["path"])
        target = beneath(cache, "models/" + item["path"])
        copy_verified(found, target, item["sha256"])
        record(cache, manifest, item["path"], "model", target)
        print("Verified model:", item["path"], flush=True)
    print("Models sealed. This does not grant redistribution rights.")


def download_bert(args):
    """Public BERT only; each allowed byte sequence is pinned by assets.json."""
    output = Path(args.output_dir).absolute()
    if output.is_symlink():
        raise ValueError("Refusing a symlink model output")
    output.mkdir(parents=True, exist_ok=True)
    prefix = "dream/agent/plan/RCRank/bert-base-uncased/"
    for item in models():
        if not item["path"].startswith(prefix):
            continue
        filename = Path(item["path"]).name
        # 'main' is only a discovery URL. Content is fail-closed against the
        # committed SHA256, not accepted because the mutable URL returned 200.
        url = "https://huggingface.co/google-bert/bert-base-uncased/resolve/main/" + filename
        print("Preparing official BERT asset:", filename, flush=True)
        download_file(url, beneath(output, filename), item["sha256"])
    print("BERT files verified; add-models still requires the three genuine DREAM assets.")


def seal_wheels(args):
    cache = Path(args.directory).absolute()
    manifest = load(cache, create=True)
    for group in WHEEL_GROUPS:
        directory = beneath(cache, "wheelhouses/" + group)
        wheels = sorted(directory.glob("*.whl"))
        if not wheels:
            raise ValueError("Missing wheelhouse: " + group)
        for path in wheels:
            if path.is_symlink():
                raise ValueError("Wheel symlink refused")
            record(cache, manifest, group + "/" + path.name, "wheel", path)
    metadata = beneath(cache, "wheelhouses/platform.json")
    if not metadata.is_file():
        raise ValueError("Missing wheel build platform metadata; use prepare_wheelhouses.sh")
    record(cache, manifest, "wheel-platform", "wheel-metadata", metadata)
    print("Wheelhouse hashes sealed; verify dependency completeness with offline-check.")


def verify(cache, manifest, require_complete=False):
    known = specs()
    model_entries = {item["path"]: item for item in models()}
    seen = set()
    for entry in manifest["files"]:
        path = beneath(cache, entry["path"])
        if entry["path"] in seen:
            raise ValueError("Duplicate manifest path")
        seen.add(entry["path"])
        kind, name = entry["kind"], entry["name"]
        if kind in {"source", "monitoring"}:
            expected = known.get(name, {})
            if expected.get("path") != entry["path"] or expected.get("kind") != kind:
                raise ValueError("Unknown dependency entry")
            if expected.get("sha256") and entry["sha256"] != expected["sha256"]:
                raise ValueError("Hash differs from committed dependency lock")
        elif kind == "model":
            expected = model_entries.get(name, {})
            if (entry["path"] != "models/" + name or expected.get("sha256") != entry["sha256"]
                    or expected.get("bytes") != entry["bytes"]):
                raise ValueError("Unknown/different model entry")
        elif kind == "wheel":
            parts = relative_path(entry["path"]).parts
            if (len(parts) != 3 or parts[0] != "wheelhouses" or parts[1] not in WHEEL_GROUPS
                    or not parts[2].endswith(".whl") or name != "/".join(parts[1:])):
                raise ValueError("Invalid wheel entry")
        elif kind == "wheel-metadata":
            if entry["path"] != "wheelhouses/platform.json" or name != "wheel-platform":
                raise ValueError("Invalid wheel metadata")
        else:
            raise ValueError("Unknown dependency kind")
        if not path.is_file() or path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError("Missing/corrupt artifact: " + entry["path"])
    # Detect accidental secrets, stale/unsealed files and partial transfers.
    for path in cache.rglob("*"):
        if path.is_symlink():
            raise ValueError("Cache contains a symlink")
        if path.is_file() and path.relative_to(cache).as_posix() not in seen | {MANIFEST}:
            raise ValueError("Unsealed file in cache: " + str(path.relative_to(cache)))
    if require_complete:
        needed = {s["path"] for s in known.values()}
        needed.update("models/" + p for p in model_entries)
        needed.add("wheelhouses/platform.json")
        if needed - seen:
            raise ValueError("Incomplete full cache: " + ", ".join(sorted(needed - seen)))
        for group in WHEEL_GROUPS:
            if not any(e["kind"] == "wheel" and e["name"].startswith(group + "/")
                       for e in manifest["files"]):
                raise ValueError("Missing wheel group: " + group)
    print("Verified cache:", len(seen), "files")


def extract_source(archive_path, destination, maximum_bytes=2 * 1024 ** 3):
    """Strip one upstream root; reject links, traversal and special files."""
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        roots = {PurePosixPath(m.name).parts[0] for m in members if PurePosixPath(m.name).parts}
        if len(roots) != 1:
            raise ValueError("Archive must contain exactly one upstream root")
        total = 0
        paths = set()
        for member in members:
            relative_path(member.name.rstrip("/"))
            parts = PurePosixPath(member.name).parts
            if not member.isdir() and not member.isfile():
                raise ValueError("Archive links/special files are refused: " + member.name)
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError("Unexpected root file")
                continue
            relative = "/".join(parts[1:])
            if relative in paths:
                raise ValueError("Duplicate archive member")
            paths.add(relative)
            total += member.size
            if total > maximum_bytes:
                raise ValueError("Source archive expands beyond safety limit")
        for member in members:
            parts = PurePosixPath(member.name).parts
            if len(parts) <= 1:
                continue
            path = beneath(destination, "/".join(parts[1:]))
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as inp, path.open("xb") as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)


def install_sources(args):
    cache = Path(args.directory).absolute()
    target_root = Path(args.target_root).absolute()
    if target_root.is_symlink() or not target_root.is_dir():
        raise ValueError("--target-root must be an existing real directory")
    manifest = load(cache)
    verify(cache, manifest)
    entries = {e["name"]: e for e in manifest["files"] if e["kind"] == "source"}
    known = specs()
    for name in args.only or list(entries):
        if name not in entries:
            raise ValueError("Source not present in cache: " + name)
        entry, spec = entries[name], known[name]
        destination = beneath(target_root, spec["destination"])
        identity = {"cacheSha256": entry["sha256"], "commit": spec["commit"],
                    "locks": manifest["locks"]}
        marker = destination / ".keeninsight-offline-source.json"
        if destination.exists():
            if marker.is_file() and json.loads(marker.read_text()) == identity:
                print("Accepted existing offline source:", name)
                continue
            # Upstream may leave an empty submodule directory.
            if destination.is_dir() and not any(destination.iterdir()):
                destination.rmdir()
            else:
                raise ValueError("Refusing existing/unmarked source: " + str(destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".keeninsight-source-", dir=destination.parent))
        try:
            extract_source(beneath(cache, entry["path"]), stage)
            if spec.get("patch"):
                # A dedicated temporary repo prevents git applying to an ancestor checkout.
                subprocess.run(["git", "init", "--quiet", str(stage)], check=True)
                patch = str(ROOT / spec["patch"])
                for options in (["--check"], []):
                    subprocess.run(["git", "-C", str(stage), "apply", *options, patch], check=True)
                shutil.rmtree(stage / ".git")
            if name == "dream":
                from fetch_dream_source import portable_worker_defaults
                portable_worker_defaults(stage)
                tools = stage / "dream/agent/action/mcp_tools/action_server_config.yaml"
                tools.write_text(tools.read_text().replace(str(stage), str(destination)))
            if name == "postgresql":
                (stage / ".gitrevision").write_text(spec["commit"] + "\n")
            (stage / marker.name).write_text(json.dumps(identity, indent=2) + "\n")
            stage.rename(destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        print("Installed pinned source:", name, flush=True)
    # Only explicitly sealed models and monitoring archives may be copied.
    for entry in manifest["files"]:
        if entry["kind"] == "model" and not args.only:
            target = beneath(target_root, "dream/" + entry["name"])
        elif entry["kind"] == "monitoring" and not args.only:
            target = beneath(target_root, known[entry["name"]]["destination"])
        else:
            continue
        copy_verified(beneath(cache, entry["path"]), target, entry["sha256"])
    print("No DB/service changed. Build tools, install Python/OS prerequisites and configure privately.")


def offline_check(args):
    cache = Path(args.directory).absolute()
    manifest = load(cache)
    verify(cache, manifest, require_complete=True)
    requirements = {"sysinsight": "requirements-sysinsight.txt",
                    "dream": "requirements-dream.lock.txt", "dev": "requirements-dev.txt"}
    # Metadata/hash completeness alone is not a resolver/ABI check.
    for group, requirement in requirements.items():
        subprocess.run([
            args.python, "-m", "pip", "install", "--dry-run", "--ignore-installed",
            "--no-index", "--only-binary=:all:", "--find-links",
            str(cache / "wheelhouses" / group), "-r", str(ROOT / requirement),
        ], check=True, env={**os.environ, "PIP_NO_INDEX": "1",
                           "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    print("Offline dependency resolution passed for this interpreter/platform.")
    print("LLM reachability, trusted model inference, DB workload and performance are NOT tested here.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="List exact downloads and manual prerequisites")
    plan.set_defaults(handler=lambda a: print(json.dumps(
        {"downloads": specs(), "manualModels": models(),
         "manualPrerequisites": ["matching Linux/Python/OS development packages",
                                 "private LLM API or reachable internal model service",
                                 "fresh dedicated PostgreSQL database"]}, indent=2)))
    bert = commands.add_parser("download-bert", help="Download public BERT files with pinned content hashes")
    bert.add_argument("--output-dir", required=True)
    bert.add_argument("--accept-upstream-terms", action="store_true", required=True)
    bert.set_defaults(handler=download_bert)
    for name, handler in (("download", download), ("import-archive", import_archive),
                          ("add-models", add_models),
                          ("seal-wheels", seal_wheels), ("verify", None),
                          ("install", install_sources), ("offline-check", offline_check)):
        command = commands.add_parser(name)
        command.add_argument("--directory", required=True, help="Trusted dependency cache directory")
        command.set_defaults(handler=handler)
        if name == "download":
            command.add_argument("--group", choices=["source", "monitoring", "all"], default="all")
            command.add_argument("--accept-upstream-terms", action="store_true", required=True)
        if name == "add-models":
            command.add_argument("--from-dir", required=True)
        if name == "import-archive":
            command.add_argument("--from-file", required=True)
            command.add_argument("--name", required=True, choices=list(specs()))
        if name == "verify":
            command.add_argument("--require-complete", action="store_true")
            command.set_defaults(handler=lambda a: verify(Path(a.directory).absolute(),
                load(Path(a.directory).absolute()), a.require_complete))
        if name == "install":
            command.add_argument("--only", nargs="+", help="Install only named source archives")
            command.add_argument("--target-root", default=str(ROOT),
                                 help="Existing new checkout directory; never point at a running deployment")
        if name == "offline-check":
            command.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print("Reproduction preparation failed:", str(exc), file=sys.stderr)
        raise SystemExit(1)
