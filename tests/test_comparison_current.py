"""Natural proof for current acquisition and prepared pair publication."""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from minotaur import cli, comparison, config, git
from minotaur.cli import _produce_selection
from minotaur.comparison import CurrentInputError, prepare_comparison, validate_saved_selection
from minotaur.comparison_snapshot import SnapshotMutationError
from minotaur.graph_model.loading import graph_digest
from minotaur.graph_model.serialization import serialize
from minotaur.language_interpreter.contract import Diagnostic, DiagnosticCode
from minotaur.language_interpreter.sql import interpreter as sql_interpreter


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


def test_strict_selection_is_order_and_duplicate_neutral() -> None:
    assert validate_saved_selection(["app.py", "./app.py", "app.py"], {"app.py"}) == ("app.py",)
    assert validate_saved_selection(["."], {"."}) == (".",)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "saved graph selection is missing or not a list/tuple"),
        ("app.py", "saved graph selection is missing or not a list/tuple"),
        ([], "saved graph selection must not be empty"),
        (["app.py", 3], "saved graph selection members must all be strings"),
        (["app.py", 7], "saved graph selection members must all be strings"),
        ([""], "selection members must be non-empty relative paths"),
        (["/app.py"], "selection members must be non-empty relative paths"),
        (["../app.py"], "selection member escapes the repository root"),
        (
            ["other.py"],
            "saved graph selection does not match historical targets: ['other.py'] != ['app.py']",
        ),
    ],
)
def test_validate_saved_selection_rejects_malformed_values(raw: object, message: str) -> None:
    with pytest.raises(ValueError) as error:
        validate_saved_selection(raw, {"app.py"})
    assert str(error.value) == message


def test_prepare_comparison_ignores_committed_graph_and_sidecar(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "graph.json", "this is not a graph\n")
    _write(root, "graph.json.sha256", "not a digest\n")
    _commit(root, "corrupt saved graph and sidecar")

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert old_snapshot.document.nodes
    assert new_snapshot.document.nodes


def test_prepare_comparison_publishes_both_snapshots_from_real_repository(
    tmp_path: Path,
) -> None:
    root, sha, _ = _repository(tmp_path)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert before.commit == sha
    assert after.config_coordinate == ".minotaur.toml"
    assert after.normalized_root == "."
    assert after.normalized_targets == ("app.py",)
    assert after.selection == ("app.py",)
    assert old_snapshot.document.nodes
    assert new_snapshot.document.nodes
    assert new_snapshot.document.extensions["minotaur"]["selection"] == ("app.py",)
    with pytest.raises(FrozenInstanceError):
        after.selection = ("other.py",)  # type: ignore[misc]


def test_prepare_comparison_preserves_each_captured_sql_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["migrations"]\n[minotaur.sql]\n'
        'migration_patterns = ["migrations/**/*.sql"]\n'
        'foreign_key_target_files = { "Parent" = "schema/parent.sql" }\n',
    )
    _write(root, "migrations/001.sql", "CREATE TABLE base (id int)\n")
    _write(root, "migrations/nested/002.sql", "CREATE TABLE nested (id int)\n")
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit(root, "sql baseline")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["migrations"]\n[minotaur.sql]\n'
        'migration_patterns = ["migrations/*.sql"]\n'
        'foreign_key_target_files = { "PARENT" = "schema/parent-after.sql" }\n',
    )

    observed = []
    original = sql_interpreter.analyze_view_warnings

    def observe(document, settings):
        observed.append(settings)
        return original(document, settings)

    monkeypatch.setattr(sql_interpreter, "analyze_view_warnings", observe)
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert new_snapshot.document.nodes
    assert [settings.migration_patterns for settings in observed] == [
        ("migrations/**/*.sql",),
        ("migrations/*.sql",),
    ]
    assert [dict(settings.foreign_key_target_files) for settings in observed] == [
        {"parent": "schema/parent.sql"},
        {"parent": "schema/parent-after.sql"},
    ]
    recursive, direct_only = observed
    assert recursive.matches_migration("migrations/001.sql")
    assert recursive.matches_migration("migrations/nested/002.sql")
    assert direct_only.matches_migration("migrations/001.sql")
    assert not direct_only.matches_migration("migrations/nested/002.sql")


def test_prepare_comparison_rejects_lexical_config_traversal_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="unresolved traversal"):
        prepare_comparison(root, Path("missing/../.minotaur.toml"), producer)  # type: ignore[arg-type]
    assert not called


@pytest.mark.parametrize("route", ["link", "directory", "blocked"])
def test_prepare_comparison_rejects_nonregular_or_blocked_config_route(
    tmp_path: Path, route: str
) -> None:
    root, _, _ = _repository(tmp_path)
    if route == "link":
        selected = root / "config-link.toml"
        selected.symlink_to(root / ".minotaur.toml")
    elif route == "directory":
        selected = root / "config-directory.toml"
        selected.mkdir()
    else:
        blocked = _write(root, "blocked", "not a directory")
        selected = blocked / ".minotaur.toml"
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="(symbolic link|ordinary file|blocked)"):
        prepare_comparison(root, selected.relative_to(root), producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_uses_raw_worktree_for_nested_start_and_discovery(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    nested = root / "nested" / "work"
    nested.mkdir(parents=True)

    worktree, _, after, _, _ = prepare_comparison(nested, None, _produce_selection)

    assert worktree == root
    assert after.config_coordinate == ".minotaur.toml"
    assert after.normalized_root == "."


def test_prepare_comparison_rejects_nested_repository_crossed_by_dotdot_routes(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    _run(nested, "init", "--quiet")
    start = root / "nested/../work"
    start.mkdir()
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="nested repository"):
        prepare_comparison(start, None, producer)  # type: ignore[arg-type]
    with pytest.raises(CurrentInputError, match="nested repository"):
        prepare_comparison(start, Path("nested/../.minotaur.toml"), producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_preserves_terminal_dotdot_route_coordinate(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "a/b").mkdir(parents=True)
    _write(root, "a/app.py", "def app():\n    return 2\n")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "a/b/.."\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )
    assert before.normalized_root == "."
    assert after.normalized_root == "a"


def test_prepare_comparison_rejects_unsupported_regular_target_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "README.md", "narrative\n")
    _set_config(root, targets=["README.md"])
    _set_selection(root, ["README.md"])
    _commit(root, "configure unsupported target")
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("unsupported target must stop before production")

    with pytest.raises(CurrentInputError, match="unsupported source file"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_all_deleted_targets_keep_complete_metadata(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "other.py", "def other():\n    return 2\n")
    _set_config(root, targets=["app.py", "other.py"])
    _set_selection(root, ["app.py", "other.py"])
    _commit(root, "configure two targets")
    (root / "app.py").unlink()
    (root / "other.py").unlink()
    observed: list[tuple[tuple[Path, ...], tuple[Path, ...] | None]] = []

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        observed.append((targets, metadata_targets))
        return _produce_selection(workspace_root, targets, metadata_targets)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert len(observed) == 2
    assert observed[0][0]
    assert observed[0][0] == observed[0][1]
    assert observed[1][0] == ()
    assert observed[1][1]
    assert after.selection == ("app.py", "other.py")
    assert new_snapshot.document.nodes == ()


def test_prepare_comparison_rejects_malformed_selection_without_diagnostics(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        workspace, selection, result = _produce_selection(workspace_root, targets, metadata_targets)
        return (
            workspace,
            selection,
            replace(result, document=replace(result.document, extensions={})),
        )

    with pytest.raises(CurrentInputError, match="invalid produced selection") as error:
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert error.value.diagnostics == ()


def test_prepare_comparison_rejects_systems_root_link_and_blocked_routes(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    systems = root / "systems-link"
    systems.symlink_to(root / "docs/systems", target_is_directory=True)
    _set_config(root, targets=["app.py"])
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        'systems_dir = "systems-link"\n',
    )
    with pytest.raises(CurrentInputError, match="symbolic link"):
        prepare_comparison(root, None, _produce_selection)

    blocked = _write(root, "systems-blocked", "file")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        'systems_dir = "systems-blocked/child"\n',
    )
    assert blocked.is_file()
    with pytest.raises(CurrentInputError, match="blocked"):
        prepare_comparison(root, None, _produce_selection)


def test_prepare_comparison_attributes_special_systems_route_failure(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    special = root / "systems-special"
    os.mkfifo(special)
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        'systems_dir = "systems-special"\n',
    )
    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)
    assert error.value.path == str(special)
    assert error.value.cause_type is None


def test_prepare_comparison_rejects_special_explicit_target_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    special = root / "special-target"
    os.mkfifo(special)
    _set_config(root, targets=["special-target"])
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("special target must stop before production")

    with pytest.raises(CurrentInputError, match="ordinary file or directory") as error:
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert error.value.path == str(special)
    assert not called


def test_prepare_comparison_preserves_lstat_permission_cause_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    target = root / "app.py"
    import minotaur.comparison as comparison

    original_lstat = comparison.os.lstat

    def denied(path: object) -> os.stat_result:
        if Path(path) == target:
            raise PermissionError("target inspection denied")
        return original_lstat(path)

    monkeypatch.setattr(comparison.os, "lstat", denied)
    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)
    assert error.value.side == "current"
    assert error.value.path == str(target)
    assert error.value.cause_type == "PermissionError"
    assert isinstance(error.value.__cause__, PermissionError)
    assert str(error.value.__cause__) == "target inspection denied"


def test_prepare_comparison_preserves_system_read_permission_cause_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    definition = root / "docs/systems/core/system.toml"
    original_read_bytes = Path.read_bytes

    def denied(path: Path) -> bytes:
        if path == definition:
            raise PermissionError("system definition read denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)
    assert error.value.side == "current"
    assert error.value.path == str(definition)
    assert error.value.cause_type == "PermissionError"
    assert isinstance(error.value.__cause__, PermissionError)
    assert str(error.value.__cause__) == "system definition read denied"


def test_prepare_comparison_retains_every_captured_config_coordinate_after_live_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original_parse = config.parse_config_bytes
    calls = 0
    observed: list[tuple[Path, tuple[Path, ...], tuple[Path, ...] | None]] = []

    def parse_once(data: bytes, *, source: Path | str):
        nonlocal calls
        parsed = original_parse(data, source=source)
        calls += 1
        if calls == 1:
            _write(root, "changed-root/changed.py", "def changed():\n    return 5\n")
            _write(
                root,
                "changed-systems/core/system.toml",
                'schema_version = 1\nname = "changed"\nfiles = ["changed.py"]\n',
            )
            _write(root, "changed-graph.json", b"unreadable current graph\n")
            _set_config(root, targets=["changed-root/changed.py"], root_value="changed-root")
            _write(
                root,
                ".minotaur.toml",
                '[minotaur]\nschema_version = 1\nroot = "changed-root"\n'
                'graph = "changed-graph.json"\ntargets = ["changed.py"]\n'
                'systems_dir = "changed-systems"\n',
            )
        return parsed

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        observed.append((workspace_root, targets, metadata_targets))
        return _produce_selection(workspace_root, targets, metadata_targets)

    monkeypatch.setattr(config, "parse_config_bytes", parse_once)
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    # Live validation, both staged-config reads in the plan, then both analyses.
    assert calls == 5
    assert after.config_coordinate == ".minotaur.toml"
    assert after.config.root == "changed-root"
    assert after.config.graph == "changed-graph.json"
    assert after.config.systems_dir == "changed-systems"
    assert after.config.targets == ("changed.py",)
    assert after.normalized_root == "changed-root"
    assert after.normalized_graph == "changed-root/changed-graph.json"
    assert after.normalized_systems_dir == "changed-root/changed-systems"
    assert after.normalized_targets == ("changed-root/changed.py",)
    assert len(observed) == 2
    assert observed[0][0] != observed[1][0]
    assert new_snapshot.document.nodes


@pytest.mark.parametrize("systems_value", ["missing-systems", "systems-file"])
def test_prepare_comparison_treats_missing_or_nondirectory_systems_root_as_empty(
    tmp_path: Path, systems_value: str
) -> None:
    root, _, _ = _repository(tmp_path)
    if systems_value == "systems-file":
        _write(root, systems_value, "ordinary file")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        f'systems_dir = "{systems_value}"\n',
    )

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert after.systems == ()


def test_prepare_comparison_rejects_linked_current_system_definition(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    definition = root / "docs/systems/core/system.toml"
    real_definition = root / "docs/systems/core/system-real.toml"
    definition.rename(real_definition)
    definition.symlink_to(real_definition)
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="symbolic link"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_rejects_linked_current_root_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    real_root = root / "real-root"
    real_root.mkdir()
    (root / "root-link").symlink_to(real_root, target_is_directory=True)
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "root-link"\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )

    with pytest.raises(CurrentInputError, match="symbolic link"):
        prepare_comparison(root, None, _produce_selection)


def test_prepare_comparison_rejects_linked_current_target_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "target-link.py").symlink_to(root / "app.py")
    _set_config(root, targets=["target-link.py"])
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="symbolic link"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_validates_complete_current_definition_set(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "docs/systems/core/system.toml", "schema_version = 1\nname = [invalid\n")
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="system.toml"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_accepts_ordinary_directory_definition_as_empty(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    definition = root / "docs/systems/core/system.toml"
    definition.unlink()
    definition.mkdir()

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert after.systems == ()


def test_prepare_comparison_accepts_a_changed_current_target_set(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "other.py", "def other():\n    return 2\n")
    _set_config(root, targets=["other.py"])
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )
    assert before.normalized_targets == ("app.py",)
    assert after.normalized_targets == ("other.py",)


def test_prepare_comparison_accepts_a_changed_current_analysis_root(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    alternate = root / "alternate"
    alternate.mkdir()
    _write(alternate, "app.py", "def app():\n    return 2\n")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "alternate"\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )
    assert before.normalized_root == "."
    assert after.normalized_root == "alternate"


def test_prepare_comparison_does_not_inspect_or_read_unused_current_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    graph_link = root / "graph-link.json"
    graph_link.symlink_to(root / "missing-graph.json")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\n'
        'graph = "graph-link.json"\ntargets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )
    import minotaur.comparison as comparison

    real_lstat = comparison.os.lstat

    def forbidden_lstat(path: object, *args: object, **kwargs: object) -> object:
        if Path(path) == graph_link:
            raise AssertionError("unused current graph must not be inspected")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(comparison.os, "lstat", forbidden_lstat)
    original_read_bytes = Path.read_bytes

    def forbidden_read_bytes(path: Path) -> bytes:
        if path == graph_link:
            raise AssertionError("unused current graph must not be read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert after.normalized_graph == "graph-link.json"
    assert new_snapshot.document.nodes


def test_prepare_comparison_rejects_current_graph_escape_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\n'
        'graph = "../outside.json"\ntargets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="configured graph escapes"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            'root = ".."\ngraph = "g.json"\ntargets = ["app.py"]\nsystems_dir = "systems"\n',
            "root",
        ),
        (
            'root = "."\ngraph = "../g.json"\ntargets = ["app.py"]\nsystems_dir = "../systems"\n',
            "graph",
        ),
        (
            'root = "."\ngraph = "g.json"\ntargets = ["app.py"]\nsystems_dir = "../systems"\n',
            "systems_dir",
        ),
    ],
)
def test_prepare_comparison_refuses_declarations_outside_the_config_folder(
    tmp_path: Path, body: str, field: str
) -> None:
    """A config below the worktree root confines every declared route."""
    root, _, _ = _repository(tmp_path)
    _write(root, "sub/.minotaur.toml", "[minotaur]\nschema_version = 1\n" + body)
    producer_calls: list[object] = []

    def producer(*args: object, **kwargs: object) -> object:
        producer_calls.append(args)
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root / "sub", None, producer)  # type: ignore[arg-type]

    assert f"configured {field} escapes the config folder (allowed folder is sub)" in str(
        error.value
    )
    assert "current input" in str(error.value)
    assert producer_calls == []


def test_prepare_comparison_allows_declarations_inside_the_config_folder(
    tmp_path: Path,
) -> None:
    """In-folder values below the worktree root compare normally."""
    root, _, _ = _repository(tmp_path)
    _write(root, "sub/app.py", "def app():\n    return 2\n")
    _write(
        root,
        "sub/docs/systems/core/system.toml",
        'schema_version = 1\nname = "core"\nfiles = ["app.py"]\n',
    )
    _write(
        root,
        "sub/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app.py"]\nsystems_dir = "docs/systems"\n',
    )
    _commit(root, "confined sub config")

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root / "sub", None, _produce_selection
    )

    assert after.normalized_root == "sub"
    assert after.normalized_graph == "sub/graph.json"
    assert after.normalized_systems_dir == "sub/docs/systems"
    assert new_snapshot.document.nodes


def test_prepare_comparison_preserves_current_parse_cause_and_path(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    config_path = _write(root, ".minotaur.toml", b"[minotaur\n")

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)

    assert error.value.path == str(config_path)
    assert error.value.cause_type == "ConfigError"
    assert isinstance(error.value.__cause__, config.ConfigError)
    assert error.value.detail == str(error.value.__cause__)


def test_prepare_comparison_keeps_the_config_error_detail_on_the_captured_side(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nroot = "."\ngraph = "graph.json"\ntargets = ["app.py"]\n',
        encoding="utf-8",
    )
    _commit(root, "bad committed config")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app.py"]\n',
        encoding="utf-8",
    )

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)

    assert error.value.side == "before"
    assert error.value.revision == "HEAD"
    assert error.value.path == ".minotaur.toml"
    assert error.value.detail == "missing required field: schema_version (in .minotaur.toml)"
    assert error.value.cause_type == "ConfigError"
    assert isinstance(error.value.__cause__, config.ConfigError)
    assert error.value.detail == str(error.value.__cause__)


def test_prepare_comparison_rejects_a_target_absent_on_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _set_config(root, targets=["untracked.py"])
    _commit(root, "configure absent target")
    untracked = _write(root, "untracked.py", "def later():\n    return 3\n")
    untracked.unlink()
    captures = _redirect_captures(tmp_path, monkeypatch)
    calls: list[object] = []

    with pytest.raises(ValueError) as error:
        prepare_comparison(root, None, _spy_producer(calls))

    assert str(error.value) == "configured target is absent on both comparison sides: untracked.py"
    assert len(calls) == 2
    assert list(captures.iterdir()) == []


def test_prepare_comparison_ignores_old_saved_selection(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _set_selection(root, ["other.py"])
    _commit(root, "corrupt saved selection")
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )
    assert old_snapshot.document.nodes
    assert new_snapshot.document.nodes


def test_prepare_comparison_passes_existing_targets_and_full_metadata_to_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    observed: list[tuple[Path, tuple[Path, ...], tuple[Path, ...] | None]] = []

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        observed.append((workspace_root, targets, metadata_targets))
        return _produce_selection(workspace_root, targets, metadata_targets)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert after.selection == ("app.py",)
    assert len(observed) == 2
    assert observed[0][0] != root
    assert observed[1][0] != root
    assert observed[0][0] != observed[1][0]
    for workspace_root, targets, metadata_targets in observed:
        assert targets == (workspace_root / "app.py",)
        assert metadata_targets == (workspace_root / "app.py",)


def test_prepare_comparison_proves_deleted_current_target_from_head_pin(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "app.py").unlink()
    observed: list[tuple[tuple[Path, ...], tuple[Path, ...] | None]] = []

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        observed.append((targets, metadata_targets))
        return _produce_selection(workspace_root, targets, metadata_targets)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert after.selection == ("app.py",)
    assert len(observed) == 2
    assert observed[0][0]
    assert observed[1][0] == ()
    assert observed[1][1]
    assert new_snapshot.document.nodes == ()


def test_prepare_comparison_reports_complete_source_diagnostics_before_metadata(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    diagnostic = Diagnostic(DiagnosticCode.PARSE_ERROR, "app.py", "broken source")

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        workspace, selection, result = _produce_selection(workspace_root, targets, metadata_targets)
        broken_document = replace(result.document, extensions={})
        return (
            workspace,
            selection,
            replace(result, document=broken_document, diagnostics=(diagnostic,)),
        )

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert error.value.path == "app.py"
    assert error.value.diagnostics == (diagnostic,)


def test_prepare_comparison_rejects_invalid_produced_graph_with_public_attribution(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        workspace, selection, result = _produce_selection(workspace_root, targets, metadata_targets)
        node = result.document.nodes[0]
        return (
            workspace,
            selection,
            replace(
                result,
                document=replace(result.document, nodes=(node, *result.document.nodes)),
            ),
        )

    with pytest.raises(CurrentInputError, match="<produced graph>"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]


def test_prepare_comparison_rejects_dangling_produced_relationship_before_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    fake_node_id = "node:sha256:" + "f" * 64
    import minotaur.comparison as comparison

    def forbidden_snapshot(*args: object, **kwargs: object) -> object:
        raise AssertionError("invalid produced graph must not build snapshots")

    monkeypatch.setattr(comparison.ReportingSnapshot, "prepare", forbidden_snapshot)

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        workspace, selection, result = _produce_selection(workspace_root, targets, metadata_targets)
        relationship = replace(result.document.relationships[0], target=fake_node_id)
        return (
            workspace,
            selection,
            replace(result, document=replace(result.document, relationships=(relationship,))),
        )

    with pytest.raises(CurrentInputError, match="<produced graph>") as error:
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert "relationship-endpoint-missing" in error.value.detail


def test_prepare_comparison_uses_actual_producer_diagnostics_for_broken_source(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "app.py").write_bytes(b"def broken(:\n")

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)

    assert error.value.path == "app.py"
    assert error.value.diagnostics
    assert error.value.diagnostics[0].code == DiagnosticCode.PARSE_ERROR


def test_prepare_comparison_attributes_worktree_diagnostics_to_after_side(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "app.py").write_bytes(b"def broken(:\n")

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)

    assert error.value.side == "after"
    assert error.value.revision == "WORKTREE"
    assert error.value.path == "app.py"
    assert error.value.diagnostics
    assert error.value.diagnostics[0].code == DiagnosticCode.PARSE_ERROR


def test_prepare_comparison_rejects_invalid_utf8_from_actual_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "app.py").write_bytes(b"\xff\xfe\x00")

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, _produce_selection)

    assert error.value.path == "app.py"
    assert error.value.diagnostics
    assert error.value.diagnostics[0].code == DiagnosticCode.SOURCE_READ_ERROR


def test_prepare_comparison_captures_current_config_once_before_live_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original_parse = config.parse_config_bytes
    calls = 0

    def parse_once(data: bytes, *, source: Path | str):
        nonlocal calls
        parsed = original_parse(data, source=source)
        calls += 1
        if calls == 1:
            _set_config(root, targets=["changed.py"])
            _write(root, "changed.py", "def changed():\n    return 3\n")
        return parsed

    monkeypatch.setattr(config, "parse_config_bytes", parse_once)
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert calls == 5
    assert after.normalized_targets == ("changed.py",)
    assert after.config.targets == ("changed.py",)


def test_prepare_comparison_keeps_pinned_history_when_head_moves_during_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, old_sha, old_graph = _repository(tmp_path)
    original_listing = git.PinnedCommit.full_listing
    moved = False

    def move_head_once(pinned: git.PinnedCommit) -> tuple[git.TreeEntry, ...]:
        nonlocal moved
        if not moved:
            moved = True
            _write(root, "new.py", "def new():\n    return 4\n")
            _commit(root, "advance current head during pair acquisition")
        return original_listing(pinned)

    monkeypatch.setattr(git.PinnedCommit, "full_listing", move_head_once)
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert moved
    assert before.commit == old_sha
    assert serialize(old_snapshot.document) != old_graph
    assert _run(root, "rev-parse", "HEAD") != old_sha


def test_prepare_comparison_preserves_worktree_and_git_state_on_success_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, _ = _repository(tmp_path / "success")
    before = _working_snapshot(root)
    prepare_comparison(root, None, _produce_selection)
    assert _working_snapshot(root) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    root, _, _ = _repository(tmp_path / "failure")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\n'
        'graph = "../outside.json"\ntargets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )
    before = _working_snapshot(root)
    with pytest.raises(CurrentInputError):
        prepare_comparison(root, None, _produce_selection)
    assert _working_snapshot(root) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_prepare_comparison_analyzes_head_and_worktree_from_separate_roots_with_untracked_source(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "app.py").rename(root / "old.py")
    _set_config(root, targets=["old.py"])
    _set_selection(root, ["old.py"])
    _write(
        root,
        "docs/systems/core/system.toml",
        'schema_version = 1\nname = "core"\nfiles = ["old.py"]\n',
    )
    _commit(root, "directory source target")
    _write(root, "src/new.py", "def new():\n    return 2\n")
    _write(
        root,
        "src/systems/current/system.toml",
        'schema_version = 1\nname = "current"\nfiles = ["new.py"]\n',
    )
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "src"\n'
        'graph = "stale.json"\ntargets = ["new.py"]\n'
        'systems_dir = "systems"\n',
    )

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    old_paths = {node.path for node in old_snapshot.document.nodes if node.path}
    new_paths = {node.path for node in new_snapshot.document.nodes if node.path}
    assert "old.py" in old_paths
    assert "old.py" not in new_paths
    assert "new.py" not in old_paths
    assert "new.py" in new_paths
    assert before.normalized_root == "."
    assert after.normalized_root == "src"
    assert before.systems[0].name == "core"
    assert after.systems[0].name == "current"
    assert all(str(root) not in str(node) for node in new_snapshot.document.nodes)


def test_prepare_comparison_ignores_stale_saved_graph_bytes(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    (root / "graph.json").write_bytes(b"this is not a graph\n")
    (root / "graph.json.sha256").write_text("not-a-digest\n", encoding="ascii")

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert old_snapshot.document.nodes
    assert new_snapshot.document.nodes
    assert serialize(old_snapshot.document) != b"this is not a graph\n"


def test_prepare_comparison_maps_absolute_declarations_into_each_capture(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(
        root,
        ".minotaur.toml",
        "[minotaur]\nschema_version = 1\n"
        f"root = {json.dumps(str(root))}\n"
        f"graph = {json.dumps(str(root / 'graph.json'))}\n"
        f"targets = {json.dumps([str(root / 'app.py')])}\n"
        f"systems_dir = {json.dumps(str(root / 'docs/systems'))}\n",
    )

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert before.normalized_root == "."
    assert after.normalized_root == "."
    assert new_snapshot.document.nodes


def test_prepare_comparison_rejects_diagnostics_before_analyzing_other_side(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    diagnostic = Diagnostic(DiagnosticCode.PARSE_ERROR, "app.py", "broken source")
    calls = 0

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        nonlocal calls
        calls += 1
        workspace, selection, result = _produce_selection(workspace_root, targets, metadata_targets)
        return workspace, selection, replace(result, diagnostics=(diagnostic,))

    with pytest.raises(CurrentInputError) as error:
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert calls == 1
    assert error.value.side == "before"
    assert error.value.diagnostics == (diagnostic,)


def test_prepare_comparison_verifies_manifest_around_each_side_analysis(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)

    def producer(
        workspace_root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> object:
        workspace, selection, result = _produce_selection(workspace_root, targets, metadata_targets)
        (workspace_root / "app.py").write_text("tampered\n", encoding="utf-8")
        return workspace, selection, result

    with pytest.raises(SnapshotMutationError) as error:
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert error.value.side == "before"
    assert error.value.pinned_sha
    assert "app.py" in str(error.value)


def _spy_capture_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    list[tuple[tuple[object, ...], functools.partial[object]]],
    list[tuple[set[str], set[str], bytes]],
]:
    """Wrap the ``capture_pair`` that ``comparison`` calls, passing through."""
    plans: list[tuple[tuple[object, ...], functools.partial[object]]] = []
    sealed: list[tuple[set[str], set[str], bytes]] = []
    original = comparison.capture_pair

    def spy(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        before, after, result = original(*args, **kwargs)  # type: ignore[arg-type]
        plan = kwargs["plan"]
        assert isinstance(plan, functools.partial)
        plans.append((args, plan))
        sealed.append(
            (
                {entry.path for entry in before.manifest.entries},
                {entry.path for entry in after.manifest.entries},
                (after.root / ".minotaur.toml").read_bytes(),
            )
        )
        return before, after, result

    monkeypatch.setattr(comparison, "capture_pair", spy)
    return plans, sealed


def test_prepare_comparison_analyzes_the_config_bytes_the_selection_was_derived_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    original_bytes = (root / ".minotaur.toml").read_bytes()
    original_parse = config.parse_config_bytes
    live = {"rewrites": 0}
    sources: list[Path] = []

    def parse_staged(data: bytes, *, source: Path | str):
        parsed = original_parse(data, source=source)
        sources.append(Path(source))
        # Parses run in this order: live validation, the plan's read of the
        # staged before config, then of the staged after config (staged before
        # this rewrite), then the two analyses. This is the before-side read.
        if len(sources) == 2:
            live["rewrites"] += 1
            _write(root, "other.py", "def other():\n    return 9\n")
            _set_config(root, targets=["other.py"])
        return parsed

    monkeypatch.setattr(config, "parse_config_bytes", parse_staged)
    _, sealed = _spy_capture_pair(monkeypatch)
    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert live["rewrites"] == 1
    assert sources[0] == root / ".minotaur.toml"
    assert root not in sources[1].parents
    assert (root / ".minotaur.toml").read_bytes() != original_bytes
    assert after.normalized_targets == ("app.py",)
    assert after.config.targets == ("app.py",)
    assert sealed[0][2] == original_bytes
    assert "other.py" not in sealed[0][1]


def test_prepare_comparison_captures_only_the_selection_through_one_planned_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "notes/readme.txt", "tracked, outside the selection\n")
    _commit(root, "add notes")
    _write(root, "other/data.txt", "untracked, outside the selection\n")
    plans, sealed = _spy_capture_pair(monkeypatch)

    prepare_comparison(root, None, _produce_selection, validate=False)

    assert len(plans) == 1
    assert plans[0][0] == (root, "HEAD", None)
    assert plans[0][1].func is comparison._comparison_plan
    expected = {
        ".minotaur.toml",
        "app.py",
        "docs",
        "docs/systems",
        "docs/systems/core",
        "docs/systems/core/system.toml",
    }
    assert sealed[0][0] == expected
    assert sealed[0][1] == expected


def test_prepare_comparison_judges_target_exclusion_from_the_analysis_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _set_config(root, targets=["pkg"], root_value=".src")
    _write(root, ".src/pkg/mod.py", "def mod():\n    return 1\n")
    _write(root, ".src/pkg/__pycache__/x.pyc", b"\x00cache")
    _commit(root, "hidden analysis root")
    _, sealed = _spy_capture_pair(monkeypatch)

    prepare_comparison(root, None, _produce_selection)

    before, after, _ = sealed[0]
    assert before == after
    assert ".src/pkg/mod.py" in before
    assert ".src/pkg/__pycache__" not in before
    assert ".src/pkg/__pycache__/x.pyc" not in before
    assert (root / ".src/pkg/__pycache__/x.pyc").exists()


def test_prepare_comparison_keeps_hidden_content_of_a_directly_declared_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _set_config(root, targets=[".tools"])
    _write(root, ".tools/run.py", "def run():\n    return 1\n")
    _write(root, ".tools/.inner/x.py", "def inner():\n    return 2\n")
    _commit(root, "hidden target")
    _, sealed = _spy_capture_pair(monkeypatch)

    prepare_comparison(root, None, _produce_selection)

    before, after, _ = sealed[0]
    assert before == after
    assert {".tools/run.py", ".tools/.inner/x.py"} <= before


def test_prepare_comparison_attributes_a_config_missing_from_head_to_the_before_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _run(root, "rm", "--cached", "--quiet", ".minotaur.toml")
    _run(root, "commit", "--quiet", "-m", "stop tracking the config")
    leftovers = tmp_path / "captures"
    leftovers.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(leftovers))

    with pytest.raises(CurrentInputError) as raised:
        prepare_comparison(root, None, _produce_selection)

    assert raised.value.side == "before"
    assert raised.value.path == ".minotaur.toml"
    assert "required config is absent" in raised.value.detail
    assert list(leftovers.iterdir()) == []


def _sealed_paths(monkeypatch: pytest.MonkeyPatch) -> list[tuple[set[str], set[str]]]:
    """Record both sealed manifest path sets of the ``capture_pair`` call."""
    sealed: list[tuple[set[str], set[str]]] = []
    original = comparison.capture_pair

    def spy(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        before, after, result = original(*args, **kwargs)  # type: ignore[arg-type]
        sealed.append(
            (
                {entry.path for entry in before.manifest.entries},
                {entry.path for entry in after.manifest.entries},
            )
        )
        return before, after, result

    monkeypatch.setattr(comparison, "capture_pair", spy)
    return sealed


def test_prepare_comparison_selects_from_an_explicit_config_off_the_discovery_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "other.py", "def other():\n    return 2\n")
    _write(
        root,
        "alt.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "alt-graph.json"\n'
        'targets = ["other.py"]\nsystems_dir = "alt-systems"\n',
    )
    _commit(root, "add an explicit alternative config")
    sealed = _sealed_paths(monkeypatch)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, Path("alt.toml"), _produce_selection
    )

    # The explicitly selected config, not the discoverable .minotaur.toml,
    # decides both sides' coordinates and both sides' selections.
    assert before.config_coordinate == "alt.toml"
    assert after.config_coordinate == "alt.toml"
    assert before.normalized_targets == ("other.py",)
    assert after.normalized_targets == ("other.py",)
    assert sealed == [({"alt.toml", "other.py"}, {"alt.toml", "other.py"})]


def test_prepare_comparison_captures_hidden_system_folders_of_an_unpruned_systems_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(
        root,
        "docs/systems/.staged/system.toml",
        'schema_version = 1\nname = "staged"\nfiles = ["staged.py"]\n',
    )
    _commit(root, "add a hidden system folder")
    sealed = _sealed_paths(monkeypatch)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    # Analysis scans every child of systems_dir, hidden ones included, so the
    # capture must not prune that subtree by the source exclusion rule.
    assert {item.name for item in before.systems} == {"core", "staged"}
    assert {item.name for item in after.systems} == {"core", "staged"}
    before, after = sealed[0]
    assert "docs/systems/.staged/system.toml" in before
    assert "docs/systems/.staged/system.toml" in after


@pytest.mark.parametrize(
    ("systems_dir", "target", "detail", "path"),
    [
        (
            "systems",
            "../app.py",
            "configured target escapes the captured analysis root",
            "lib/../app.py",
        ),
        (
            "/nonexistent-outside/systems",
            "x.py",
            "systems root escapes the selected worktree",
            "/nonexistent-outside/systems",
        ),
        (
            "systems",
            "sub/../../app.py",
            "configured target escapes the captured analysis root",
            "lib/sub/../../app.py",
        ),
    ],
)
def test_prepare_comparison_reports_an_escaping_declaration_not_a_missing_root(
    tmp_path: Path, systems_dir: str, target: str, detail: str, path: str
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "lib/x.py", "def x():\n    return 1\n")
    _write(root, "lib/sub/y.py", "def y():\n    return 2\n")
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "lib"\ngraph = "graph.json"\n'
        f'targets = ["{target}"]\nsystems_dir = "{systems_dir}"\n',
    )
    _commit(root, "root below the repository root with an escaping declaration")
    _set_config(root, targets=["app.py"])

    with pytest.raises(CurrentInputError) as raised:
        prepare_comparison(root, None, _produce_selection)

    assert raised.value.side == "before"
    assert raised.value.detail == detail
    assert raised.value.path == path


def test_prepare_comparison_keeps_hidden_systems_when_a_target_is_the_systems_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "src/core.py", "def core():\n    return 1\n")
    _write(
        root,
        "src/core/system.toml",
        'schema_version = 1\nname = "core"\nfiles = ["core.py"]\n',
    )
    _write(root, "src/staged.py", "def staged():\n    return 2\n")
    _write(
        root,
        "src/.staged/system.toml",
        'schema_version = 1\nname = "staged"\nfiles = ["staged.py"]\n',
    )
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "src"\ngraph = "graph.json"\n'
        'targets = ["."]\nsystems_dir = "."\n',
    )
    _commit(root, "systems directory that is also a target")
    sealed = _sealed_paths(monkeypatch)

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection
    )

    assert {item.name for item in before.systems} == {"core", "staged"}
    assert {item.name for item in after.systems} == {"core", "staged"}
    assert "src/.staged/system.toml" in sealed[0][0]
    assert "src/.staged/system.toml" in sealed[0][1]


def _redirect_captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send every capture directory to an empty folder the test can inspect."""
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(captures))
    return captures


def _spy_producer(calls: list[object]):  # type: ignore[no-untyped-def]
    """Wrap the real producer and keep the call expressions each side returned."""

    def producer(root, targets, metadata_targets=None, sql_settings=None):  # type: ignore[no-untyped-def]
        produced = _produce_selection(root, targets, metadata_targets, sql_settings=sql_settings)
        calls.append(produced[2].call_expressions)
        return produced

    return producer


def _strings(value: object, seen: set[int] | None = None) -> list[str]:
    """Collect every ``str`` and ``Path`` reachable from *value*."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, (str, Path)):
        return [str(value)]
    if isinstance(value, Mapping):
        return [text for item in (*value.keys(), *value.values()) for text in _strings(item, seen)]
    if isinstance(value, (tuple, list, set, frozenset)):
        return [text for item in value for text in _strings(item, seen)]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [
            text
            for field in dataclasses.fields(value)
            for text in _strings(getattr(value, field.name), seen)
        ]
    return []


def _call_repository(tmp_path: Path) -> tuple[Path, str]:
    """A repository whose HEAD and working-tree ``app.py`` differ and make calls."""
    root, sha, _ = _repository(tmp_path)
    _write(root, "app.py", "def helper():\n    return 1\n\ndef app():\n    return helper()\n")
    _commit(root, "make a call")
    sha = _run(root, "rev-parse", "HEAD")
    _write(
        root,
        "app.py",
        "def helper():\n    return 1\n\ndef app():\n    return helper() + helper()\n",
    )
    return root, sha


def test_prepare_comparison_working_tree_form_returns_symmetric_plain_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha = _call_repository(tmp_path)
    captures = _redirect_captures(tmp_path, monkeypatch)
    calls: list[object] = []

    worktree, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _spy_producer(calls)
    )

    assert worktree == root
    assert type(before) is type(after)
    assert (before.kind, before.requested_revision, before.commit) == ("commit", "HEAD", sha)
    assert (after.kind, after.requested_revision, after.commit) == ("working-tree", None, None)
    with pytest.raises(FrozenInstanceError):
        before.kind = "working-tree"  # type: ignore[misc]
    assert before.definition_files == after.definition_files == ("docs/systems/core/system.toml",)
    assert calls[0] and calls[1] and calls[0] != calls[1]
    assert before.call_observations == calls[0]
    assert after.call_observations == calls[1]
    assert before.source_bytes["app.py"] == _run(root, "show", "HEAD:app.py").encode() + b"\n"
    assert after.source_bytes["app.py"] == (root / "app.py").read_bytes()
    for record, snapshot in ((before, old_snapshot), (after, new_snapshot)):
        assert all(item.definition_directory is None for item in record.systems)
        assert len(snapshot.systems) == len(record.systems) > 0
        assert all(
            held is sanitized
            for held, sanitized in zip(snapshot.systems, record.systems, strict=True)
        )
        assert not any(str(captures) in text for text in _strings(record))
    assert list(captures.iterdir()) == []


def test_prepare_comparison_revision_pair_form_records_both_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, first = _call_repository(tmp_path)
    _commit(root, "second")
    second = _run(root, "rev-parse", "HEAD")
    captures = _redirect_captures(tmp_path, monkeypatch)
    calls: list[object] = []

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _spy_producer(calls), before=first[:12], after="HEAD"
    )

    assert (before.kind, before.requested_revision, before.commit) == ("commit", first[:12], first)
    assert (after.kind, after.requested_revision, after.commit) == ("commit", "HEAD", second)
    assert before.source_bytes["app.py"] != after.source_bytes["app.py"]
    assert before.call_observations == calls[0]
    assert after.call_observations == calls[1]
    assert not any(str(captures) in text for text in _strings((before, after)))
    assert list(captures.iterdir()) == []


def test_prepare_comparison_revision_pair_compares_without_a_live_config(
    tmp_path: Path,
) -> None:
    root, first = _call_repository(tmp_path)
    _commit(root, "second")
    (root / ".minotaur.toml").unlink()

    with pytest.raises(CurrentInputError, match="no config was found within the selected worktree"):
        prepare_comparison(root, None, _produce_selection)

    _, before, after, _, _ = prepare_comparison(
        root, None, _produce_selection, before=first, after="HEAD"
    )

    assert before.config_coordinate == after.config_coordinate == ".minotaur.toml"


def test_prepare_comparison_revision_pair_uses_each_sides_committed_config(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "a.toml", (root / ".minotaur.toml").read_text(encoding="utf-8"))
    _commit(root, "first config")
    first = _run(root, "rev-parse", "HEAD")
    _write(root, "other.py", "def other():\n    return 2\n")
    _write(
        root,
        "b.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["other.py"]\nsystems_dir = "docs/systems"\n',
    )
    _commit(root, "second config")

    _, before, after, _, _ = prepare_comparison(
        root,
        None,
        _produce_selection,
        before=first,
        after="HEAD",
        before_config="a.toml",
        after_config="b.toml",
    )

    assert (before.config_coordinate, before.normalized_targets) == ("a.toml", ("app.py",))
    assert (after.config_coordinate, after.normalized_targets) == ("b.toml", ("other.py",))


def test_prepare_comparison_revision_pair_compares_a_target_deleted_at_the_after_revision(
    tmp_path: Path,
) -> None:
    root, first = _call_repository(tmp_path)
    _run(root, "rm", "--quiet", "-f", "app.py")
    _run(root, "commit", "--quiet", "-m", "delete the target")

    _, before, after, old_snapshot, new_snapshot = prepare_comparison(
        root, None, _produce_selection, before=first, after="HEAD"
    )

    assert before.normalized_targets == after.normalized_targets == ("app.py",)
    assert old_snapshot.document.nodes
    assert new_snapshot.document.nodes == ()


@pytest.mark.parametrize("named", ["before", "after"])
def test_prepare_comparison_rejects_a_single_revision_before_any_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: str
) -> None:
    root, _, _ = _repository(tmp_path)
    captures = _redirect_captures(tmp_path, monkeypatch)
    calls: list[object] = []

    with pytest.raises(ValueError, match="either zero or exactly two revisions"):
        prepare_comparison(root, None, _spy_producer(calls), **{named: "HEAD"})  # type: ignore[arg-type]

    assert calls == []
    assert list(captures.iterdir()) == []
