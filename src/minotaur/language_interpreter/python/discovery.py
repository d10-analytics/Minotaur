"""Deterministic, local-only discovery of Python source files."""

from __future__ import annotations

from pathlib import Path

from minotaur.language_interpreter import exclusions
from minotaur.language_interpreter.workspace import Workspace


def discover_python_files(workspace: Workspace) -> tuple[Path, ...]:
    """Return regular ``.py`` files in canonical workspace-relative order."""
    files = (
        path
        for path in workspace.root.rglob("*.py")
        if path.is_file() and not exclusions.is_excluded(path.relative_to(workspace.root))
    )
    return tuple(
        sorted(files, key=lambda path: exclusions.relative_order_key(path, workspace.root))
    )
