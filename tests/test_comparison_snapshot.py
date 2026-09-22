from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import minotaur.comparison_snapshot as snapshots
from minotaur.comparison_snapshot import (
    SnapshotError,
    SnapshotMutationError,
    capture_pair,
    capture_revision,
    capture_working_tree,
)


def _run(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "--quiet")
    _run(root, "config", "user.email", "tests@example.invalid")
    _run(root, "config", "user.name", "snapshot tests")
    (root / "app.py").write_text("old = True\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "README").write_text("old\n", encoding="utf-8")
    _run(root, "add", "--all")
    _run(root, "commit", "--quiet", "-m", "old")
    old = _run(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("new = True\n", encoding="utf-8")
    _run(root, "add", "--all")
    _run(root, "commit", "--quiet", "-m", "new")
    new = _run(root, "rev-parse", "HEAD")
    return root, old, new


def _checkout_state(root: Path) -> tuple[str, str, str, str]:
    return (
        _run(root, "rev-parse", "HEAD"),
        _run(root, "ls-files", "--stage"),
        _run(root, "status", "--porcelain=v1"),
        _run(root, "config", "--local", "--list", "--null"),
    )


def test_capture_pins_each_side_and_cleans_temporary_roots(tmp_path: Path) -> None:
    root, old, new = _repository(tmp_path)
    before_state = _checkout_state(root)

    first, second = capture_pair(root, old, "HEAD")
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
    snapshot = capture_revision(root, old, side="before")
    temporary_root = snapshot.root
    snapshot.path("app.py").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(SnapshotMutationError, match="app.py"):
        snapshot.close()

    assert not temporary_root.exists()


def test_capture_pair_closes_first_side_when_second_side_is_missing(tmp_path: Path) -> None:
    root, old, _ = _repository(tmp_path)
    first_root: Path | None = None
    original = snapshots.capture_revision

    def recording_capture(*args: object, **kwargs: object):
        nonlocal first_root
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        if first_root is None:
            first_root = result.root
        return result

    snapshots.capture_revision = recording_capture  # type: ignore[assignment]
    try:
        with pytest.raises(Exception) as error:
            capture_pair(root, old, "missing-revision")
    finally:
        snapshots.capture_revision = original

    assert getattr(error.value, "side", None) == "after"
    assert first_root is not None
    assert not first_root.exists()


def test_capture_preserves_tracked_bytes_despite_archive_attributes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "--quiet")
    _run(root, "config", "user.email", "tests@example.invalid")
    _run(root, "config", "user.name", "snapshot tests")
    omitted = root / "omitted.py"
    substituted = root / "substituted.py"
    attributes = root / ".gitattributes"
    omitted.write_text("omitted = True\n", encoding="utf-8")
    substituted.write_text("commit = $Format:%H$\n", encoding="utf-8")
    attributes.write_text(
        "omitted.py export-ignore\nsubstituted.py export-subst\n", encoding="utf-8"
    )
    _run(root, "add", "--all")
    _run(root, "commit", "--quiet", "-m", "attributes")

    snapshot = capture_revision(root, "HEAD", side="before")
    try:
        assert snapshot.path("omitted.py").read_bytes() == omitted.read_bytes()
        assert snapshot.path("substituted.py").read_bytes() == substituted.read_bytes()
    finally:
        snapshot.close()


def test_capture_rejects_gitlink_entries_instead_of_materializing_empty_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    nested = tmp_path / "nested"
    root.mkdir()
    nested.mkdir()
    for repository in (root, nested):
        _run(repository, "init", "--quiet")
        _run(repository, "config", "user.email", "tests@example.invalid")
        _run(repository, "config", "user.name", "snapshot tests")
    (nested / "README").write_text("nested\n", encoding="utf-8")
    _run(nested, "add", "--all")
    _run(nested, "commit", "--quiet", "-m", "nested")
    nested_commit = _run(nested, "rev-parse", "HEAD")
    (root / "app.py").write_text("app = True\n", encoding="utf-8")
    _run(root, "add", "--all")
    _run(root, "update-index", "--add", "--cacheinfo", f"160000,{nested_commit},vendor")
    _run(root, "commit", "--quiet", "-m", "gitlink")

    with pytest.raises(SnapshotError, match="unsafe entry.*vendor"):
        capture_revision(root, "HEAD", side="before")


def test_capture_attributes_unexpected_blob_read_failure_to_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original = snapshots.PinnedCommit.read_blob

    def fail_read(pin: snapshots.PinnedCommit, relative: str) -> bytes:
        if relative == "app.py":
            raise OSError("simulated blob read failure")
        return original(pin, relative)

    monkeypatch.setattr(snapshots.PinnedCommit, "read_blob", fail_read)

    with pytest.raises(SnapshotError) as error:
        capture_revision(root, "HEAD", side="before")

    assert error.value.side == "before"
    assert error.value.revision == "HEAD"
    assert error.value.pinned_sha
    assert "simulated blob read failure" in str(error.value)


def test_working_tree_attributes_copy_failure_to_after_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original = snapshots.shutil.copyfile

    def fail_copy(source: Path, destination: Path) -> str:
        if source.name == "app.py":
            raise OSError("simulated working-tree copy failure")
        return original(source, destination)

    monkeypatch.setattr(snapshots.shutil, "copyfile", fail_copy)

    with pytest.raises(SnapshotError) as error:
        capture_working_tree(root, side="after")

    assert error.value.side == "after"
    assert error.value.revision == "WORKTREE"
    assert error.value.pinned_sha
    assert "simulated working-tree copy failure" in str(error.value)


def test_working_tree_capture_includes_untracked_bytes_without_checkout_identity(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "untracked.py").write_text("value = 2\n", encoding="utf-8")
    before = _checkout_state(root)

    snapshot = capture_working_tree(root, side="after")
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
