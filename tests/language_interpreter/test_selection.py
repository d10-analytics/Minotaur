"""Behavioral coverage for source selection and its shared exclusions."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from minotaur.language_interpreter import exclusions, selection
from minotaur.language_interpreter.python.discovery import discover_python_files
from minotaur.language_interpreter.registry import default_registry
from minotaur.language_interpreter.selection import _discover_directory, select_sources
from minotaur.language_interpreter.workspace import Workspace


def _write(root: Path, relative: str, content: str = "value = 1\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _oracle_is_excluded(relative: Path) -> bool:
    excluded = frozenset({".git", ".mypy_cache", ".pytest_cache", "__pycache__", ".venv", "venv"})
    return any(part in excluded or part.startswith(".") for part in relative.parts)


def _oracle_discover_directory(
    directory: Path, root: Path, include_excluded: bool
) -> tuple[Path, ...]:
    found: list[Path] = []
    for candidate in directory.rglob("*"):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if not include_excluded and _oracle_is_excluded(relative):
            continue
        found.append(resolved)
    return tuple(sorted(found, key=lambda path: path.relative_to(root).as_posix()))


def _oracle_select_sources(root: Path, targets: tuple[Path, ...] | None = None) -> tuple[Path, ...]:
    selected: dict[str, Path] = {}
    for target in targets or (root,):
        for candidate in _oracle_discover_directory(target, root, False):
            if candidate.suffix.lower() == ".py":
                selected[candidate.relative_to(root).as_posix()] = candidate
    return tuple(selected[key] for key in sorted(selected))


def _dedup(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Drop repeated entries, keeping first-occurrence order."""
    seen: dict[Path, None] = {}
    for path in paths:
        seen.setdefault(path, None)
    return tuple(seen)


def _is_subsequence(candidate: tuple[Path, ...], whole: tuple[Path, ...]) -> bool:
    """Report whether ``candidate`` is ``whole`` with entries only removed."""
    remaining = iter(whole)
    return all(item in remaining for item in candidate)


def _make_nested_excluded(root: Path) -> None:
    _write(root, "a/keep.py")
    _write(root, "a/__pycache__/b.py")
    _write(root, "a/.venv/c.py")


def _make_hidden_file(root: Path) -> None:
    _write(root, "pkg/.hidden.py")
    _write(root, "pkg/visible.py")


def _make_outside_symlink(root: Path) -> None:
    outside = root.parent / "outside.py"
    outside.write_text("outside = 1\n", encoding="utf-8")
    os.symlink(outside, root / "escape.py")


def _make_inside_symlink(root: Path) -> None:
    _write(root, "real.py")
    os.symlink(root / "real.py", root / "alias.py")


def _make_symlinked_directory(root: Path) -> None:
    _write(root, "real/hidden.py")
    os.symlink(root / "real", root / "a")


def _make_symlink_inside_hidden_directory(root: Path) -> None:
    _write(root, "pkg/real.py")
    (root / ".hidden").mkdir()
    os.symlink(root / "pkg/real.py", root / ".hidden/link.py")


def _make_symlink_inside_pycache(root: Path) -> None:
    _write(root, "pkg/real.py")
    (root / "__pycache__").mkdir()
    os.symlink(root / "pkg/real.py", root / "__pycache__/link.py")


def _make_broken_symlink(root: Path) -> None:
    os.symlink(root / "missing.py", root / "broken.py")
    _write(root, "kept.py")


def _make_non_ascii(root: Path) -> None:
    _write(root, "é.py")
    _write(root, "e\u0301.py")
    _write(root, "中.py")


def _make_empty(root: Path) -> None:
    (root / "empty").mkdir(parents=True)


def _make_include_excluded(root: Path) -> None:
    _make_nested_excluded(root)


_SHAPES: tuple[tuple[str, Callable[[Path], None], str, bool], ...] = (
    ("nested excluded directory", _make_nested_excluded, ".", False),
    ("nested excluded directory from a target", _make_nested_excluded, "a", False),
    ("hidden file component", _make_hidden_file, ".", False),
    ("outside symlink", _make_outside_symlink, ".", False),
    ("inside symlink", _make_inside_symlink, ".", False),
    ("symlinked directory", _make_symlinked_directory, ".", False),
    ("symlinked directory from a target", _make_symlinked_directory, "a", False),
    ("symlink inside hidden directory", _make_symlink_inside_hidden_directory, ".", False),
    ("symlink inside __pycache__", _make_symlink_inside_pycache, ".", False),
    ("broken symlink", _make_broken_symlink, ".", False),
    ("non-ASCII names", _make_non_ascii, ".", False),
    ("empty directory", _make_empty, ".", False),
    ("include excluded", _make_include_excluded, ".", True),
)


@pytest.mark.parametrize(
    ("name", "builder", "target_relative", "include_excluded"),
    _SHAPES,
    ids=[shape[0] for shape in _SHAPES],
)
def test_discover_directory_matches_verbatim_previous_implementation(
    tmp_path: Path,
    name: str,
    builder: Callable[[Path], None],
    target_relative: str,
    include_excluded: bool,
) -> None:
    del name
    root = tmp_path / "workspace"
    root.mkdir()
    builder(root)
    target = root / target_relative

    discovered = _discover_directory(target, root, include_excluded)
    oracle = _oracle_discover_directory(target, root, include_excluded)
    # Reviewed 2026-08-24: the pre-change walker judged exclusion on the
    # *resolved* path after descending, so a symlink living inside an excluded
    # or hidden directory reported its physical target a second time when the
    # scan started at the root.  Pruning by unresolved name drops that
    # duplicate.  The root rows pin the unchanged root-level result; the
    # ``root / "a"`` rows apply the same member, order, and deduplication
    # assertions to a selected subdirectory, including a symlink whose target
    # remains inside the workspace but outside that selected path.
    assert _dedup(discovered) == _dedup(oracle)
    assert _is_subsequence(discovered, oracle)
    _, selected = select_sources(root, (target,), default_registry())
    assert selected.files == _oracle_select_sources(root, (target,))


def test_symlink_inside_excluded_directory_loses_only_its_duplicate(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _make_symlink_inside_pycache(root)
    real = root / "pkg" / "real.py"

    assert _oracle_discover_directory(root, root, False) == (real, real)
    assert _discover_directory(root, root, False) == (real,)

    _, selected = select_sources(root, (root,), default_registry())
    assert selected.files == _oracle_select_sources(root) == (real,)


def test_symlink_only_reachable_file_is_dropped_for_a_subdirectory_target(
    tmp_path: Path,
) -> None:
    """The accepted divergence, stated at its true scope (spec addendum).

    With ``sub`` as the target, ``pkg/real.py`` is reachable only through
    ``sub/.hidden/link.py``.  The pre-change walker resolved the link and
    reported the file once; pruning by name never sees it.  ``select_sources``
    and ``drift().added`` over the recorded target ``sub`` therefore differ
    from the pre-change result by exactly that file, and by nothing else.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root, "pkg/real.py")
    _write(root, "sub/plain.py")
    hidden = root / "sub" / ".hidden"
    hidden.mkdir()
    os.symlink(root / "pkg" / "real.py", hidden / "link.py")
    real = root / "pkg" / "real.py"
    plain = root / "sub" / "plain.py"

    oracle = _oracle_discover_directory(root / "sub", root, False)
    assert oracle == (real, plain)
    assert _discover_directory(root / "sub", root, False) == (plain,)

    _, selected = select_sources(root, (root / "sub",), default_registry())
    assert selected.files == (plain,)
    assert tuple(sorted(set(oracle) - set(selected.files))) == (real,)

    # A root-level scan still reaches the physical file directly.
    _, whole = select_sources(root, (root,), default_registry())
    assert whole.files == (real, plain)


def test_relative_order_key_requires_an_absolute_root() -> None:
    with pytest.raises(ValueError, match="root must be absolute"):
        exclusions.relative_order_key(Path("rel/pkg/a.py"), Path("rel"))


def test_discover_directory_requires_an_absolute_root(tmp_path: Path) -> None:
    directory = tmp_path / "workspace"
    directory.mkdir()

    with pytest.raises(ValueError, match="root must be absolute"):
        _discover_directory(directory, Path("relative-root"), False)


def test_discover_directory_normalizes_an_unresolved_root(tmp_path: Path) -> None:
    physical = tmp_path / "physical"
    physical.mkdir()
    _write(physical, "pkg/module.py")
    linked = tmp_path / "linked"
    os.symlink(physical, linked)
    expected = (physical / "pkg" / "module.py",)

    # `select_sources` always resolves first (`Workspace`), so a symlinked root
    # only reaches `_discover_directory` through a direct call. Normalizing
    # both arguments makes that call agree with the physical one instead of
    # silently reporting nothing.
    assert _discover_directory(physical, physical, False) == expected
    assert _discover_directory(linked, linked, False) == expected

    _, selected = select_sources(linked, (linked,), default_registry())
    assert selected.files == expected


def test_relative_order_key_rejects_a_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    assert exclusions.relative_order_key(root / "pkg" / "module.py", root) == "pkg/module.py"
    # The key is the graph wire path and the deduplication key, so falling
    # through to an absolute path would name files by machine-specific paths.
    with pytest.raises(ValueError, match="not inside root"):
        exclusions.relative_order_key(tmp_path / "outside.py", root)
    with pytest.raises(ValueError, match="not inside root"):
        exclusions.relative_order_key(root, root)


@pytest.mark.skipif(os.name != "posix", reason="backslash is a valid POSIX filename character")
def test_literal_backslash_filename_keeps_distinct_root_relative_identity(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root, "a\\b.py")
    _write(root, "a/b.py")

    expected = _oracle_select_sources(root)
    _, selected = select_sources(root, (root,), default_registry())
    assert _discover_directory(root, root, False) == _oracle_discover_directory(root, root, False)
    assert selected.files == expected
    assert len(selected.files) == 2


@pytest.mark.skipif(os.name != "posix", reason="backslash is a valid POSIX filename character")
def test_root_name_ending_in_backslash_preserves_containment(tmp_path: Path) -> None:
    root = tmp_path / "workspace\\"
    root.mkdir()
    _write(root, "a.py")

    expected = _oracle_select_sources(root)
    _, selected = select_sources(root, (root,), default_registry())
    assert _discover_directory(root, root, False) == _oracle_discover_directory(root, root, False)
    assert selected.files == expected == (root / "a.py",)


def test_filesystem_root_containment_discovers_child_without_scanning_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    physical_directory = tmp_path / "workspace"
    physical_directory.mkdir()
    child = physical_directory / "child.py"
    child.write_text("value = 1\n", encoding="utf-8")
    logical_root = Path("/")

    def fake_walk(directory: str, *, followlinks: bool) -> object:
        assert directory == os.fspath(logical_root)
        assert followlinks is False
        yield os.fspath(physical_directory), [], [child.name]

    monkeypatch.setattr(selection.os, "walk", fake_walk)
    assert _discover_directory(logical_root, logical_root, False) == (child,)


def test_filesystem_root_containment_is_reached_by_select_sources(tmp_path: Path) -> None:
    child_directory = tmp_path / "workspace"
    child_directory.mkdir()
    child = child_directory / "child.py"
    child.write_text("value = 1\n", encoding="utf-8")

    _, selected = select_sources(Path("/"), (child_directory,), default_registry())

    assert selected.files == (child,)


def test_shared_exclusion_predicate_controls_both_walkers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root, "keep/b.py")
    _write(root, "zzz_marker/a.py")
    workspace = Workspace(root)

    assert discover_python_files(workspace) == (root / "keep/b.py", root / "zzz_marker/a.py")
    _, selected = select_sources(root, (root,), default_registry())
    assert selected.files == (root / "keep/b.py", root / "zzz_marker/a.py")

    def patched_is_excluded(relative: Path) -> bool:
        return "zzz_marker" in relative.parts

    monkeypatch.setattr(exclusions, "is_excluded", patched_is_excluded)
    assert discover_python_files(workspace) == (root / "keep/b.py",)
    _, selected = select_sources(root, (root,), default_registry())
    assert selected.files == (root / "keep/b.py",)


def test_excluded_directory_names_have_one_source_owner() -> None:
    source_root = Path(__file__).parents[2] / "src"
    occurrences = sum(
        '".mypy_cache"' in path.read_text(encoding="utf-8") for path in source_root.rglob("*.py")
    )
    assert occurrences == 1


@pytest.mark.parametrize(
    "first_import",
    ("minotaur.language_interpreter.selection", "minotaur.language_interpreter.python.discovery"),
)
def test_selection_and_discovery_import_in_either_order(first_import: str) -> None:
    second_import = (
        "minotaur.language_interpreter.python.discovery"
        if first_import.endswith("selection")
        else "minotaur.language_interpreter.selection"
    )
    source_root = Path(__file__).parents[2] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", f"import {first_import}; import {second_import}"],
        capture_output=True,
        cwd=source_root.parent,
        env=environment,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
