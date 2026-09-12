"""Natural proof for the complete pinned historical acquisition."""

from __future__ import annotations

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


def _repository(tmp_path: Path, selection: object = ["app.py"]) -> tuple[Path, str, bytes]:
    root = tmp_path / "repo"
    root.mkdir()
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
