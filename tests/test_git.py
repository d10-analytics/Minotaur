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


def _promisor_clone(tmp_path: Path) -> tuple[Path, str]:
    """Clone a local origin with blob filtering, leaving the blob promised.

    The origin advertises ``uploadpack.allowFilter`` so the file-transport
    clone is a real partial clone: the commit and tree are present, the file
    blob is absent, and the promisor remote is reachable for a lazy fetch.
    """
    origin = tmp_path / "promisor-origin"
    origin.mkdir()
    _run(origin, "init", "--quiet")
    _run(origin, "config", "user.email", "tests@example.invalid")
    _run(origin, "config", "user.name", "Git tests")
    _run(origin, "config", "uploadpack.allowFilter", "true")
    (origin / "promised.py").write_bytes(b"print('promised bytes')\n")
    _run(origin, "add", "--all")
    _run(origin, "commit", "--quiet", "-m", "initial")
    sha = _run(origin, "rev-parse", "HEAD").strip()

    clone = tmp_path / "promisor-clone"
    _run(
        tmp_path,
        "clone",
        "--quiet",
        "--no-checkout",
        "--filter=blob:none",
        "--no-local",
        origin.as_uri(),
        str(clone),
    )
    return clone, sha


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
        ("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
        {"text": False},
    ) in calls
    tolerant = original(root, ("rev-parse", "HEAD"))
    assert tolerant is not None
    assert isinstance(tolerant.stdout, str)


def test_resolve_accepts_local_refs_and_pins_before_the_ref_moves(tmp_path: Path) -> None:
    root, first_sha = _repository(tmp_path)
    _run(root, "branch", "release")
    pinned = git.PinnedCommit.resolve(root, "release", side="before")

    (root / "plain file.txt").write_bytes(b"after\n")
    _run(root, "add", "--all")
    _run(root, "commit", "--quiet", "-m", "advance")
    _run(root, "branch", "-f", "release", "HEAD")

    assert pinned.commit == first_sha
    assert git.resolve_commit(root, "release", side="after") != pinned.commit
    assert pinned.read_blob("plain file.txt") == b"historical\x00bytes\n"


def test_resolve_missing_revision_is_side_specific_and_never_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repository(tmp_path)
    calls: list[tuple[str, ...]] = []
    original = git.run_git

    def recording(root_arg: Path, arguments: tuple[str, ...], **kwargs: object):
        calls.append(arguments)
        return original(root_arg, arguments, **kwargs)

    monkeypatch.setattr(git, "run_git", recording)
    with pytest.raises(git.GitInputError) as error:
        git.PinnedCommit.resolve(root, "missing-branch", side="after")

    assert error.value.side == "after"
    assert error.value.commit is None
    assert "missing-branch" in str(error.value)
    assert all(arguments[0] != "fetch" for arguments in calls)
    assert all(arguments[0] not in {"checkout", "worktree"} for arguments in calls)


def test_promisor_clone_reads_refuse_lazy_fetching_missing_objects(tmp_path: Path) -> None:
    """A promised object is a side-attributed error, never an implicit fetch."""
    clone, sha = _promisor_clone(tmp_path)
    pinned = git.PinnedCommit.resolve(clone, sha, side="before")

    # The commit and tree resolve locally; the blob is only promised.
    assert pinned.commit == sha
    assert [entry.path for entry in pinned.entries()] == ["promised.py"]

    with pytest.raises(git.GitInputError) as error:
        pinned.read_blob("promised.py")

    assert error.value.side == "before"
    assert error.value.commit == sha
    assert error.value.path == "promised.py"
    assert "could not fetch" in str(error.value)

    # Control: the same object is genuinely fetchable from the promisor remote,
    # so the refusal above is the lazy-fetch policy and not an absent object.
    fetched = subprocess.run(
        ["git", "show", f"{sha}:promised.py"],
        cwd=clone,
        capture_output=True,
        check=False,
    )
    assert fetched.returncode == 0
    assert fetched.stdout == b"print('promised bytes')\n"


def test_run_git_environment_disables_lazy_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every strict Git probe carries the no-lazy-fetch environment."""
    root, _ = _repository(tmp_path)
    captured: list[dict[str, str]] = []
    real_run = subprocess.run

    def recording(*args: object, **kwargs: object):
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        captured.append(environment)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git.subprocess, "run", recording)
    assert git.run_git(root, ("rev-parse", "HEAD")) is not None

    assert captured
    assert all(environment.get("GIT_NO_LAZY_FETCH") == "1" for environment in captured)


def test_resolved_pin_keeps_side_attribution_for_later_input_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repository(tmp_path)
    pinned = git.PinnedCommit.resolve(root, "HEAD", side="before")
    original = git.run_git

    def unavailable_listing(root_arg: Path, arguments: tuple[str, ...], **kwargs: object):
        if arguments[:2] == ("ls-tree", "-z"):
            return None
        return original(root_arg, arguments, **kwargs)

    monkeypatch.setattr(git, "run_git", unavailable_listing)
    with pytest.raises(git.GitInputError) as error:
        pinned.entries()

    assert error.value.side == "before"


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
    assert "historical" in str(error.value)
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
        assert "historical" in str(error.value)


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
