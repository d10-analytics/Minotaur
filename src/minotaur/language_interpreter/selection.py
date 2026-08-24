"""Validated, deterministic source selection for registered interpreters."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from minotaur.language_interpreter import exclusions
from minotaur.language_interpreter.registry import InterpreterRegistry
from minotaur.language_interpreter.workspace import Workspace


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
    return workspace, SourceSelection(
        tuple(
            sorted(
                selected.values(),
                key=lambda path: exclusions.relative_order_key(path, workspace.root),
            )
        )
    )


def _resolve_target(target: Path, root: Path) -> Path:
    """Resolve a target before containment checks to make symlinks visible."""
    if not target.exists():
        # Targets follow the usual CLI convention and resolve against the
        # working directory, not --root. The message spells that out because
        # `--root src minotaur` is a natural first attempt on a src/ layout.
        raise SelectionError(
            f"target does not exist: {target} (targets are resolved from the current "
            f"directory {Path.cwd()}, not from --root {root})"
        )
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

    Both arguments are normalized once with ``os.path.realpath`` so the result
    cannot depend on whether the caller already resolved them.  ``Workspace``
    resolves the root and ``_resolve_target`` resolves each target, so this is
    a no-op for ``select_sources``; it makes a direct call with a symlinked
    root behave like the same call with its physical path.  That single
    normalization is also what containment, relative-path, and ordering all
    read, so the root is never trimmed two different ways.

    Directory entries are pruned by unresolved name, so a symlink living inside
    an excluded or hidden directory is never resolved and never reported.  The
    pre-change implementation judged exclusion only after resolving, so such a
    link reported its physical target a second time; ``select_sources`` is
    unaffected because ``_add`` deduplicates on the root-relative path.
    """
    found: list[Path] = []
    # realpath leaves no trailing separator except on the filesystem root, so
    # the normalized text is directly usable as a containment prefix.
    root_text = os.path.realpath(os.fspath(root))
    root_path = Path(root_text)
    for current_dir, dirnames, filenames in os.walk(
        os.path.realpath(os.fspath(directory)), followlinks=False
    ):
        dirnames[:] = [
            name
            for name in dirnames
            if not os.path.islink(os.path.join(current_dir, name))
            and (include_excluded or not _is_excluded(Path(name)))
        ]
        for filename in filenames:
            candidate = os.path.join(current_dir, filename)
            resolved_text = os.path.realpath(candidate) if os.path.islink(candidate) else candidate
            if not os.path.isfile(resolved_text):
                continue
            if not _is_within_root(resolved_text, root_text):
                continue
            resolved = Path(resolved_text)
            if not include_excluded and _is_excluded(_relative_path(resolved_text, root_text)):
                continue
            found.append(resolved)
    return tuple(sorted(found, key=lambda path: exclusions.relative_order_key(path, root_path)))


def _is_excluded(relative: Path) -> bool:
    """Identify directories omitted from ordinary recursive source scans."""
    return exclusions.is_excluded(relative)


def _is_within_root(path: str, root: str) -> bool:
    """Test containment against the root normalized by ``_discover_directory``."""
    if root == os.sep:
        return path.startswith(os.sep)
    return path == root or path.startswith(f"{root}{os.sep}")


def _relative_path(path: str, root: str) -> Path:
    """Strip the same normalized root ``_is_within_root`` just matched."""
    return Path(path[len(root) :].lstrip(os.sep))


def _add(selected: dict[str, Path], root: Path, path: Path) -> None:
    # Root-relative POSIX paths are graph wire paths and deterministic sort
    # keys. They also let equivalent resolved paths overwrite one dictionary
    # entry, which is the desired deduplication behavior.
    selected[exclusions.relative_order_key(path, root)] = path
