"""Deterministic, local-only discovery of Python source files."""

from __future__ import annotations

from pathlib import Path

from minotaur.language_interpreter.workspace import Workspace


def discover_python_files(workspace: Workspace) -> tuple[Path, ...]:
    """Return regular ``.py`` files in canonical workspace-relative order."""
    # The selection module imports the registry, which imports the Python
    # interpreter, which imports this module; importing it here keeps the
    # modules importable in either order.
    from minotaur.language_interpreter.registry import default_registry
    from minotaur.language_interpreter.selection import select_sources

    _, selection = select_sources(workspace.root, (workspace.root,), default_registry())
    return tuple(path for path in selection.files if path.suffix.lower() == ".py")
