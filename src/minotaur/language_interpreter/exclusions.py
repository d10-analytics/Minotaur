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
    separator = "\\" if root.anchor.endswith("\\") else "/"
    root_text = str(root)
    path_text = str(path)
    prefix = root_text if root_text.endswith(separator) else f"{root_text}{separator}"
    if path_text.startswith(prefix):
        path_text = path_text[len(prefix) :]
    return path_text.replace("\\", "/") if separator == "\\" else path_text
