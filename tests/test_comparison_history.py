"""Natural proof for the complete pinned historical acquisition."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from minotaur import git, system
from minotaur.comparison import (
    HistoricalInputError,
    load_historical_inputs,
    validate_saved_selection,
)
from minotaur.graph_model import loading
from minotaur.graph_model.loading import graph_digest
from minotaur.query.freshness import recorded_selection_view


def _run(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return target


def _commit(root: Path, message: str) -> None:
    _run(root, "add", "-A")
    _run(root, "commit", "--quiet", "-m", message)


def _set_config(root: Path, *, targets: list[str], root_value: str = ".") -> None:
    _write(
        root,
        ".minotaur.toml",
        "[minotaur]\n"
        "schema_version = 1\n"
        f"root = {json.dumps(root_value)}\n"
        'graph = "graph.json"\n'
        f"targets = {json.dumps(targets)}\n"
        'systems_dir = "docs/systems"\n',
    )


def _set_selection(root: Path, selection: object) -> None:
    graph_path = root / "graph.json"
    document = json.loads(graph_path.read_text(encoding="utf-8"))
    document["extensions"] = {"minotaur": {"selection": selection}}
    graph = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    graph_path.write_bytes(graph)
    _write(root, "graph.json.sha256", (graph_digest(graph) + "\n").encode("ascii"))


def _relocate_snapshot(root: Path, relative: str) -> Path:
    destination = root / relative
    destination.mkdir(parents=True)
    for name in ("app.py", "graph.json", "graph.json.sha256", "docs"):
        shutil.move(str(root / name), str(destination / name))
    return destination


def _working_snapshot(root: Path) -> tuple[dict[Path, bytes], str, str, str]:
    files = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    return (
        files,
        _run(root, "ls-files", "--stage"),
        _run(root, "status", "--porcelain=v1"),
        _run(root, "diff", "--cached", "--binary"),
    )


def _repository(tmp_path: Path, selection: object = ["app.py"]) -> tuple[Path, str, bytes]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _run(root, "init", "--quiet")
    _run(root, "config", "user.email", "tests@example.invalid")
    _run(root, "config", "user.name", "Minotaur tests")
    _write(
        root,
        ".minotaur.toml",
        "[minotaur]\n"
        "schema_version = 1\n"
        'root = "."\n'
        'graph = "graph.json"\n'
        'targets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )
    _write(root, "app.py", "def app():\n    return 1\n")
    _write(
        root,
        "docs/systems/core/system.toml",
        'schema_version = 1\nname = "core"\nfiles = ["app.py"]\n',
    )
    fixture = Path(__file__).parent / "../examples/synthetic-graphs/small-workflow.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    document["extensions"] = {"minotaur": {"selection": selection}}
    graph = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _write(root, "graph.json", graph)
    _write(root, "graph.json.sha256", (graph_digest(graph) + "\n").encode("ascii"))
    _run(root, "add", ".")
    commit = _run(root, "commit", "--quiet", "-m", "historical snapshot")
    sha = _run(root, "rev-parse", "HEAD")
    assert commit == ""
    return root, sha, graph


def test_load_historical_inputs_composes_one_pinned_snapshot(tmp_path: Path) -> None:
    root, sha, graph_bytes = _repository(tmp_path)

    result = load_historical_inputs(root, ".minotaur.toml")

    assert result.commit == sha
    assert result.config_coordinate == ".minotaur.toml"
    assert result.normalized_root == "."
    assert result.normalized_graph == "graph.json"
    assert result.normalized_systems_dir == "docs/systems"
    assert result.normalized_targets == ("app.py",)
    assert result.selection == ("app.py",)
    assert result.graph_bytes == graph_bytes
    assert result.sidecar == (graph_digest(graph_bytes) + "\n").encode("ascii")
    assert result.systems[0].name == "core"
    assert result.systems[0].definition_directory == root / "docs/systems/core"
    assert result.pin.entry("app.py") is not None


def test_returned_pin_proves_old_entry_after_head_moves(tmp_path: Path) -> None:
    root, sha, _ = _repository(tmp_path)
    result = load_historical_inputs(root, ".minotaur.toml")

    (root / "app.py").unlink()
    _run(root, "add", "-A")
    _run(root, "commit", "--quiet", "-m", "delete target")

    assert result.commit == sha
    assert result.pin.entry("app.py") is not None
    assert git.PinnedCommit.pin(root).entry("app.py") is None


@pytest.mark.parametrize(
    "raw",
    [None, "app.py", [], ["app.py", 3], [""], ["/app.py"], ["../app.py"]],
)
def test_historical_selection_rejects_malformed_values_through_acquisition(
    tmp_path: Path, raw: object
) -> None:
    root, _, _ = _repository(tmp_path, selection=raw)

    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")

    assert error.value.historical_side == "historical"
    assert error.value.pinned_sha
    assert "selection" in error.value.path


def test_strict_selection_is_order_and_duplicate_neutral() -> None:
    assert validate_saved_selection(["app.py", "./app.py", "app.py"], {"app.py"}) == ("app.py",)
    assert validate_saved_selection(["."], {"."}) == (".",)


def test_missing_sidecar_fails_before_graph_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _run(root, "rm", "--quiet", "graph.json.sha256")
    _run(root, "commit", "--quiet", "-m", "remove graph sidecar")
    called = False

    def forbidden(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("canonical graph loader must not receive a missing sidecar")

    monkeypatch.setattr("minotaur.comparison.loading.load_graph_blob", forbidden)
    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")

    assert not called
    assert "graph.json.sha256" in error.value.path


def test_route_link_is_rejected_even_when_definition_is_absent(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "docs/systems").rename(root / "actual-systems")
    (root / "docs/systems").symlink_to(root / "actual-systems", target_is_directory=True)
    _run(root, "add", "-A")
    _run(root, "commit", "--quiet", "-m", "link systems")

    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")

    assert "symbolic link" in str(error.value)
    assert "docs/systems" in error.value.path


@pytest.mark.parametrize(
    ("target", "selection", "expected"),
    [
        ("missing.py", ["missing.py"], "success"),
        ("docs/../app.py", ["app.py"], "success"),
        ("missing/../app.py", ["app.py"], "error"),
        ("graph.json/child", ["graph.json/child"], "error"),
        ("/app.py", ["app.py"], "error"),
        ("../app.py", ["app.py"], "error"),
    ],
)
def test_target_route_matrix_walks_raw_components_before_normalization(
    tmp_path: Path,
    target: str,
    selection: object,
    expected: str,
) -> None:
    root, _, _ = _repository(tmp_path)
    _set_config(root, targets=[target])
    _set_selection(root, selection)
    _commit(root, "route matrix")

    if expected == "success":
        result = load_historical_inputs(root, ".minotaur.toml")
        assert result.commit == _run(root, "rev-parse", "HEAD")
        expected_target = "missing.py" if target == "missing.py" else "app.py"
        assert result.target_coordinates == (expected_target,)
    else:
        with pytest.raises(HistoricalInputError) as error:
            load_historical_inputs(root, ".minotaur.toml")
        assert error.value.historical_side == "historical"
        assert error.value.pinned_sha
        assert error.value.path


def test_absolute_declaration_is_lexical_and_rejects_an_outside_path(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    _set_config(root, targets=["app.py"], root_value=str(root / "missing" / ".."))
    _commit(root, "absolute lexical root")

    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")

    assert error.value.pinned_sha
    assert "missing/.." in error.value.path

    root, _, _ = _repository(tmp_path / "outside")
    _set_config(root, targets=["app.py"], root_value=str(root.parent))
    _commit(root, "absolute escape")
    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")
    assert "escapes the worktree" in str(error.value)


def test_spaces_and_gitlink_target_leaves_keep_typed_route_errors(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "app.py").rename(root / "space named.py")
    _set_config(root, targets=["space named.py"])
    _set_selection(root, ["space named.py"])
    _commit(root, "space target")
    result = load_historical_inputs(root, ".minotaur.toml")
    assert result.target_coordinates == ("space named.py",)

    root, _, _ = _repository(tmp_path / "gitlink")
    linked_commit = _run(root, "rev-parse", "HEAD")
    _run(root, "update-index", "--add", "--cacheinfo", f"160000,{linked_commit},linked")
    _set_config(root, targets=["linked"])
    _set_selection(root, ["linked"])
    _run(root, "add", ".minotaur.toml", "graph.json", "graph.json.sha256")
    _run(root, "commit", "--quiet", "-m", "gitlink target")
    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")
    assert "gitlink" in str(error.value)
    assert error.value.pinned_sha


def test_malformed_definition_is_attributed_and_publishes_no_partial_set(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    _write(
        root,
        "docs/systems/core/system.toml",
        'schema_version = 1\nname = "core"\nfiles = []\n',
    )
    _commit(root, "invalid definition")

    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")

    assert error.value.historical_side == "historical"
    assert error.value.pinned_sha
    assert error.value.path.endswith("docs/systems/core/system.toml")


def test_graph_sidecar_mismatch_uses_canonical_fallback_and_bad_graph_fails(
    tmp_path: Path,
) -> None:
    root, _, graph = _repository(tmp_path)
    _write(root, "graph.json.sha256", b"wrong\n")
    _commit(root, "mismatched sidecar")
    result = load_historical_inputs(root, ".minotaur.toml")
    assert result.graph_bytes == graph

    root, _, _ = _repository(tmp_path / "bad-graph")
    _write(root, "graph.json", b"{")
    _write(root, "graph.json.sha256", (hashlib.sha256(b"{").hexdigest() + "\n").encode())
    _commit(root, "malformed graph")
    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")
    assert error.value.pinned_sha
    assert error.value.path == "graph.json"


def test_pin_failure_precedes_any_historical_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    observed: list[str] = []

    def fail_pin(path: Path) -> git.PinnedCommit:
        observed.append(str(path))
        raise git.GitInputError(side="historical", commit=None, path=str(path), detail="pin failed")

    def forbidden_entries(self: git.PinnedCommit, relative: str = "") -> tuple[git.TreeEntry, ...]:
        raise AssertionError(f"observation before pin: {relative}")

    monkeypatch.setattr(git.PinnedCommit, "pin", fail_pin)
    monkeypatch.setattr(git.PinnedCommit, "entries", forbidden_entries)
    with pytest.raises(git.GitInputError):
        load_historical_inputs(root, ".minotaur.toml")
    assert observed == [str(root)]


def test_pin_survives_head_move_during_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha, _ = _repository(tmp_path)
    original_entries = git.PinnedCommit.entries
    moved = False

    def move_head(self: git.PinnedCommit, relative: str = "") -> tuple[git.TreeEntry, ...]:
        nonlocal moved
        if not moved:
            moved = True
            (root / "app.py").write_text("def app():\n    return 2\n", encoding="utf-8")
            _commit(root, "move symbolic head")
        return original_entries(self, relative)

    monkeypatch.setattr(git.PinnedCommit, "entries", move_head)
    result = load_historical_inputs(root, ".minotaur.toml")
    assert moved
    assert result.commit == sha
    assert result.pin.entry("app.py") is not None
    assert _run(root, "rev-parse", "HEAD") != sha


def test_acquisition_preserves_input_inventory_bytes_and_status(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    status_before = _run(root, "status", "--porcelain")

    result = load_historical_inputs(root, ".minotaur.toml")

    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert result.commit
    assert after == before
    assert _run(root, "status", "--porcelain") == status_before


def test_non_root_analysis_selection_and_nested_config_anchors(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path / "rooted")
    analysis = _relocate_snapshot(root, "proj")
    _set_config(root, targets=["app.py"], root_value="proj")
    _commit(root, "non-root analysis")

    result = load_historical_inputs(root, ".minotaur.toml")
    assert result.normalized_root == "proj"
    assert result.normalized_graph == "proj/graph.json"
    assert result.normalized_systems_dir == "proj/docs/systems"
    assert result.normalized_targets == ("proj/app.py",)
    assert result.selection == ("app.py",)
    assert result.systems[0].definition_directory == analysis / "docs/systems/core"

    nested_root = _repository(tmp_path / "nested")[0]
    nested = _relocate_snapshot(nested_root, "config/project")
    (nested_root / ".minotaur.toml").unlink()
    _write(
        nested_root,
        "config/.minotaur.toml",
        "[minotaur]\n"
        "schema_version = 1\n"
        'root = "project"\n'
        'graph = "graph.json"\n'
        'targets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )
    _commit(nested_root, "nested config anchor")
    result = load_historical_inputs(nested_root, "config/.minotaur.toml")
    assert result.normalized_root == "config/project"
    assert result.normalized_graph == "config/project/graph.json"
    assert result.normalized_systems_dir == "config/project/docs/systems"
    assert result.normalized_targets == ("config/project/app.py",)
    assert result.selection == ("app.py",)
    assert result.systems[0].definition_directory == nested / "docs/systems/core"


def test_non_root_target_cannot_escape_analysis_root(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    _relocate_snapshot(root, "proj")
    _write(root, "sibling.py", "def sibling():\n    return 1\n")
    _set_config(root, targets=["../sibling.py"], root_value="proj")
    _commit(root, "target outside analysis root")

    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")
    assert error.value.historical_side == "historical"
    assert error.value.pinned_sha
    assert error.value.path == "../sibling.py"
    assert "analysis root" in str(error.value)


def test_strict_selection_rejects_member_tolerant_view_drops(tmp_path: Path) -> None:
    root, _, graph = _repository(tmp_path, selection=["app.py", 7])
    sidecar = (graph_digest(graph) + "\n").encode("ascii")
    document = loading.load_graph_blob(graph, sidecar).document
    view = recorded_selection_view(document)
    assert view.recorded is True
    assert view.targets == ("app.py",)

    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")
    assert error.value.pinned_sha
    assert "selection" in error.value.path
    assert "strings" in str(error.value)


@pytest.mark.parametrize("unavailable", [False, True])
def test_failed_tree_listing_is_attributed_and_never_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    root, _, _ = _repository(tmp_path)
    original = git.run_git

    def failed_listing(
        worktree: Path, arguments: tuple[str, ...], *, text: bool = True
    ) -> subprocess.CompletedProcess[object] | None:
        if arguments[:2] == ("ls-tree", "-z"):
            if unavailable:
                return None
            return subprocess.CompletedProcess(
                ["git", *arguments], 128, b"", b"fatal: Not a valid object name\n"
            )
        return original(worktree, arguments, text=text)

    monkeypatch.setattr(git, "run_git", failed_listing)
    with pytest.raises(git.GitInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")
    assert error.value.historical_side == "historical"
    assert error.value.pinned_sha
    assert error.value.path == "."
    if unavailable:
        assert "unavailable" in str(error.value)
    else:
        assert "Not a valid object name" in str(error.value)


def test_graph_directory_fails_before_canonical_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "graph.json").unlink()
    (root / "graph.json").mkdir()
    _write(root, "graph.json/marker", "tree\n")
    _commit(root, "graph is a directory")
    called = False

    def forbidden(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("wrong-type graph must fail before loading")

    monkeypatch.setattr("minotaur.comparison.loading.load_graph_blob", forbidden)
    with pytest.raises(HistoricalInputError) as error:
        load_historical_inputs(root, ".minotaur.toml")
    assert not called
    assert error.value.path == "graph.json"
    assert "regular file" in str(error.value)


@pytest.mark.parametrize("ordinary", [False, True])
def test_missing_or_ordinary_systems_root_is_empty(tmp_path: Path, ordinary: bool) -> None:
    root, _, _ = _repository(tmp_path)
    if ordinary:
        _write(root, "systems-file", "narrative\n")
        systems_dir = "systems-file"
    else:
        systems_dir = "missing-systems"
    _set_config(root, targets=["app.py"], root_value=".")
    _write(
        root,
        ".minotaur.toml",
        "[minotaur]\n"
        'schema_version = 1\nroot = "."\n'
        'graph = "graph.json"\n'
        'targets = ["app.py"]\n'
        f"systems_dir = {json.dumps(systems_dir)}\n",
    )
    _commit(root, "empty systems root")
    assert load_historical_inputs(root, ".minotaur.toml").systems == ()


def test_system_child_and_definition_links_and_gitlinks_are_rejected(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path / "child-link")
    (root / "docs/systems/core").rename(root / "actual-core")
    (root / "docs/systems/core").symlink_to(root / "actual-core", target_is_directory=True)
    _commit(root, "linked system child")
    with pytest.raises(HistoricalInputError) as child_error:
        load_historical_inputs(root, ".minotaur.toml")
    assert child_error.value.path == "docs/systems/core"
    assert "symbolic link" in str(child_error.value)

    root, _, _ = _repository(tmp_path / "definition-link")
    definition = root / "docs/systems/core/system.toml"
    definition.rename(root / "definition-copy.toml")
    definition.symlink_to("../../../definition-copy.toml")
    _commit(root, "linked system definition")
    with pytest.raises(HistoricalInputError) as definition_error:
        load_historical_inputs(root, ".minotaur.toml")
    assert definition_error.value.path == "docs/systems/core/system.toml"
    assert "symbolic link" in str(definition_error.value)

    root, _, _ = _repository(tmp_path / "child-gitlink")
    linked_commit = _run(root, "rev-parse", "HEAD")
    _run(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{linked_commit},docs/systems/linked",
    )
    _run(root, "commit", "--quiet", "-m", "gitlink system child")
    with pytest.raises(HistoricalInputError) as gitlink_error:
        load_historical_inputs(root, ".minotaur.toml")
    assert gitlink_error.value.path == "docs/systems/linked"
    assert "gitlink" in str(gitlink_error.value)

    root, _, _ = _repository(tmp_path / "definition-gitlink")
    linked_commit = _run(root, "rev-parse", "HEAD")
    _run(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{linked_commit},docs/systems/core/system.toml",
    )
    _run(root, "commit", "--quiet", "-m", "gitlink system definition")
    with pytest.raises(HistoricalInputError) as gitlink_error:
        load_historical_inputs(root, ".minotaur.toml")
    assert gitlink_error.value.path == "docs/systems/core/system.toml"
    assert "gitlink" in str(gitlink_error.value)


def test_definition_listing_and_read_fail_before_finalizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path / "listing")
    original_entries = git.PinnedCommit.entries
    finalizer_called = False

    def fail_listing(self: git.PinnedCommit, relative: str = "") -> tuple[git.TreeEntry, ...]:
        if relative == "docs/systems":
            raise git.GitInputError(
                side="historical", commit=self.commit, path=relative, detail="listing unavailable"
            )
        return original_entries(self, relative)

    def forbidden_finalizer(*args: object, **kwargs: object) -> object:
        nonlocal finalizer_called
        finalizer_called = True
        raise AssertionError("failed discovery must not finalize partial definitions")

    monkeypatch.setattr(git.PinnedCommit, "entries", fail_listing)
    monkeypatch.setattr(system, "load_systems_data", forbidden_finalizer)
    with pytest.raises(git.GitInputError) as listing_error:
        load_historical_inputs(root, ".minotaur.toml")
    assert listing_error.value.path == "docs/systems"
    assert not finalizer_called

    monkeypatch.undo()
    root, _, _ = _repository(tmp_path / "read")
    original_read = git.PinnedCommit.read_blob

    def fail_read(self: git.PinnedCommit, relative: str) -> bytes:
        if relative == "docs/systems/core/system.toml":
            raise git.GitInputError(
                side="historical", commit=self.commit, path=relative, detail="read unavailable"
            )
        return original_read(self, relative)

    monkeypatch.setattr(git.PinnedCommit, "read_blob", fail_read)
    with pytest.raises(git.GitInputError) as read_error:
        load_historical_inputs(root, ".minotaur.toml")
    assert read_error.value.path == "docs/systems/core/system.toml"
    assert not finalizer_called


def test_head_move_keeps_every_historical_value_and_entry_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha, old_graph = _repository(tmp_path)
    old_config = (root / ".minotaur.toml").read_bytes()
    old_sidecar = (root / "graph.json.sha256").read_bytes()
    old_definition = (root / "docs/systems/core/system.toml").read_bytes()
    original_entries = git.PinnedCommit.entries
    moved = False

    def move_symbolic_head(self: git.PinnedCommit, relative: str = "") -> tuple[git.TreeEntry, ...]:
        nonlocal moved
        if not moved:
            moved = True
            (root / "app.py").unlink()
            _write(root, "new.py", "def new():\n    return 2\n")
            (root / "app.py").symlink_to("new.py")
            _set_config(root, targets=["new.py"])
            _set_selection(root, ["new.py"])
            _write(
                root,
                "docs/systems/core/system.toml",
                'schema_version = 1\nname = "new-core"\nfiles = ["new.py"]\n',
            )
            _commit(root, "move symbolic head during acquisition")
        return original_entries(self, relative)

    monkeypatch.setattr(git.PinnedCommit, "entries", move_symbolic_head)
    result = load_historical_inputs(root, ".minotaur.toml")
    assert moved
    assert result.commit == sha
    assert result.config_coordinate == ".minotaur.toml"
    assert result.historical_config.targets == ("app.py",)
    assert result.graph_bytes == old_graph
    assert result.sidecar == old_sidecar
    assert result.systems[0].name == "core"
    assert result.systems[0].files == ("app.py",)
    assert result.systems[0].definition_directory == root / "docs/systems/core"
    assert result.pin.read_blob("docs/systems/core/system.toml") == old_definition
    assert result.pin.entry("app.py").is_regular_file  # type: ignore[union-attr]
    assert result.pin.read_blob("app.py") == b"def app():\n    return 1\n"
    assert (root / ".minotaur.toml").read_bytes() != old_config
    assert _run(root, "rev-parse", "HEAD") != sha


def test_historical_definition_provenance_survives_live_directory_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original_entries = git.PinnedCommit.entries
    replaced = False

    def replace_live_directory(
        self: git.PinnedCommit, relative: str = ""
    ) -> tuple[git.TreeEntry, ...]:
        nonlocal replaced
        if not replaced:
            replaced = True
            (root / "docs/systems").rename(root / "live-systems")
            (root / "docs/systems").symlink_to(root / "live-systems", target_is_directory=True)
        return original_entries(self, relative)

    monkeypatch.setattr(git.PinnedCommit, "entries", replace_live_directory)
    result = load_historical_inputs(root, ".minotaur.toml")
    assert replaced
    assert (root / "docs/systems").is_symlink()
    assert result.systems[0].definition_directory == root / "docs/systems/core"
    assert result.systems[0].name == "core"


def test_success_and_failure_preserve_bytes_index_status_and_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, _ = _repository(tmp_path / "success")
    before = _working_snapshot(root)
    load_historical_inputs(root, ".minotaur.toml")
    assert _working_snapshot(root) == before
    assert capsys.readouterr().out == ""

    root, _, _ = _repository(tmp_path / "failure")
    _run(root, "rm", "--quiet", "graph.json.sha256")
    _run(root, "commit", "--quiet", "-m", "missing sidecar")
    before = _working_snapshot(root)
    with pytest.raises(HistoricalInputError):
        load_historical_inputs(root, ".minotaur.toml")
    assert _working_snapshot(root) == before
    assert capsys.readouterr().out == ""


def test_sidecar_validation_distinguishes_trusted_and_fallback_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    original_validator = loading._validate_wire_shape

    def record_validation(raw: dict[str, object]) -> None:
        calls.append(raw)
        original_validator(raw)

    monkeypatch.setattr(loading, "_validate_wire_shape", record_validation)
    root, _, graph = _repository(tmp_path / "fallback")
    _write(root, "graph.json.sha256", b"wrong\n")
    _commit(root, "mismatched sidecar")
    load_historical_inputs(root, ".minotaur.toml")
    assert calls

    calls.clear()
    root, _, _ = _repository(tmp_path / "trusted")
    load_historical_inputs(root, ".minotaur.toml")
    assert calls == []
