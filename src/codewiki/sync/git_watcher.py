"""
Git Watcher — Python translation of codegraph incremental sync logic.

Provides:
- git diff --name-status for change detection
- non-ancestor commit detection (merge-base)
- commit pointer tracking via project_metadata
"""

from __future__ import annotations

import subprocess
import os
from typing import Optional


def get_current_commit(repo_path: str, branch: str) -> Optional[str]:
    """Get the current HEAD SHA of a branch (git rev-parse)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", branch],
            cwd=repo_path,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def get_last_indexed_commit(store) -> Optional[str]:
    """Read last_indexed_commit from project_metadata."""
    return store.get_metadata("last_indexed_commit")


def set_last_indexed_commit(store, commit: str) -> None:
    """Write last_indexed_commit to project_metadata."""
    store.set_metadata("last_indexed_commit", commit)


def is_ancestor(repo_path: str, ancestor: str, descendant: str) -> bool:
    """Check if `ancestor` is an ancestor of `descendant` (git merge-base --is-ancestor)."""
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo_path,
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def get_changed_files(
    repo_path: str, old_commit: str, new_commit: str
) -> list[tuple[str, str]]:
    """
    Get list of changed files between two commits (git diff --name-status).

    Returns list of (status, file_path) where status is 'A'/'M'/'D'/'R100'.
    Renamed files have status like 'R100' with old and new paths.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", old_commit, new_commit],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []

        files: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            if status.startswith("R"):
                # Rename: R100\told_path\tnew_path
                if len(parts) >= 3:
                    files.append(("D", parts[1]))  # Delete old
                    files.append(("A", parts[2]))  # Add new
            else:
                files.append((status, parts[1] if len(parts) > 1 else ""))
        return files
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_branch_name(repo_path: str) -> Optional[str]:
    """Get the current branch name (git rev-parse --abbrev-ref HEAD)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def init_git_repo(repo_path: str, branch: Optional[str] = None) -> Optional[str]:
    """
    Initialize git tracking for a project.
    Returns the branch name to track, or None if not a git repo.
    """
    branch_name = branch or get_branch_name(repo_path)
    if not branch_name:
        return None

    commit = get_current_commit(repo_path, branch_name)
    if not commit:
        return None

    return branch_name
