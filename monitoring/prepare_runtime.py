#!/usr/bin/env python3
"""Build a persistent postgres-readable DREAM runtime, never synthetic assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
MARKER = ".keeninsight-runtime.json"


def make_public_directory(path):
    """Set traversal on newly created runtime parents, never on existing /root."""
    path = Path(path)
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        created.chmod(0o755)


def copy_file(source, target, executable=False):
    source, target = Path(source), Path(target)
    make_public_directory(target.parent)
    if target.is_symlink():
        raise ValueError("Refusing to overwrite a runtime symlink: " + str(target))
    if not target.exists() or (source.stat().st_size, source.stat().st_mtime_ns) != (
            target.stat().st_size, target.stat().st_mtime_ns):
        shutil.copy2(str(source), str(target), follow_symlinks=True)
    target.chmod(0o755 if executable else 0o644)


def copy_tree(source, target, excluded=()):
    """Incremental copies, not hard links: worker writes cannot alter checkout."""
    source, target = Path(source), Path(target)
    skipped = {".git", "__pycache__", ".pytest_cache", ".env", ".env.local",
               "auth.json", ".cache", *excluded}
    for base, directories, files in os.walk(str(source), followlinks=True):
        directories[:] = [name for name in directories if name not in skipped]
        destination = target / Path(base).relative_to(source)
        if destination.is_symlink():
            raise ValueError("Runtime directory must not be a symlink: " + str(destination))
        make_public_directory(destination)
        destination.chmod(0o755)
        for name in files:
            if name in skipped or name.endswith((".pyc", ".pem", ".key")):
                continue
            item = Path(base) / name
            if item.is_file():
                copy_file(item, destination / name, bool(item.stat().st_mode & 0o111))


def relocate_config(config, runtime, source):
    def relocate(value):
        if isinstance(value, dict):
            return {k: relocate(v) for k, v in value.items() if k.lower() != "api_key"}
        if isinstance(value, list):
            return [relocate(v) for v in value]
        if isinstance(value, str):
            if value.startswith("${DREAM_ROOT}/"):
                return str(runtime / value[len("${DREAM_ROOT}/"):])
            if value.startswith(str(source) + "/"):
                return str(runtime / value[len(str(source)) + 1:])
            # The checked-in example came from the original deployment.
            if "/dream/dream/" in value:
                return str(runtime / "dream" / value.split("/dream/dream/", 1)[1])
        return value
    result = relocate(config)
    result.setdefault("MEMORY_MANAGER_CONFIG", {}).update(
        db_path=str(runtime / "state/memory.json"),
        model_output_dir=str(runtime / "state/models"),
        samples_save_path=str(runtime / "state/retriever_samples.jsonl"))
    db = result.setdefault("DATABASE_CONFIG", {})
    for env, key in (("SYSINSIGHT_DB", "dbname"), ("SYSINSIGHT_DB_USER", "user"),
                     ("SYSINSIGHT_DB_HOST", "host"), ("SYSINSIGHT_DB_SCHEMA", "schema")):
        if os.environ.get(env):
            db[key] = os.environ[env]
    if os.environ.get("SYSINSIGHT_DB_PORT"):
        db["port"] = int(os.environ["SYSINSIGHT_DB_PORT"])
    db["search_path"] = [db.get("schema", "tpcds"), "public"]
    return result


def prepare(runtime, source, venv, config, worker):
    runtime, source, venv, config = [Path(p).absolute() for p in (runtime, source, venv, config)]
    if runtime.is_symlink() or runtime.resolve() in (Path("/"), Path("/root"), PROJECT, source):
        raise ValueError("Unsafe or source-overlapping runtime directory")
    if runtime.exists() and any(runtime.iterdir()) and not (runtime / MARKER).is_file():
        raise ValueError("Refusing to modify an unmarked nonempty runtime: " + str(runtime))
    if source in runtime.parents or runtime in source.parents:
        raise ValueError("Runtime and source must be disjoint")
    for item in (source / "dream/agent/db_agent.py", venv / "bin/python", config):
        if not item.is_file():
            raise ValueError("Missing deployment asset: " + str(item))
    original = json.loads(config.read_text())
    account = pwd.getpwnam(worker)
    metadata = json.loads(subprocess.check_output(
        [str(venv / "bin/python"), "-c",
         "import json,sys;print(json.dumps({'base':sys.base_prefix,'version':list(sys.version_info[:2])}))"],
        text=True, timeout=15))
    if metadata["version"][0] != 3 or metadata["version"][1] < 10:
        raise ValueError("DREAM requires a prepared Python 3.10+ environment")
    base = Path(metadata["base"])
    ancestor = runtime.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(str(ancestor)).free < 4 * 1024**3 and not (runtime / MARKER).exists():
        raise ValueError("At least 4 GiB free is required to create the runtime")
    make_public_directory(runtime)
    runtime.chmod(0o755)
    marker = {"source": str(source), "runtime": str(runtime), "worker": worker,
              "pythonVersion": metadata["version"], "status": "preparing"}
    if (runtime / MARKER).exists():
        previous = json.loads((runtime / MARKER).read_text())
        if previous["source"] != str(source) or previous["worker"] != worker:
            raise ValueError("Existing runtime belongs to a different source/worker")
    (runtime / MARKER).write_text(json.dumps(marker, indent=2))
    print("Preparing persistent DREAM runtime (incremental): " + str(runtime), flush=True)
    copy_tree(source / "dream", runtime / "dream", excluded=("res", "pretrain", "logs", "results"))
    # RCRank imports Alignment from pretrain.py, but its unrelated historical
    # pretraining checkpoints (~1 GiB) are not needed by the selected model.
    pretrain = source / "dream/agent/plan/RCRank/pretrain"
    if pretrain.is_dir():
        for code in pretrain.rglob("*.py"):
            copy_file(code, runtime / "dream/agent/plan/RCRank/pretrain" / code.relative_to(pretrain))
    copy_file(PROJECT / "app/dream_live_adapter.py",
              runtime / "app/dream_live_adapter.py")
    # Console scripts embed old absolute prefixes; the worker needs libraries
    # and one real interpreter, not those scripts or symlinks into /root.
    copy_tree(venv / "lib", runtime / ".venv/lib")
    copy_file(venv / "bin/python", runtime / ".venv/bin/python", executable=True)
    if (venv / "pyvenv.cfg").is_file():
        copy_file(venv / "pyvenv.cfg", runtime / ".venv/pyvenv.cfg")
    if str(base).startswith(("/root/", str(PROJECT))):
        copy_tree(base, runtime / "python310", excluded=("include", "share"))
    state = runtime / "state"
    state.mkdir(exist_ok=True)
    state.chmod(0o700)
    os.chown(str(state), account.pw_uid, account.pw_gid)
    generated = relocate_config(original, runtime, source)
    for section, field in (("PLANNER_CONFIG", "model_path"), ("PLANNER_CONFIG", "train_data_path"),
                           ("MEMORY_MANAGER_CONFIG", "retriever_model_path"),
                           ("MEMORY_MANAGER_CONFIG", "bert_model_path")):
        item = Path(generated[section][field])
        if not item.exists():
            raise ValueError("Missing real DREAM model/data asset: " + str(item))
    destination = runtime / "config.json"
    destination.write_text(json.dumps(generated, ensure_ascii=False, indent=2))
    destination.chmod(0o600)
    marker.update(status="ready", config=str(destination),
                  configSha256=hashlib.sha256(destination.read_bytes()).hexdigest())
    (runtime / MARKER).write_text(json.dumps(marker, indent=2))
    return marker


def verify(runtime, worker, load_model=False):
    """Exercise imports/real model as the actual worker, not as privileged root."""
    runtime = Path(runtime)
    config = json.loads((runtime / "config.json").read_text())
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    libraries = list((runtime / ".venv/lib").glob("python*/site-packages"))
    env["PYTHONPATH"] = os.pathsep.join([str(runtime), str(runtime / "app")] + [str(p) for p in libraries])
    if (runtime / "python310").is_dir():
        env["PYTHONHOME"] = str(runtime / "python310")
    code = "from dream.agent.db_agent import DBAgent; print('DREAM worker imports passed')"
    if load_model:
        code += "; import sys,json; from dream.agent.plan.online_predict import RCRankPredictor; RCRankPredictor(json.load(sys.stdin)); print('DREAM real model load passed')"
    command = [str(runtime / ".venv/bin/python"), "-c", code]
    if os.geteuid() == 0:
        command = ["runuser", "-u", worker, "--"] + command
    subprocess.run(command, input=json.dumps(config["PLANNER_CONFIG"]), text=True,
                   env=env, cwd="/", check=True, timeout=120 if load_model else 45)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default=os.environ.get("SYSINSIGHT_DREAM_RUNTIME_ROOT", "/var/lib/keeninsight/dream-runtime"))
    parser.add_argument("--source", default=str(PROJECT / "dream"))
    parser.add_argument("--venv", default=os.environ.get("DREAM_SOURCE_VENV", str(PROJECT / "dream/.venv")))
    parser.add_argument("--config", default=os.environ.get("DREAM_SOURCE_CONFIG", str(PROJECT / "config/dream.example.json")))
    parser.add_argument("--worker", default=os.environ.get("SYSINSIGHT_DREAM_RUN_AS", "postgres"))
    parser.add_argument("--verify", action="store_true", help="Import as the real worker, without LLM/SQL")
    parser.add_argument("--load-model", action="store_true", help="Also load real RCRank/BERT assets")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.runtime, args.source, args.venv, args.config, args.worker)))
        if args.verify or args.load_model:
            verify(args.runtime, args.worker, args.load_model)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, "DREAM runtime preparation failed: {}\n".format(exc))


if __name__ == "__main__":
    main()
