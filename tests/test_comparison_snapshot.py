from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import minotaur.comparison_snapshot as snapshots
import minotaur.git as git_module
from minotaur import cli
from minotaur.comparison_snapshot import (
    CaptureSelection,
    CaptureStage,
    RevisionSnapshot,
    SnapshotError,
    SnapshotMutationError,
    capture_pair,
)
from minotaur.git import GitInputError
from minotaur.language_interpreter import exclusions
from minotaur.language_interpreter.registry import default_registry
from minotaur.language_interpreter.selection import select_sources

Plan = Callable[[CaptureStage, CaptureStage], tuple[CaptureSelection, None]]

ALL = CaptureSelection(trees={".": True})


def _run(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _init(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "--quiet")
    _run(root, "config", "user.email", "tests@example.invalid")
    _run(root, "config", "user.name", "snapshot tests")
    return root


def _commit(
    root: Path, message: str = "commit", *, force: bool = False, stage_all: bool = True
) -> str:
    if stage_all:
        _run(root, "add", "--all", *(("--force",) if force else ()))
    _run(root, "commit", "--quiet", "--allow-empty", "-m", message)
    return _run(root, "rev-parse", "HEAD")


def _write(root: Path, relative: str, text: str = "value = 1\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = _init(tmp_path / "repo")
    (root / "app.py").write_text("old = True\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "README").write_text("old\n", encoding="utf-8")
    old = _commit(root, "old")
    (root / "app.py").write_text("new = True\n", encoding="utf-8")
    new = _commit(root, "new")
    return root, old, new


def _checkout_state(root: Path) -> tuple[str, str, str, str]:
    return (
        _run(root, "rev-parse", "HEAD"),
        _run(root, "ls-files", "--stage"),
        _run(root, "status", "--porcelain=v1"),
        _run(root, "config", "--local", "--list", "--null"),
    )


def _plan(selection: CaptureSelection) -> Plan:
    return lambda before, after: (selection, None)


def _pair(
    root: Path,
    before: str,
    after: str | None,
    selection: CaptureSelection = ALL,
    plan: Plan | None = None,
) -> tuple[RevisionSnapshot, RevisionSnapshot]:
    first, second, _ = capture_pair(root, before, after, plan=plan or _plan(selection))
    return first, second


def _manifest_paths(snapshot: RevisionSnapshot) -> dict[str, str]:
    return {entry.path: entry.kind for entry in snapshot.manifest.entries}


def _manifest_view(snapshot: RevisionSnapshot) -> dict[str, tuple[str, int, int, str]]:
    return {
        entry.path: (entry.kind, entry.mode, entry.size, entry.digest)
        for entry in snapshot.manifest.entries
    }


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point temporary directories at a root the test can inspect."""
    path = tmp_path / "capture-tmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def _leftovers(temp_root: Path) -> list[str]:
    return sorted(
        item.name
        for item in temp_root.iterdir()
        if item.name.startswith(("minotaur-revision-", "minotaur-working-"))
    )


class _Recorder:
    """Pass-through recorder of the Git probes made while it is installed."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        hook: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []
        original = git_module.run_git

        def recording(root: Path, arguments: Any, **kwargs: Any) -> Any:
            self.calls.append((tuple(arguments), kwargs.get("input")))
            if hook is not None:
                hook(tuple(arguments))
            return original(root, arguments, **kwargs)

        monkeypatch.setattr(git_module, "run_git", recording)

    def subcommands(self, name: str) -> list[tuple[str, ...]]:
        return [arguments for arguments, _ in self.calls if arguments[0] == name]

    def shape(self) -> list[tuple[str, ...]]:
        """The call sequence with object ids and SHAs replaced."""
        return [
            tuple(re.sub(r"[0-9a-f]{40}", "<id>", part) for part in arguments)
            for arguments, _ in self.calls
        ]

    def batch_input(self) -> bytes:
        return b"".join(data or b"" for arguments, data in self.calls if arguments[0] == "cat-file")


def test_capture_pins_each_side_and_cleans_temporary_roots(tmp_path: Path) -> None:
    root, old, new = _repository(tmp_path)
    before_state = _checkout_state(root)

    first, second = _pair(root, old, "HEAD", CaptureSelection(files={"app.py"}))
    first_root, second_root = first.root, second.root
    try:
        assert first.commit == old
        assert second.commit == new
        assert first.path("app.py").read_text(encoding="utf-8") == "old = True\n"
        assert second.path("app.py").read_text(encoding="utf-8") == "new = True\n"
        assert first_root != second_root
        first.verify_unchanged()
        second.verify_unchanged()
    finally:
        first.close()
        second.close()

    assert not first_root.exists()
    assert not second_root.exists()
    assert _checkout_state(root) == before_state


def test_capture_rejects_mutation_and_still_cleans_up(tmp_path: Path) -> None:
    root, old, _ = _repository(tmp_path)
    snapshot, other = _pair(root, old, old, CaptureSelection(files={"app.py"}))
    temporary_root = snapshot.root
    other.close()
    snapshot.path("app.py").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(SnapshotMutationError, match="app.py"):
        snapshot.close()

    assert not temporary_root.exists()


def test_capture_pair_closes_first_side_when_second_side_is_missing(
    tmp_path: Path, temp_root: Path
) -> None:
    root, old, _ = _repository(tmp_path)

    with pytest.raises(Exception) as error:
        _pair(root, old, "missing-revision")

    assert getattr(error.value, "side", None) == "after"
    assert _leftovers(temp_root) == []


def test_capture_pair_removes_first_stage_when_second_stage_cannot_open(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, old, _ = _repository(tmp_path)
    original = tempfile.TemporaryDirectory
    opened: list[object] = []

    def second_fails(*args: Any, **kwargs: Any) -> Any:
        if opened:
            raise OSError("simulated temporary directory failure")
        opened.append(object())
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots.tempfile, "TemporaryDirectory", second_fails)

    with pytest.raises(SnapshotError) as error:
        _pair(root, old, "HEAD")

    assert error.value.side == "after"
    assert "simulated temporary directory failure" in str(error.value)
    assert _leftovers(temp_root) == []


def test_capture_preserves_tracked_bytes_despite_archive_attributes(tmp_path: Path) -> None:
    root = _init(tmp_path / "repo")
    omitted = _write(root, "omitted.py", "omitted = True\n")
    substituted = _write(root, "substituted.py", "commit = $Format:%H$\n")
    _write(root, ".gitattributes", "omitted.py export-ignore\nsubstituted.py export-subst\n")
    _commit(root, "attributes")

    snapshot, other = _pair(
        root, "HEAD", "HEAD", CaptureSelection(files={"omitted.py", "substituted.py"})
    )
    try:
        assert snapshot.path("omitted.py").read_bytes() == omitted.read_bytes()
        assert snapshot.path("substituted.py").read_bytes() == substituted.read_bytes()
    finally:
        snapshot.close()
        other.close()


def test_capture_rejects_gitlink_entries_instead_of_materializing_empty_directories(
    tmp_path: Path,
) -> None:
    root = _init(tmp_path / "repo")
    nested = _init(tmp_path / "nested")
    (nested / "README").write_text("nested\n", encoding="utf-8")
    nested_commit = _commit(nested, "nested")
    (root / "app.py").write_text("app = True\n", encoding="utf-8")
    _run(root, "add", "--all")
    _run(root, "update-index", "--add", "--cacheinfo", f"160000,{nested_commit},vendor")
    _run(root, "commit", "--quiet", "-m", "gitlink")

    with pytest.raises(SnapshotError, match="unsafe entry.*vendor"):
        _pair(root, "HEAD", "HEAD", ALL)


def test_capture_attributes_in_selection_missing_object_to_side(
    tmp_path: Path, temp_root: Path
) -> None:
    """A blob inside the selection that cannot be read is a side error."""
    root = _init(tmp_path / "repo")
    _write(root, "inside.py", "inside = 'unique inside'\n")
    _write(root, "outside.py", "outside = 'unique outside'\n")
    commit = _commit(root)
    inside = _run(root, "rev-parse", "HEAD:inside.py")
    object_file = root / ".git" / "objects" / inside[:2] / inside[2:]
    assert object_file.exists()
    object_file.unlink()

    with pytest.raises(GitInputError) as error:
        _pair(root, "HEAD", "HEAD", CaptureSelection(files={"inside.py"}))

    assert error.value.side == "before"
    assert error.value.commit == commit
    assert error.value.path == "inside.py"
    assert _leftovers(temp_root) == []


def test_capture_never_reads_blobs_outside_the_selection(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable blob outside the selection is never asked for."""
    root = _init(tmp_path / "repo")
    _write(root, "inside.py", "inside = 'unique inside'\n")
    _write(root, "outside.py", "outside = 'unique outside'\n")
    _commit(root)
    outside = _run(root, "rev-parse", "HEAD:outside.py")
    object_file = root / ".git" / "objects" / outside[:2] / outside[2:]
    assert object_file.exists()
    object_file.unlink()
    recorder = _Recorder(monkeypatch)

    first, second = _pair(root, "HEAD", "HEAD", CaptureSelection(files={"inside.py"}))
    try:
        assert _manifest_paths(first) == {"inside.py": "file"}
    finally:
        first.close()
        second.close()

    assert outside.encode("ascii") not in recorder.batch_input()
    assert recorder.batch_input()


def test_working_tree_attributes_copy_failure_to_after_side(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original = snapshots.shutil.copyfile

    def fail_copy(source: Path, destination: Path) -> str:
        if source.name == "app.py":
            raise OSError("simulated working-tree copy failure")
        return original(source, destination)

    monkeypatch.setattr(snapshots.shutil, "copyfile", fail_copy)

    with pytest.raises(SnapshotError) as error:
        _pair(root, "HEAD", None, CaptureSelection(files={"app.py"}))

    assert error.value.side == "after"
    assert error.value.revision == "WORKTREE"
    assert error.value.commit
    assert "simulated working-tree copy failure" in str(error.value)
    assert _leftovers(temp_root) == []


def test_capture_attributes_materialization_write_failure_to_side(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original = Path.write_bytes

    def fail_write(path: Path, data: bytes) -> int:
        if path.name == "app.py":
            raise OSError("simulated materialization write failure")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_write)

    with pytest.raises(SnapshotError) as error:
        _pair(root, "HEAD", "HEAD", CaptureSelection(files={"app.py"}))

    assert error.value.side == "before"
    assert error.value.revision == "HEAD"
    assert error.value.commit
    assert "simulated materialization write failure" in str(error.value)
    assert _leftovers(temp_root) == []


def test_manifest_verification_translates_traversal_failure_to_mutation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    snapshot, other = _pair(root, "HEAD", "HEAD", ALL)
    other.close()
    original_walk = snapshots.os.walk

    def fail_walk(*args: object, **kwargs: object):
        raise OSError("simulated manifest traversal failure")

    monkeypatch.setattr(snapshots.os, "walk", fail_walk)
    try:
        with pytest.raises(SnapshotMutationError) as error:
            snapshot.verify_unchanged()
    finally:
        monkeypatch.setattr(snapshots.os, "walk", original_walk)
        snapshot.close()

    assert error.value.side == "before"
    assert error.value.revision == "HEAD"
    assert error.value.commit
    assert "simulated manifest traversal failure" in str(error.value)


def test_working_tree_capture_includes_untracked_bytes_without_checkout_identity(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "untracked.py").write_text("value = 2\n", encoding="utf-8")
    before = _checkout_state(root)

    first, snapshot = _pair(root, "HEAD", None, ALL)
    first.close()
    temporary_root = snapshot.root
    try:
        assert snapshot.revision == "WORKTREE"
        assert snapshot.side == "after"
        assert snapshot.path("untracked.py").read_text(encoding="utf-8") == "value = 2\n"
        assert not snapshot.path(".git").exists()
        snapshot.verify_unchanged()
    finally:
        snapshot.close()

    assert not temporary_root.exists()
    assert _checkout_state(root) == before


def _digest_under_umask(root: Path, revision: str, mask: int) -> str:
    previous = os.umask(mask)
    try:
        snapshot, other = _pair(root, revision, revision, ALL)
        try:
            return cli._capture_digest(snapshot.manifest)
        finally:
            snapshot.close()
            other.close()
    finally:
        os.umask(previous)


def test_manifest_digest_is_independent_of_process_umask(tmp_path: Path) -> None:
    """C-02/R-07: the same logical input yields the same digest under any umask."""
    root, old, _ = _repository(tmp_path)

    restrictive = _digest_under_umask(root, old, 0o022)
    permissive = _digest_under_umask(root, old, 0o002)

    assert restrictive == permissive


def test_manifest_digest_still_detects_executable_bit_changes(tmp_path: Path) -> None:
    """Normalizing to the logical mode keeps a real +x change observable."""
    root = _init(tmp_path / "exec-repo")
    (root / "tool.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    plain = _commit(root, "plain")
    _run(root, "update-index", "--chmod=+x", "tool.sh")
    _run(root, "commit", "--quiet", "-m", "executable")
    executable = _run(root, "rev-parse", "HEAD")

    first, second = _pair(root, plain, executable, ALL)
    try:
        modes = {entry.path: entry.mode for entry in first.manifest.entries}
        executable_modes = {entry.path: entry.mode for entry in second.manifest.entries}
        assert modes["tool.sh"] == 0o644
        assert executable_modes["tool.sh"] == 0o755
        assert cli._capture_digest(first.manifest) != cli._capture_digest(second.manifest)
    finally:
        first.close()
        second.close()


# The number of Git probes does not depend on the repository size.


def _sized_repository(tmp_path: Path, count: int) -> Path:
    root = _init(tmp_path / f"repo-{count}")
    for number in range(count):
        _write(root, f"pkg{number % 7}/f{number:04d}.py", f"value = {number}\n")
    _commit(root, "base")
    _write(root, "pkg0/f0000.py", "value = 'changed'\n")
    _commit(root, "change")
    return root


def _staging_plan(coordinate: str) -> Plan:
    selection = CaptureSelection(files={coordinate})

    def plan(before: CaptureStage, after: CaptureStage) -> tuple[CaptureSelection, None]:
        before.include({coordinate})
        after.include({coordinate})
        return selection, None

    return plan


@pytest.mark.parametrize("count", [10, 2003])
def test_pair_of_revisions_issues_a_bounded_number_of_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    root = _sized_repository(tmp_path, count)
    head = _run(root, "rev-parse", "HEAD")
    parent = _run(root, "rev-parse", "HEAD~1")
    recorder = _Recorder(monkeypatch)

    first, second = _pair(root, "HEAD~1", "HEAD", plan=_staging_plan("pkg0/f0000.py"))
    first.close()
    second.close()

    names = [arguments[0] for arguments, _ in recorder.calls]
    assert names.count("rev-parse") == 2
    listings = recorder.subcommands("ls-tree")
    assert sorted(listings) == sorted(
        [("ls-tree", "-r", "-t", "-z", parent), ("ls-tree", "-r", "-t", "-z", head)]
    )
    assert recorder.subcommands("cat-file") == [("cat-file", "--batch")] * names.count("cat-file")
    assert 1 <= names.count("cat-file") <= 4
    assert set(names) == {"rev-parse", "ls-tree", "cat-file"}
    assert len(recorder.calls) <= 8


@pytest.mark.parametrize("count", [10, 2003])
def test_revision_against_working_tree_issues_a_bounded_number_of_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    root = _sized_repository(tmp_path, count)
    head = _run(root, "rev-parse", "HEAD")
    recorder = _Recorder(monkeypatch)

    first, second = _pair(root, "HEAD", None, plan=_staging_plan("pkg0/f0000.py"))
    first.close()
    second.close()

    names = [arguments[0] for arguments, _ in recorder.calls]
    assert names.count("rev-parse") == 2
    assert recorder.subcommands("ls-tree") == [("ls-tree", "-r", "-t", "-z", head)]
    assert 1 <= names.count("cat-file") <= 2
    assert set(names) == {"rev-parse", "ls-tree", "cat-file"}
    assert len(recorder.calls) <= 5


def test_probe_sequence_has_the_same_shape_for_small_and_large_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shapes = []
    for count in (10, 2003):
        root = _sized_repository(tmp_path, count)
        recorder = _Recorder(monkeypatch)
        first, second = _pair(root, "HEAD~1", "HEAD", plan=_staging_plan("pkg0/f0000.py"))
        first.close()
        second.close()
        shapes.append([re.sub(r"[0-9a-f]{40}", "<id>", " ".join(a)) for a in recorder.shape()])
        monkeypatch.undo()
    assert len(shapes[0]) == len(shapes[1])
    assert shapes[0] == shapes[1]


# Working-tree pruning follows the analysis exclusion rule.


def _pruning_repository(tmp_path: Path, *, venv_files: int = 2000) -> Path:
    root = _init(tmp_path / "repo")
    _write(root, ".gitignore", ".venv/\n__pycache__/\n.cache/\npkg/generated.py\n")
    _write(root, "pkg/mod.py", "mod = 1\n")
    _write(root, "pkg/generated.py", "generated = 'exact bytes'\n")
    _write(root, "pkg/__pycache__/m.cpython-311.pyc", "cache\n")
    _write(root, "pkg/.venv/lib.py", "lib = 1\n")
    _write(root, "pkg/.cache/x.py", "cache = 1\n")
    _write(root, ".tools/t.py", "tool = 1\n")
    _write(root, ".tools/.hid/h.py", "hidden = 1\n")
    for number in range(venv_files):
        _write(root, f".venv/lib/f{number:04d}.py", "v = 1\n")
    return root


_PRUNED = (".venv", "pkg/__pycache__", "pkg/.venv", "pkg/.cache")


def test_working_tree_capture_prunes_by_the_analysis_rule_and_never_walks_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _pruning_repository(tmp_path)
    _commit(root, "base")
    listed: list[str] = []
    original = os.listdir

    def recording_listdir(path: Any = ".") -> list[str]:
        listed.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(snapshots.os, "listdir", recording_listdir)

    first, second = _pair(root, "HEAD", None, CaptureSelection(trees={"pkg": True}))
    first.close()
    try:
        paths = set(_manifest_paths(second))
        assert "pkg/mod.py" in paths
        assert not [p for p in paths if p.split("/")[0] == ".venv"]
        for pruned in _PRUNED:
            assert not [p for p in paths if p == pruned or p.startswith(f"{pruned}/")]
        assert second.path("pkg/generated.py").read_bytes() == b"generated = 'exact bytes'\n"
    finally:
        second.close()

    assert not [item for item in listed if ".venv" in Path(item).parts]
    assert not [item for item in listed if Path(item).name in {"__pycache__", ".cache"}]


def test_clean_working_tree_and_head_capture_equal_manifests(tmp_path: Path) -> None:
    root = _pruning_repository(tmp_path, venv_files=20)
    _commit(root, "base", force=True)
    assert _run(root, "status", "--porcelain") == ""
    selection = CaptureSelection(trees={"pkg": True, ".tools": False})

    revision, working = _pair(root, "HEAD", None, selection)
    try:
        assert _manifest_view(revision) == _manifest_view(working)
        assert cli._capture_digest(revision.manifest) == cli._capture_digest(working.manifest)
        paths = set(_manifest_paths(revision))
        for pruned in _PRUNED[1:]:
            assert not [p for p in paths if p == pruned or p.startswith(f"{pruned}/")]
        assert "pkg/generated.py" in paths
    finally:
        revision.close()
        working.close()


def test_directly_declared_excluded_target_is_captured_with_its_contents(
    tmp_path: Path,
) -> None:
    root = _pruning_repository(tmp_path, venv_files=0)
    _commit(root, "base", force=True)

    revision, working = _pair(root, "HEAD", None, CaptureSelection(trees={".tools": False}))
    try:
        for snapshot in (revision, working):
            assert {p for p, kind in _manifest_paths(snapshot).items() if kind == "file"} == {
                ".tools/t.py",
                ".tools/.hid/h.py",
            }
    finally:
        revision.close()
        working.close()


def test_exclusion_is_judged_relative_to_the_analysis_root(tmp_path: Path) -> None:
    root = _init(tmp_path / "repo")
    _write(root, ".src/pkg/mod.py", "mod = 1\n")
    _write(root, ".src/pkg/__pycache__/x.pyc", "cache\n")
    _commit(root, "base", force=True)
    selection = CaptureSelection(directories={".src"}, trees={".src/pkg": True})

    revision, working = _pair(root, "HEAD", None, selection)
    try:
        for snapshot in (revision, working):
            files = {p for p, kind in _manifest_paths(snapshot).items() if kind == "file"}
            assert files == {".src/pkg/mod.py"}
    finally:
        revision.close()
        working.close()


# An unreadable directory is reported by its own location.


def _skip_when_permissions_are_not_enforced() -> None:
    if os.geteuid() == 0:
        pytest.skip("permission bits are not enforced for the root user")


def test_unreadable_directory_inside_the_selection_is_reported_at_that_directory(
    tmp_path: Path, temp_root: Path
) -> None:
    _skip_when_permissions_are_not_enforced()
    root = _init(tmp_path / "repo")
    _write(root, "pkg/a_first.py")
    _write(root, "pkg/data/file.py")
    _write(root, "pkg/z_last.py")
    _commit(root)
    data = root / "pkg" / "data"
    data.chmod(0o000)
    try:
        with pytest.raises(SnapshotError) as error:
            _pair(root, "HEAD", None, CaptureSelection(trees={"pkg": True}))
    finally:
        data.chmod(0o755)

    assert error.value.side == "after"
    assert error.value.revision == "WORKTREE"
    assert str(data.resolve()) in str(error.value)
    assert _leftovers(temp_root) == []


def test_unreadable_directories_outside_the_selection_or_under_pruned_names_are_ignored(
    tmp_path: Path,
) -> None:
    _skip_when_permissions_are_not_enforced()
    root = _init(tmp_path / "repo")
    _write(root, "pkg/a.py")
    _write(root, "pkg/.venv/lib.py")
    _write(root, "beside/data/file.py")
    _commit(root, "base")
    blocked = [root / "pkg" / ".venv", root / "beside" / "data"]
    for path in blocked:
        path.chmod(0o000)
    try:
        first, second = _pair(root, "HEAD", None, CaptureSelection(trees={"pkg": True}))
        first.close()
        second.close()
    finally:
        for path in blocked:
            path.chmod(0o755)


# Links are refused inside the selection and ignored outside it.

_MESSAGES = {
    "symlink": "symbolic links are not captured",
    "gitlink": "Gitlink is not a source directory",
}


def _link_selection() -> CaptureSelection:
    return CaptureSelection(
        directories={"."},
        trees={
            "pkg": True,
            "tools": False,
            "docs/systems": False,
            "routed/deep/target": True,
            "sysroute/systems": False,
        },
    )


def _link_plan(before: CaptureStage, after: CaptureStage) -> tuple[CaptureSelection, None]:
    before.include({"cfg/.minotaur.toml"})
    after.include({"cfg/.minotaur.toml"})
    return _link_selection(), None


def _link_base(tmp_path: Path) -> tuple[Path, str]:
    root = _init(tmp_path / "repo")
    _write(root, "pkg/ok.py")
    _write(root, "pkg/sub/inner.py")
    _write(root, "other/outside.py")
    _write(root, "tools/t.py")
    _write(root, "docs/systems/a/system.toml", "name = 'a'\n")
    _write(root, "docs/systems/.experimental/system.toml", "name = 'x'\n")
    _write(root, "cfg/.minotaur.toml", "root = '.'\n")
    return root, _commit(root, "base")


def _add_link(root: Path, kind: str, relative: str, base: str) -> str:
    target = root / relative
    if kind == "symlink":
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                _run(root, "rm", "-r", "-q", "-f", relative)
            else:
                target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("elsewhere", target)
        _run(root, "add", "--force", relative)
    else:
        _run(root, "rm", "-q", "-f", "--ignore-unmatch", "-r", "--cached", relative)
        _run(root, "update-index", "--add", "--cacheinfo", f"160000,{base},{relative}")
    return _commit(root, f"{kind} at {relative}", stage_all=False)


_ALLOWED = ["other/link", "pkg/.cache/link"]
_REFUSED = [
    "pkg/sub/link",
    "tools/.hidden/link",
    "routed",
    "routed/deep",
    "sysroute",
    "docs/systems/a/link",
    "docs/systems/.experimental/link",
    "cfg/.minotaur.toml",
]


@pytest.mark.parametrize("kind", ["symlink", "gitlink"])
@pytest.mark.parametrize("relative", _ALLOWED)
def test_links_outside_the_selection_or_under_pruned_names_are_ignored(
    tmp_path: Path, kind: str, relative: str
) -> None:
    root, base = _link_base(tmp_path)
    head = _add_link(root, kind, relative, base)

    first, second = _pair(root, base, head, plan=_link_plan)
    try:
        for snapshot in (first, second):
            assert relative not in _manifest_paths(snapshot)
            assert _manifest_paths(snapshot)["pkg/ok.py"] == "file"
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("kind", ["symlink", "gitlink"])
@pytest.mark.parametrize("relative", _REFUSED)
def test_links_inside_the_selection_are_refused_with_the_side_and_pinned_commit(
    tmp_path: Path, temp_root: Path, kind: str, relative: str
) -> None:
    root, base = _link_base(tmp_path)
    head = _add_link(root, kind, relative, base)
    detail = f"unsafe entry {relative!r}: {_MESSAGES[kind]}"

    with pytest.raises(SnapshotError) as after_error:
        _pair(root, base, head, plan=_link_plan)
    with pytest.raises(SnapshotError) as before_error:
        _pair(root, head, base, plan=_link_plan)

    assert str(after_error.value) == f"after revision {head!r}: {detail}"
    assert after_error.value.side == "after"
    assert after_error.value.commit == head
    assert str(before_error.value) == f"before revision {head!r}: {detail}"
    assert before_error.value.side == "before"
    assert before_error.value.commit == head
    assert _leftovers(temp_root) == []


def test_links_under_a_pruned_name_of_an_unpruned_tree_are_still_refused(
    tmp_path: Path,
) -> None:
    root, base = _link_base(tmp_path)
    head = _add_link(root, "symlink", "tools/__pycache__/link", base)

    with pytest.raises(SnapshotError, match="symbolic links are not captured"):
        _pair(root, base, head, plan=_link_plan)


def test_working_tree_side_skips_links_silently(tmp_path: Path) -> None:
    root, base = _link_base(tmp_path)
    os.symlink("elsewhere", root / "pkg" / "sub" / "link")
    os.symlink("elsewhere", root / "tools" / "tool_link")
    os.symlink("elsewhere", root / "sysroute")

    first, second = _pair(root, base, None, plan=_link_plan)
    try:
        paths = _manifest_paths(second)
        assert "pkg/sub/link" not in paths
        assert "tools/tool_link" not in paths
        assert "sysroute" not in paths
        assert paths["pkg/sub/inner.py"] == "file"
    finally:
        first.close()
        second.close()


def test_regular_file_at_a_route_or_root_position_is_captured_as_that_file(
    tmp_path: Path,
) -> None:
    root = _init(tmp_path / "repo")
    _write(root, "src", "not a directory\n")
    _write(root, "a", "ancestor file\n")
    _commit(root)
    selection = CaptureSelection(directories={"src"}, trees={"a/b": True})

    revision, working = _pair(root, "HEAD", None, selection)
    try:
        for snapshot in (revision, working):
            assert _manifest_paths(snapshot) == {"src": "file", "a": "file"}
            assert snapshot.path("src").read_text(encoding="utf-8") == "not a directory\n"
    finally:
        revision.close()
        working.close()


def test_unpruned_systems_directory_keeps_hidden_folders_inside_a_pruned_enclosing_target(
    tmp_path: Path,
) -> None:
    root, base = _link_base(tmp_path)
    _write(root, "docs/.private/hidden.py")
    head = _commit(root, "hidden")
    selection = CaptureSelection(trees={".": True, "docs/systems": False})

    first, second = _pair(root, head, None, selection)
    try:
        for snapshot in (first, second):
            paths = _manifest_paths(snapshot)
            assert paths["docs/systems/.experimental/system.toml"] == "file"
            assert "docs/.private/hidden.py" not in paths
    finally:
        first.close()
        second.close()

    refused = _add_link(root, "symlink", "docs/systems/.experimental/link", base)
    with pytest.raises(SnapshotError, match="symbolic links are not captured"):
        _pair(root, refused, None, selection)


# Capture selects what the analysis selects.


def _analysis_files(capture_root: Path, targets: tuple[str, ...]) -> set[str]:
    workspace, selection = select_sources(
        capture_root, tuple(capture_root / target for target in targets), default_registry()
    )
    return {item.relative_to(workspace.root).as_posix() for item in selection.files}


def _analysis_repository(tmp_path: Path) -> Path:
    root = _init(tmp_path / "repo")
    for relative in (
        "pkg/a.py",
        "pkg/sub/b.py",
        "pkg/nested/inner/n.py",
        "pkg/.hidden/h.py",
        "pkg/__pycache__/c.py",
        "pkg/.venv/v.py",
        "pkg/node_modules/m.py",
        "pkg/node_modules/m.js",
        ".tools/t.py",
        ".tools/.hid/h.py",
        "elsewhere/e.py",
    ):
        _write(root, relative)
    _commit(root, "base", force=True)
    return root


_ANALYSIS_TARGETS = ("pkg", ".tools")
_ANALYSIS_SELECTION = CaptureSelection(trees={"pkg": True, ".tools": False})


def test_captured_files_match_the_analysis_selection_on_both_sides(tmp_path: Path) -> None:
    root = _analysis_repository(tmp_path)
    live = _analysis_files(root, _ANALYSIS_TARGETS)

    revision, working = _pair(root, "HEAD", None, _ANALYSIS_SELECTION)
    try:
        assert _analysis_files(working.root, _ANALYSIS_TARGETS) == live
        assert _analysis_files(revision.root, _ANALYSIS_TARGETS) == live
        assert "pkg/.hidden/h.py" not in live
        assert ".tools/.hid/h.py" in live
    finally:
        revision.close()
        working.close()


def test_capture_honours_a_patched_exclusion_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _analysis_repository(tmp_path)
    original = exclusions.is_excluded
    monkeypatch.setattr(
        exclusions,
        "is_excluded",
        lambda relative: original(relative) or "node_modules" in relative.parts,
    )
    live = _analysis_files(root, _ANALYSIS_TARGETS)
    assert "pkg/node_modules/m.py" not in live

    revision, working = _pair(root, "HEAD", None, _ANALYSIS_SELECTION)
    try:
        for snapshot in (revision, working):
            assert not [p for p in _manifest_paths(snapshot) if "node_modules" in p]
            assert _analysis_files(snapshot.root, _ANALYSIS_TARGETS) == live
    finally:
        revision.close()
        working.close()


# Lifecycle, write-once staging and pinning.


def test_plan_failure_propagates_unchanged_and_removes_every_directory(
    tmp_path: Path, temp_root: Path
) -> None:
    root, old, _ = _repository(tmp_path)
    failure = RuntimeError("plan failed")

    def failing(before: CaptureStage, after: CaptureStage) -> tuple[CaptureSelection, None]:
        before.include({"app.py"})
        after.include({"app.py"})
        assert len(_leftovers(temp_root)) == 2
        raise failure

    for after in ("HEAD", None):
        with pytest.raises(RuntimeError) as error:
            _pair(root, old, after, plan=failing)
        assert error.value is failure
        assert _leftovers(temp_root) == []


def test_staged_working_tree_bytes_are_sealed_not_recopied(tmp_path: Path) -> None:
    root, old, _ = _repository(tmp_path)

    def plan(before: CaptureStage, after: CaptureStage) -> tuple[CaptureSelection, None]:
        after.include({"app.py"})
        (root / "app.py").write_text("edited = after_staging\n", encoding="utf-8")
        return CaptureSelection(files={"app.py"}), None

    first, second = _pair(root, old, None, plan=plan)
    try:
        assert second.path("app.py").read_text(encoding="utf-8") == "new = True\n"
        assert first.path("app.py").read_text(encoding="utf-8") == "old = True\n"
    finally:
        first.close()
        second.close()


def test_paths_staged_on_one_side_are_included_in_both_sides(tmp_path: Path) -> None:
    root, old, _ = _repository(tmp_path)

    def plan(before: CaptureStage, after: CaptureStage) -> tuple[CaptureSelection, None]:
        after.include({"docs/README"})
        return CaptureSelection(files={"app.py"}), None

    first, second = _pair(root, old, "HEAD", plan=plan)
    try:
        assert set(_manifest_paths(first)) == set(_manifest_paths(second))
        assert "docs/README" in _manifest_paths(first)
    finally:
        first.close()
        second.close()


def test_head_moving_during_capture_does_not_change_the_pinned_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, new = _repository(tmp_path)
    moved: list[str] = []

    def advance(arguments: tuple[str, ...]) -> None:
        if arguments[0] == "ls-tree" and not moved:
            (root / "app.py").write_text("newer = True\n", encoding="utf-8")
            moved.append(_commit(root, "newer"))

    _Recorder(monkeypatch, advance)

    first, second = _pair(root, "HEAD", None, CaptureSelection(files={"app.py"}))
    try:
        assert moved and moved[0] != new
        assert first.commit == new
        assert first.path("app.py").read_text(encoding="utf-8") == "new = True\n"
    finally:
        first.close()
        second.close()


def test_including_a_path_absent_everywhere_stages_nothing_and_raises_nothing(
    tmp_path: Path,
) -> None:
    root, old, _ = _repository(tmp_path)

    def plan(before: CaptureStage, after: CaptureStage) -> tuple[CaptureSelection, None]:
        before.include({"does/not/exist.toml"})
        after.include({"does/not/exist.toml"})
        return CaptureSelection(files={"app.py"}), None

    for after in ("HEAD", None):
        first, second = _pair(root, old, after, plan=plan)
        try:
            for snapshot in (first, second):
                assert _manifest_paths(snapshot) == {"app.py": "file"}
                assert not (snapshot.root / "does").exists()
        finally:
            first.close()
            second.close()


def test_plan_result_is_returned_with_the_snapshots(tmp_path: Path) -> None:
    root, old, _ = _repository(tmp_path)

    first, second, result = capture_pair(
        root, old, "HEAD", plan=lambda before, after: (CaptureSelection(), {"answer": 42})
    )
    try:
        assert result == {"answer": 42}
    finally:
        first.close()
        second.close()


def test_working_tree_never_enters_git_even_inside_an_unpruned_tree(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)

    first, second = _pair(root, "HEAD", None, CaptureSelection(trees={".": False}))
    try:
        assert not [p for p in _manifest_paths(second) if p.split("/")[0] == ".git"]
        assert _manifest_paths(second)["app.py"] == "file"
    finally:
        first.close()
        second.close()


# Failure location, seal cleanup, route listing and staging as listed.


def test_unreadable_directory_is_the_reported_location_not_the_previous_entry(
    tmp_path: Path,
) -> None:
    """The ``at`` location names the directory, even when a sibling was visited last."""
    _skip_when_permissions_are_not_enforced()
    root = _init(tmp_path / "repo")
    _write(root, "pkg/a_first.py")
    _write(root, "pkg/data/file.py")
    _write(root, "pkg/z_last.py")
    _commit(root)
    data = (root / "pkg" / "data").resolve()
    data.chmod(0o000)
    try:
        with pytest.raises(SnapshotError) as error:
            _pair(root, "HEAD", None, CaptureSelection(trees={"pkg": True}))
    finally:
        data.chmod(0o755)

    assert f"could not capture working-tree input at {data!r}:" in str(error.value)


@pytest.mark.parametrize("after", ["HEAD", None])
def test_seal_failure_after_the_other_side_sealed_removes_every_directory(
    tmp_path: Path, temp_root: Path, after: str | None
) -> None:
    root, old, _ = _repository(tmp_path)
    # Holding the stages keeps the temporary-directory finalizers from running,
    # so only an explicit removal by the capture owner empties the root.
    stages: list[CaptureStage] = []

    def plan(before: CaptureStage, after_stage: CaptureStage) -> tuple[CaptureSelection, None]:
        stages.extend((before, after_stage))
        os.symlink("elsewhere", after_stage.root / "planted")
        return CaptureSelection(files={"app.py"}), None

    with pytest.raises(SnapshotError, match="captured path is not an ordinary file: planted"):
        _pair(root, old, after, plan=plan)

    assert len(stages) == 2
    assert _leftovers(temp_root) == []


def test_working_tree_lists_only_selected_subtrees_never_route_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _init(tmp_path / "repo")
    _write(root, "pkg/sub/a.py")
    _write(root, "pkg/sub/inner/b.py")
    _write(root, "pkg/beside.py")
    _write(root, "cfg/c.toml")
    _write(root, "cfg/unrelated.toml")
    _write(root, "data/big.bin")
    _commit(root)
    listed: list[Path] = []
    original = os.listdir

    def recording_listdir(path: Any = ".") -> list[str]:
        listed.append(Path(path))
        return original(path)

    monkeypatch.setattr(snapshots.os, "listdir", recording_listdir)
    selection = CaptureSelection(files={"cfg/c.toml"}, trees={"pkg/sub": True})

    first, second = _pair(root, "HEAD", None, selection)
    try:
        assert {p for p, kind in _manifest_paths(second).items() if kind == "file"} == {
            "cfg/c.toml",
            "pkg/sub/a.py",
            "pkg/sub/inner/b.py",
        }
    finally:
        first.close()
        second.close()

    resolved = root.resolve()
    assert {path.relative_to(resolved).as_posix() for path in listed} == {
        "pkg/sub",
        "pkg/sub/inner",
    }


@pytest.mark.parametrize("after", ["HEAD", None])
def test_included_path_present_as_a_directory_is_staged_as_a_directory_only(
    tmp_path: Path, after: str | None
) -> None:
    root, old, _ = _repository(tmp_path)

    def plan(before: CaptureStage, after_stage: CaptureStage) -> tuple[CaptureSelection, None]:
        before.include({"docs"})
        after_stage.include({"docs"})
        return CaptureSelection(files={"app.py"}), None

    first, second = _pair(root, old, after, plan=plan)
    try:
        for snapshot in (first, second):
            assert _manifest_paths(snapshot) == {"app.py": "file", "docs": "directory"}
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("layout", "blocked", "mode", "selection"),
    [
        # A route ancestor that cannot be searched, with the target beneath it.
        (["pkg/sub/a.py"], "pkg", 0o000, CaptureSelection(trees={"pkg/sub": True})),
        # An in-tree directory that can be listed but not searched.
        (["pkg/sub/data/f.py"], "pkg/sub/data", 0o444, CaptureSelection(trees={"pkg": True})),
        # A directory holding only an exactly selected file.
        (["pkg/sub/a.py"], "pkg/sub", 0o000, CaptureSelection(files={"pkg/sub/a.py"})),
    ],
)
def test_entry_inspection_failure_is_reported_at_the_directory_that_cannot_be_searched(
    tmp_path: Path,
    temp_root: Path,
    layout: list[str],
    blocked: str,
    mode: int,
    selection: CaptureSelection,
) -> None:
    _skip_when_permissions_are_not_enforced()
    root = _init(tmp_path / "repo")
    for relative in layout:
        _write(root, relative)
    _commit(root)
    directory = root / blocked
    directory.chmod(mode)
    try:
        if os.access(directory / "probe", os.F_OK) or (mode and os.access(directory, os.X_OK)):
            pytest.skip("directory permission bits have no effect here")
        with pytest.raises(SnapshotError) as error:
            _pair(root, "HEAD", None, selection)
    finally:
        directory.chmod(0o755)

    assert error.value.side == "after"
    assert error.value.revision == "WORKTREE"
    # The location is the text after "input at"; the OS error text that follows
    # may name the entry that could not be inspected.
    assert f"input at {directory.resolve()!r}: " in str(error.value)
    assert _leftovers(temp_root) == []


@pytest.mark.parametrize(("count", "size"), [(16, 4 << 20), (4, 8 << 20)])
def test_revision_capture_memory_stays_near_one_selected_file(
    tmp_path: Path, count: int, size: int
) -> None:
    import tracemalloc

    root = _init(tmp_path / "repo")
    for index in range(count):
        label = f"file {index}\n".encode()
        (root / f"big{index:02}.bin").write_bytes((label * (size // len(label) + 1))[:size])
    commit = _commit(root, "large selection")

    tracemalloc.start()
    try:
        first, second, _ = capture_pair(root, commit, commit, plan=_plan(ALL))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    try:
        assert len(_manifest_paths(first)) == count
        assert first.path("big00.bin").stat().st_size == size
        assert second.path(f"big{count - 1:02}.bin").stat().st_size == size
    finally:
        first.close()
        second.close()

    assert peak < 4 * size
