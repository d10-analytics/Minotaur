"""Shared source-discovery exclusions and root-relative ordering."""

from __future__ import annotations

import os
from pathlib import Path

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", "__pycache__", ".venv", "venv"}
)


def is_excluded(relative: Path) -> bool:
    """Identify paths omitted from ordinary recursive source scans."""
    return any(part in _EXCLUDED_DIRECTORY_NAMES or part.startswith(".") for part in relative.parts)


def relative_order_key(path: Path, root: Path) -> str:
    """Return a root-relative POSIX path without resolving the filesystem entry.

    The result is the graph wire path and the deduplication key, so an
    out-of-root ``path`` is a caller error rather than something to paper over
    with an absolute fallback: two files could then collide or be named by
    machine-specific paths.  Callers establish containment first.

    Preconditions, which ``Path.relative_to`` did not have: ``root`` must be
    absolute (a relative root has an empty anchor, so the separator would be
    guessed) and ``path`` must have been produced from the same normalized
    text as ``root`` -- ``Workspace`` resolves the root and the walkers derive
    every candidate from it.  On case-insensitive filesystems the prefix test
    is case-folded so a differently-cased spelling of the same root does not
    raise, while the returned key keeps the caller's own spelling.
    """
    if not root.is_absolute():
        raise ValueError(f"root must be absolute: {root}")
    separator = "\\" if root.anchor.endswith("\\") else "/"
    root_text = str(root)
    path_text = str(path)
    prefix = root_text if root_text.endswith(separator) else f"{root_text}{separator}"
    if not os.path.normcase(path_text).startswith(os.path.normcase(prefix)):
        raise ValueError(f"path is not inside root: {path_text} (root {root_text})")
    path_text = path_text[len(prefix) :]
    return path_text.replace("\\", "/") if separator == "\\" else path_text
