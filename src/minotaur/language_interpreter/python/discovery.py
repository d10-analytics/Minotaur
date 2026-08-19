"""Deterministic, local-only discovery of Python source files."""

from __future__ import annotations

from pathlib import Path

from minotaur.language_interpreter.workspace import Workspace

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", "__pycache__", ".venv", "venv"}
)


def discover_python_files(workspace: Workspace) -> tuple[Path, ...]:
    """Return regular ``.py`` files in canonical workspace-relative order."""
    files = (
        path
        for path in workspace.root.rglob("*.py")
        if path.is_file()
        and not any(
            part in _EXCLUDED_DIRECTORY_NAMES or part.startswith(".")
            for part in path.relative_to(workspace.root).parts
        )
    )
    return tuple(sorted(files, key=lambda path: path.relative_to(workspace.root).as_posix()))
