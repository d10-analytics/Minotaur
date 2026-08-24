"""Shared source-discovery exclusions and root-relative ordering."""

from __future__ import annotations

from pathlib import Path

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", "__pycache__", ".venv", "venv"}
)


def is_excluded(relative: Path) -> bool:
    """Identify paths omitted from ordinary recursive source scans."""
    return any(part in _EXCLUDED_DIRECTORY_NAMES or part.startswith(".") for part in relative.parts)


def relative_order_key(path: Path, root: Path) -> str:
    """Return a root-relative POSIX path without resolving the filesystem entry."""
    root_text = str(root).rstrip("/\\")
    path_text = str(path)
    if path_text.startswith(root_text):
        suffix = path_text[len(root_text) :]
        if suffix.startswith(("/", "\\")):
            path_text = suffix[1:]
    return path_text.replace("\\", "/")
