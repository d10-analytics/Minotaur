"""Natural proof for the complete pinned historical acquisition."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from minotaur import git
from minotaur.comparison import (
    HistoricalInputError,
    load_historical_inputs,
    validate_saved_selection,
)
from minotaur.graph_model.loading import graph_digest


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
