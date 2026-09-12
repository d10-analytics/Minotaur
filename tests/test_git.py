from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minotaur import git


def _run(root: Path, *arguments: str, input: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        input=input,
        capture_output=True,
        check=True,
        text=input is None,
    )
    output = result.stdout
    return output.decode() if isinstance(output, bytes) else output


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "--quiet")
    _run(root, "config", "user.email", "tests@example.invalid")
    _run(root, "config", "user.name", "Git tests")
    (root / "plain file.txt").write_bytes(b"historical\x00bytes\n")
    executable = root / "run.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (root / "nested").mkdir()
    (root / "nested" / "deep file.py").write_bytes(b"print('old')\n")
    (root / "link").symlink_to("plain file.txt")

    submodule = tmp_path / "submodule"
    submodule.mkdir()
    _run(submodule, "init", "--quiet")
    _run(submodule, "config", "user.email", "tests@example.invalid")
    _run(submodule, "config", "user.name", "Git tests")
    (submodule / "README").write_text("submodule\n")
    _run(submodule, "add", "README")
    _run(submodule, "commit", "--quiet", "-m", "submodule")
    submodule_sha = _run(submodule, "rev-parse", "HEAD").strip()
    _run(root, "add", "--all")
    _run(root, "update-index", "--add", "--cacheinfo", f"160000,{submodule_sha},vendor")
    _run(root, "commit", "--quiet", "-m", "initial")
    return root, _run(root, "rev-parse", "HEAD").strip()


def _empty_repository(tmp_path: Path) -> Path:
    root = tmp_path / "empty-repo"
    root.mkdir()
    _run(root, "init", "--quiet")
    _run(root, "config", "user.email", "tests@example.invalid")
    _run(root, "config", "user.name", "Git tests")
    empty_tree = _run(root, "mktree", input=b"").strip()
    empty_commit = _run(root, "commit-tree", empty_tree, "-m", "empty").strip()
    _run(root, "update-ref", "HEAD", empty_commit)
    return root


def test_run_git_keeps_text_default_and_supports_bytes(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)

    text_result = git.run_git(root, ("rev-parse", "HEAD"))
    bytes_result = git.run_git(root, ("rev-parse", "HEAD"), text=False)

    assert text_result is not None
    assert isinstance(text_result.stdout, str)
    assert bytes_result is not None
    assert isinstance(bytes_result.stdout, bytes)


def test_pin_delegates_to_byte_mode_while_tolerant_default_stays_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repository(tmp_path)
    original = git.run_git
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def recording(root_arg: Path, arguments: tuple[str, ...], **kwargs: object):
        calls.append((arguments, kwargs))
        return original(root_arg, arguments, **kwargs)

    monkeypatch.setattr(git, "run_git", recording)
    pinned = git.PinnedCommit.pin(root)

    assert pinned.commit
    assert (
        ("rev-parse", "--verify", "HEAD^{commit}"),
        {"text": False},
    ) in calls
    tolerant = original(root, ("rev-parse", "HEAD"))
    assert tolerant is not None
    assert isinstance(tolerant.stdout, str)


def test_pinned_commit_reads_old_bytes_after_head_advances(tmp_path: Path) -> None:
    root, first_sha = _repository(tmp_path)
    pinned = git.PinnedCommit.pin(root)
    (root / "plain file.txt").write_bytes(b"current\n")
    _run(root, "add", "--all")
    _run(root, "commit", "--quiet", "-m", "advance")

    assert pinned.commit == first_sha
    assert pinned.read_blob("plain file.txt") == b"historical\x00bytes\n"
    assert git.PinnedCommit.pin(root).commit != pinned.commit


def test_pin_reports_unavailable_and_unborn_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(git, "run_git", lambda *args, **kwargs: None)

    with pytest.raises(git.GitInputError) as unavailable:
        git.PinnedCommit.pin(root)
    assert "historical" in str(unavailable.value)
    assert str(root) in str(unavailable.value)
    assert unavailable.value.commit is None
    assert unavailable.value.cause == "unavailable Git probe"

    monkeypatch.undo()
    _run(root, "init", "--quiet")
    with pytest.raises(git.GitInputError) as unborn:
        git.PinnedCommit.pin(root)
    assert "historical" in str(unborn.value)
    assert "no committed HEAD" in str(unborn.value)
    assert unborn.value.commit is None


def test_empty_pinned_tree_lists_as_empty_tuple(tmp_path: Path) -> None:
    pinned = git.PinnedCommit.pin(_empty_repository(tmp_path))

    assert pinned.entries() == ()


def test_entries_classify_modes_and_preserve_names_and_bytes(tmp_path: Path) -> None:
    root, sha = _repository(tmp_path)
    pinned = git.PinnedCommit.pin(root)

    entries = {entry.path: entry for entry in pinned.entries()}
    assert pinned.commit == sha
    assert entries["plain file.txt"].is_regular_file
    assert entries["run.sh"].is_regular_file
    assert entries["run.sh"].mode == "100755"
    assert entries["link"].is_link
    assert entries["vendor"].is_gitlink
    assert entries["vendor"].mode == "160000"
    assert entries["nested"].kind == "tree"
    assert pinned.entries("nested")[0].path == "nested/deep file.py"
    assert pinned.read_blob("plain file.txt") == b"historical\x00bytes\n"
    assert pinned.read_blob("nested/deep file.py") == b"print('old')\n"


def test_entry_returns_absence_or_typed_entry_and_rejects_blocked_ancestors(
    tmp_path: Path,
) -> None:
    root, _ = _repository(tmp_path)
    pinned = git.PinnedCommit.pin(root)

    assert pinned.entry("missing.txt") is None
    assert pinned.entry("nested/missing.py") is None
    assert pinned.entry("nested") == git.TreeEntry("nested", "040000", "tree")
    assert pinned.entry("link").is_link  # type: ignore[union-attr]
    assert pinned.entry("vendor").is_gitlink  # type: ignore[union-attr]

    for blocked in ("plain file.txt/child", "link/child", "vendor/child"):
        with pytest.raises(git.GitInputError) as error:
            pinned.entry(blocked)
        assert error.value.commit == pinned.commit
        assert error.value.path == blocked
        assert "historical" in str(error.value)


def test_failed_listing_is_not_reported_as_empty_or_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repository(tmp_path)
    pinned = git.PinnedCommit.pin(root)
    original = git.run_git

    def failed_listing(root_arg: Path, arguments: tuple[str, ...], **kwargs: object):
        if arguments[:2] == ("ls-tree", "-z"):
            return subprocess.CompletedProcess(
                ["git", *arguments], 128, b"", b"Not a valid object name\n"
            )
        return original(root_arg, arguments, **kwargs)

    monkeypatch.setattr(git, "run_git", failed_listing)
    with pytest.raises(git.GitInputError) as error:
        pinned.entries("nested")
    assert error.value.commit == pinned.commit
    assert error.value.path == "nested"
    assert "Not a valid object name" in str(error.value)


def test_unavailable_listing_is_attributed_to_historical_pin_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repository(tmp_path)
    pinned = git.PinnedCommit.pin(root)
    original = git.run_git

    def unavailable_listing(root_arg: Path, arguments: tuple[str, ...], **kwargs: object):
        if arguments[:2] == ("ls-tree", "-z"):
            return None
        return original(root_arg, arguments, **kwargs)

    monkeypatch.setattr(git, "run_git", unavailable_listing)
    with pytest.raises(git.GitInputError) as error:
        pinned.entries("nested")
    assert error.value.commit == pinned.commit
    assert error.value.path == "nested"
    assert "historical" in str(error.value)
    assert "unavailable" in str(error.value)


def test_failed_blob_read_is_attributed_and_successful_sibling_remains_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repository(tmp_path)
    pinned = git.PinnedCommit.pin(root)
    original = git.run_git

    def failed_read(root_arg: Path, arguments: tuple[str, ...], **kwargs: object):
        if arguments == ("show", f"{pinned.commit}:plain file.txt"):
            return subprocess.CompletedProcess(["git", *arguments], 128, b"", b"blob read failed\n")
        return original(root_arg, arguments, **kwargs)

    monkeypatch.setattr(git, "run_git", failed_read)
    with pytest.raises(git.GitInputError) as error:
        pinned.read_blob("plain file.txt")
    assert error.value.commit == pinned.commit
    assert error.value.path == "plain file.txt"
    assert "historical" in str(error.value)
    assert "blob read failed" in str(error.value)
    assert pinned.read_blob("nested/deep file.py") == b"print('old')\n"


def test_read_blob_rejects_non_regular_entries(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    pinned = git.PinnedCommit.pin(root)

    for path in ("link", "vendor", "nested"):
        with pytest.raises(git.GitInputError) as error:
            pinned.read_blob(path)
        assert error.value.commit == pinned.commit
        assert error.value.path == path


def test_tolerant_head_probe_still_returns_none_for_absence_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repository(tmp_path)

    assert git.read_head_blob(root, "missing.txt") is None
    original_run = git.subprocess.run

    def unavailable(*args: object, **kwargs: object):
        raise OSError("git unavailable")

    monkeypatch.setattr(git.subprocess, "run", unavailable)
    assert git.read_head_blob(root, "plain file.txt") is None
    monkeypatch.setattr(git.subprocess, "run", original_run)
