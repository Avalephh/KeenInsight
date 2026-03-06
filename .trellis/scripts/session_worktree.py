#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Session-scoped worktree helper for Trellis workflows.

Usage:
    python3 session_worktree.py open <task-dir> [--branch <branch>] [--base-branch <branch>]
    python3 session_worktree.py land [task-dir] [--keep-branch]

`open` creates or reuses a dedicated worktree for the given task, syncs the
task directory into that worktree, and sets `.current-task` in both the source
repo and the worktree.

`land` fast-forwards the task branch into the task's base branch, removes the
worktree, deletes the task branch by default, and archives the task metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# IMPORTANT: Force stdout to use UTF-8 on Windows
if sys.platform == "win32":
    import io as _io

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    elif hasattr(sys.stdout, "detach"):
        sys.stdout = _io.TextIOWrapper(sys.stdout.detach(), encoding="utf-8", errors="replace")  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).parent))

from common.git_context import _run_git_command
from common.paths import (
    DIR_WORKFLOW,
    FILE_CURRENT_TASK,
    FILE_TASK_JSON,
    clear_current_task,
    get_current_task,
    get_repo_root,
    get_tasks_dir,
    set_current_task,
)
from common.task_utils import archive_task_complete, find_task_by_name
from common.worktree import get_worktree_base_dir, get_worktree_copy_files


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def log_info(message: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {message}")


def log_success(message: str) -> None:
    print(f"{Colors.GREEN}[OK]{Colors.NC} {message}")


def log_warn(message: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {message}")


def log_error(message: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.NC} {message}", file=sys.stderr)


def _read_json_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json_file(path: Path, data: dict) -> bool:
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except (OSError, IOError):
        return False


def _slugify(value: str) -> str:
    result = value.lower().strip()
    result = re.sub(r"^[0-9]{2}-[0-9]{2}-", "", result)
    result = re.sub(r"[^a-z0-9]+", "-", result)
    return result.strip("-")


def _resolve_task_dir(task_input: str | None, repo_root: Path) -> Path:
    if task_input:
        candidate = Path(task_input)
        if candidate.is_absolute():
            return candidate

        relative_candidate = repo_root / task_input
        if relative_candidate.is_dir():
            return relative_candidate

        found = find_task_by_name(task_input, get_tasks_dir(repo_root))
        if found:
            return found

        return relative_candidate

    current_task = get_current_task(repo_root)
    if current_task:
        return repo_root / current_task

    raise FileNotFoundError("No task specified and no current task is set")


def _derive_branch(task_dir: Path, task_data: dict) -> str:
    existing = str(task_data.get("branch") or "").strip()
    if existing:
        return existing

    raw = (
        str(task_data.get("id") or "").strip()
        or str(task_data.get("name") or "").strip()
        or task_dir.name
    )
    slug = _slugify(raw) or _slugify(task_dir.name) or "task"
    return f"task/{slug}"


def _get_current_branch(repo_root: Path) -> str:
    _, branch_out, _ = _run_git_command(["branch", "--show-current"], cwd=repo_root)
    return branch_out.strip() or "main"


def _ensure_local_branch_exists(repo_root: Path, branch: str) -> bool:
    ret, _, _ = _run_git_command(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
    )
    return ret == 0


def _ensure_clean_worktree(repo_root: Path, label: str) -> bool:
    ret, status_out, err = _run_git_command(["status", "--porcelain"], cwd=repo_root)
    if ret != 0:
        log_error(f"Failed to inspect {label}: {err.strip() or repo_root}")
        return False
    dirty_lines = [line for line in status_out.splitlines() if line.strip()]
    if dirty_lines:
        log_error(f"{label} has uncommitted tracked changes; commit or stash first")
        return False
    return True


def _copy_config_files(source_repo_root: Path, worktree_root: Path) -> int:
    copy_count = 0
    for item in get_worktree_copy_files(source_repo_root):
        if not item:
            continue

        source = source_repo_root / item
        target = worktree_root / item
        if not source.is_file():
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(target))
        copy_count += 1

    return copy_count


def _sync_task_dir(source_task_dir: Path, worktree_root: Path, task_dir_relative: str) -> None:
    target_dir = worktree_root / task_dir_relative
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(str(source_task_dir), str(target_dir))


def _set_worktree_current_task(worktree_root: Path, task_dir_relative: str) -> None:
    workflow_dir = worktree_root / DIR_WORKFLOW
    workflow_dir.mkdir(parents=True, exist_ok=True)
    current_task_file = workflow_dir / FILE_CURRENT_TASK
    current_task_file.write_text(task_dir_relative, encoding="utf-8")


def _archive_task(task_dir: Path, repo_root: Path) -> None:
    if not task_dir.is_dir():
        return

    result = archive_task_complete(task_dir, repo_root)
    archived_to = result.get("archived_to")
    if archived_to:
        archived_path = Path(archived_to)
        log_success(f"Archived task to archive/{archived_path.parent.name}/{archived_path.name}/")


def cmd_open(args: argparse.Namespace) -> int:
    repo_root = get_repo_root()

    try:
        task_dir_abs = _resolve_task_dir(args.task_dir, repo_root)
    except FileNotFoundError as exc:
        log_error(str(exc))
        return 1

    if not task_dir_abs.is_dir():
        log_error(f"Task not found: {args.task_dir}")
        return 1

    try:
        task_dir_relative = str(task_dir_abs.relative_to(repo_root))
    except ValueError:
        log_error("Task directory must be inside the source repository")
        return 1

    task_json_path = task_dir_abs / FILE_TASK_JSON
    task_data = _read_json_file(task_json_path)
    if not task_data:
        log_error(f"Failed to read task.json: {task_json_path}")
        return 1

    branch = args.branch or _derive_branch(task_dir_abs, task_data)
    base_branch = args.base_branch or str(task_data.get("base_branch") or "").strip() or _get_current_branch(repo_root)

    if not _ensure_local_branch_exists(repo_root, base_branch):
        log_error(f"Base branch does not exist locally: {base_branch}")
        return 1

    worktree_path_value = str(task_data.get("worktree_path") or "").strip()
    worktree_root = Path(worktree_path_value).expanduser() if worktree_path_value else None

    if worktree_root and worktree_root.is_dir():
        worktree_root = worktree_root.resolve()
        log_info(f"Reusing existing worktree: {worktree_root}")
    else:
        worktree_base = get_worktree_base_dir(repo_root).resolve()
        worktree_base.mkdir(parents=True, exist_ok=True)
        worktree_root = (worktree_base / branch).resolve()
        worktree_root.parent.mkdir(parents=True, exist_ok=True)

        if _ensure_local_branch_exists(repo_root, branch):
            log_info(f"Adding existing branch worktree: {branch}")
            ret, _, err = _run_git_command(["worktree", "add", str(worktree_root), branch], cwd=repo_root)
        else:
            log_info(f"Creating new branch and worktree: {branch} <- {base_branch}")
            ret, _, err = _run_git_command(
                ["worktree", "add", "-b", branch, str(worktree_root), base_branch],
                cwd=repo_root,
            )

        if ret != 0:
            log_error(f"Failed to create worktree: {err.strip()}")
            return 1

        log_success(f"Created worktree: {worktree_root}")

    task_data["task_dir"] = task_dir_relative
    task_data["branch"] = branch
    task_data["base_branch"] = base_branch
    task_data["worktree_path"] = str(worktree_root)
    task_data["source_repo_root"] = str(repo_root)
    if task_data.get("status") not in ("completed", "rejected"):
        task_data["status"] = "in_progress"

    if not _write_json_file(task_json_path, task_data):
        log_error("Failed to update task.json before syncing worktree")
        return 1

    copied = _copy_config_files(repo_root, worktree_root)
    if copied > 0:
        log_success(f"Copied {copied} config file(s)")

    _sync_task_dir(task_dir_abs, worktree_root, task_dir_relative)
    _set_worktree_current_task(worktree_root, task_dir_relative)
    set_current_task(task_dir_relative, repo_root)

    log_success(f"Current task set to: {task_dir_relative}")
    print(str(worktree_root))
    return 0


def cmd_land(args: argparse.Namespace) -> int:
    repo_root = get_repo_root()

    try:
        task_dir_abs = _resolve_task_dir(args.task_dir, repo_root)
    except FileNotFoundError as exc:
        log_error(str(exc))
        return 1

    if not task_dir_abs.is_dir():
        log_error(f"Task not found: {args.task_dir or '(current task)'}")
        return 1

    task_json_path = task_dir_abs / FILE_TASK_JSON
    task_data = _read_json_file(task_json_path)
    if not task_data:
        log_error(f"Failed to read task.json: {task_json_path}")
        return 1

    branch = str(task_data.get("branch") or "").strip()
    base_branch = str(task_data.get("base_branch") or "").strip()
    task_dir_relative = str(task_data.get("task_dir") or "").strip()
    source_repo_root_value = str(task_data.get("source_repo_root") or "").strip()
    worktree_path_value = str(task_data.get("worktree_path") or "").strip()

    if not branch:
        log_error("Task branch is not set")
        return 1

    if not base_branch:
        log_error("Task base_branch is not set")
        return 1

    source_repo_root = Path(source_repo_root_value).expanduser().resolve() if source_repo_root_value else repo_root.resolve()
    source_task_dir = source_repo_root / task_dir_relative if task_dir_relative else None
    worktree_root = Path(worktree_path_value).expanduser().resolve() if worktree_path_value else repo_root.resolve()

    if not source_repo_root.is_dir():
        log_error(f"Source repo root not found: {source_repo_root}")
        return 1

    os.chdir(source_repo_root)

    if worktree_root.is_dir() and worktree_root != source_repo_root:
        if not _ensure_clean_worktree(worktree_root, "Task worktree"):
            return 1

    if not _ensure_clean_worktree(source_repo_root, "Source repo"):
        return 1

    if not _ensure_local_branch_exists(source_repo_root, base_branch):
        log_error(f"Base branch does not exist locally: {base_branch}")
        return 1

    if not _ensure_local_branch_exists(source_repo_root, branch):
        log_error(f"Task branch does not exist locally: {branch}")
        return 1

    log_info(f"Checking out base branch: {base_branch}")
    ret, _, err = _run_git_command(["checkout", base_branch], cwd=source_repo_root)
    if ret != 0:
        log_error(f"Failed to checkout {base_branch}: {err.strip()}")
        return 1

    log_info(f"Fast-forward merging {branch} -> {base_branch}")
    ret, _, err = _run_git_command(["merge", "--ff-only", branch], cwd=source_repo_root)
    if ret != 0:
        log_error(f"Fast-forward merge failed: {err.strip()}")
        log_warn("Rebase or merge manually, then run land again")
        return 1

    log_success(f"Merged {branch} into {base_branch}")

    today = datetime.now().strftime("%Y-%m-%d")
    if source_task_dir and source_task_dir.is_dir():
        source_task_json = source_task_dir / FILE_TASK_JSON
        source_task_data = _read_json_file(source_task_json) or {}
        source_task_data["status"] = "completed"
        source_task_data["completedAt"] = today
        source_task_data["worktree_path"] = None
        if not args.keep_branch:
            source_task_data["branch_deleted"] = True
        _write_json_file(source_task_json, source_task_data)

    if worktree_root.is_dir() and worktree_root != source_repo_root:
        log_info(f"Removing worktree: {worktree_root}")
        ret, _, err = _run_git_command(["worktree", "remove", str(worktree_root), "--force"], cwd=source_repo_root)
        if ret != 0:
            log_error(f"Failed to remove worktree: {err.strip()}")
            return 1
        log_success("Removed worktree")
    else:
        log_warn("No separate worktree found; skipping worktree removal")

    if not args.keep_branch:
        log_info(f"Deleting branch: {branch}")
        ret, _, err = _run_git_command(["branch", "-D", branch], cwd=source_repo_root)
        if ret != 0:
            log_warn(f"Failed to delete branch {branch}: {err.strip()}")
        else:
            log_success(f"Deleted branch: {branch}")

    if task_dir_relative and get_current_task(source_repo_root) == task_dir_relative:
        clear_current_task(source_repo_root)

    if source_task_dir and source_task_dir.is_dir():
        _archive_task(source_task_dir, source_repo_root)

    log_success("Landing complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Session-scoped worktree helper")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    open_parser = subparsers.add_parser("open", help="Create or reuse a task worktree")
    open_parser.add_argument("task_dir", help="Task directory path or task name")
    open_parser.add_argument("--branch", help="Override feature branch name")
    open_parser.add_argument("--base-branch", help="Override base branch name")

    land_parser = subparsers.add_parser("land", help="Merge task branch and remove its worktree")
    land_parser.add_argument("task_dir", nargs="?", help="Task directory path or task name")
    land_parser.add_argument("--keep-branch", action="store_true", help="Keep task branch after landing")

    args = parser.parse_args()

    if args.command == "open":
        return cmd_open(args)
    if args.command == "land":
        return cmd_land(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
