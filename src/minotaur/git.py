"""Small, tolerant Git probes shared by configuration and CLI owners."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def run_git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a Git probe, treating unavailable or failed execution as unknown."""
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def work_tree_root(start: Path) -> Path | None:
    """Return the enclosing work-tree root, or ``None`` for an unknown probe."""
    completed = run_git(start, ("rev-parse", "--show-toplevel"))
    if completed is None or completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value).resolve() if value else None


def read_head_blob(root: Path, relative_path: str) -> bytes | None:
    """Read exact bytes from ``HEAD``; ``None`` means no such committed path.

    Git command/probe failures are also represented as ``None``. Callers that
    already established a work tree treat that as a strict artifact error;
    callers deciding whether Git is available use :func:`work_tree_root`.
    """
    try:
        completed = subprocess.run(
            ["git", "show", f"HEAD:{relative_path}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
