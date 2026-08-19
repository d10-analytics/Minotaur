"""Validated, deterministic source selection for registered interpreters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from minotaur.language_interpreter.registry import InterpreterRegistry
from minotaur.language_interpreter.workspace import Workspace

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", "__pycache__", ".venv", "venv"}
)


class SelectionError(ValueError):
    """A requested source target cannot safely be analyzed.

    This separate error type lets the CLI distinguish invalid user selection
    from an interpreter diagnostic.  The former must stop before output is
    written; the latter can still yield a valid partial graph.
    """


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """Supported source files, represented by canonical resolved paths.

    Keeping paths resolved prevents the same file being analyzed twice through
    overlapping directories, repeated arguments, or an in-root symlink.
    """

    files: tuple[Path, ...]


def select_sources(
    root: Path, targets: tuple[Path, ...], registry: InterpreterRegistry
) -> tuple[Workspace, SourceSelection]:
    """Resolve and validate targets, returning supported files once each.

    This is intentionally the one place that walks user-provided paths.  Each
    interpreter can therefore concentrate on language semantics and trust that
    every supplied file is inside the workspace with a stable relative name.
    """
    workspace = Workspace(root)
    selected: dict[str, Path] = {}
    for target in targets:
        resolved = _resolve_target(target, workspace.root)
        if resolved.is_file():
            if not registry.supports(resolved):
                raise SelectionError(f"unsupported source file: {target}")
            _add(selected, workspace.root, resolved)
        elif resolved.is_dir():
            # Selecting a normally ignored directory is an explicit user
            # choice, so scan it.  A normal directory scan still avoids hidden
            # trees, caches, and virtual environments that are rarely source.
            direct_excluded = _is_excluded(resolved.relative_to(workspace.root))
            for candidate in _discover_directory(resolved, workspace.root, direct_excluded):
                if registry.supports(candidate):
                    _add(selected, workspace.root, candidate)
        else:  # pragma: no cover - Path types may be platform-specific.
            raise SelectionError(f"target is not a file or directory: {target}")
    return workspace, SourceSelection(tuple(selected[key] for key in sorted(selected)))


def _resolve_target(target: Path, root: Path) -> Path:
    """Resolve a target before containment checks to make symlinks visible."""
    if not target.exists():
        raise SelectionError(f"target does not exist: {target}")
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SelectionError(f"target escapes root: {target}") from error
    return resolved


def _discover_directory(directory: Path, root: Path, include_excluded: bool) -> tuple[Path, ...]:
    """Find in-root regular files while keeping recursive discovery predictable.

    A symlinked file can resolve outside ``root`` even when its link name looks
    safe.  It is skipped here; explicitly naming that same link is rejected by
    ``_resolve_target`` so the user receives a clear selection error.
    """
    found: list[Path] = []
    for candidate in directory.rglob("*"):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if not include_excluded and _is_excluded(relative):
            continue
        found.append(resolved)
    return tuple(sorted(found, key=lambda path: path.relative_to(root).as_posix()))


def _is_excluded(relative: Path) -> bool:
    """Identify directories omitted from ordinary recursive source scans."""
    return any(part in _EXCLUDED_DIRECTORY_NAMES or part.startswith(".") for part in relative.parts)


def _add(selected: dict[str, Path], root: Path, path: Path) -> None:
    # Root-relative POSIX paths are graph wire paths and deterministic sort
    # keys. They also let equivalent resolved paths overwrite one dictionary
    # entry, which is the desired deduplication behavior.
    selected[path.relative_to(root).as_posix()] = path
