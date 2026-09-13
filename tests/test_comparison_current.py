"""Natural proof for current acquisition and prepared pair publication."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from test_comparison_history import (
    _commit,
    _repository,
    _run,
    _set_config,
    _set_selection,
    _working_snapshot,
    _write,
)

from minotaur import config, git
from minotaur.cli import _produce_selection
from minotaur.comparison import CurrentInputError, prepare_comparison
from minotaur.language_interpreter.contract import Diagnostic, DiagnosticCode


def test_prepare_comparison_publishes_both_snapshots_from_real_repository(
    tmp_path: Path,
) -> None:
    root, sha, _ = _repository(tmp_path)

    prepared = prepare_comparison(root, None, _produce_selection)

    assert prepared.historical.commit == sha
    assert prepared.current.config_coordinate == ".minotaur.toml"
    assert prepared.current.normalized_root == "."
    assert prepared.current.normalized_targets == ("app.py",)
    assert prepared.current.selection == ("app.py",)
    assert prepared.old_snapshot.document is prepared.historical.graph.document
    assert prepared.new_snapshot.document.nodes
    assert prepared.new_snapshot.document.extensions["minotaur"]["selection"] == ("app.py",)
    assert not hasattr(prepared.current, "graph_bytes")
    with pytest.raises(FrozenInstanceError):
        prepared.current.selection = ("other.py",)  # type: ignore[misc]


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

    prepared = prepare_comparison(nested, None, _produce_selection)

    assert prepared.historical.worktree_root == root
    assert prepared.current.config_coordinate == ".minotaur.toml"
    assert prepared.current.normalized_root == "."


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
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "a/b/.."\n'
        'graph = "graph.json"\ntargets = ["app.py"]\n'
        'systems_dir = "docs/systems"\n',
    )

    with pytest.raises(CurrentInputError, match="current analysis root 'a'"):
        prepare_comparison(root, None, _produce_selection)


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

    prepared = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert observed == [((), (root / "app.py", root / "other.py"))]
    assert prepared.current.selection == ("app.py", "other.py")
    assert prepared.new_snapshot.document.nodes == ()


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
    prepared = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert calls == 2
    assert prepared.current.config_coordinate == ".minotaur.toml"
    assert prepared.current.config.root == "."
    assert prepared.current.config.graph == "graph.json"
    assert prepared.current.config.systems_dir == "docs/systems"
    assert prepared.current.config.targets == ("app.py",)
    assert prepared.current.normalized_root == "."
    assert prepared.current.normalized_graph == "graph.json"
    assert prepared.current.normalized_systems_dir == "docs/systems"
    assert prepared.current.normalized_targets == ("app.py",)
    assert observed == [(root, (root / "app.py",), (root / "app.py",))]
    assert prepared.new_snapshot.document.nodes


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

    prepared = prepare_comparison(root, None, _produce_selection)

    assert prepared.current.systems == ()


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

    prepared = prepare_comparison(root, None, _produce_selection)

    assert prepared.current.systems == ()


def test_prepare_comparison_rejects_current_root_and_target_mismatch_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _write(root, "other.py", "def other():\n    return 2\n")
    _set_config(root, targets=["other.py"])
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="current targets"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_rejects_current_root_mismatch_before_producer(
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
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="analysis root"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


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

    def forbidden_lstat(path: object) -> object:
        if Path(path) == graph_link:
            raise AssertionError("unused current graph must not be inspected")
        return real_lstat(path)

    monkeypatch.setattr(comparison.os, "lstat", forbidden_lstat)
    original_read_bytes = Path.read_bytes

    def forbidden_read_bytes(path: Path) -> bytes:
        if path == graph_link:
            raise AssertionError("unused current graph must not be read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    prepared = prepare_comparison(root, None, _produce_selection)

    assert prepared.current.graph_coordinate == "graph-link.json"
    assert prepared.new_snapshot.document.nodes


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


def test_prepare_comparison_rejects_unproven_untracked_deleted_target(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _set_config(root, targets=["untracked.py"])
    _set_selection(root, ["untracked.py"])
    _commit(root, "configure absent target")
    untracked = _write(root, "untracked.py", "def later():\n    return 3\n")
    untracked.unlink()
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(CurrentInputError, match="not proven at historical pin"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


def test_prepare_comparison_rejects_old_saved_selection_before_producer(
    tmp_path: Path,
) -> None:
    root, _, _ = _repository(tmp_path)
    _set_selection(root, ["other.py"])
    _commit(root, "corrupt saved selection")
    called = False

    def producer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("producer must not run")

    with pytest.raises(ValueError, match="historical"):
        prepare_comparison(root, None, producer)  # type: ignore[arg-type]
    assert not called


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

    prepared = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert prepared.current.selection == ("app.py",)
    assert len(observed) == 1
    workspace_root, targets, metadata_targets = observed[0]
    assert workspace_root == root
    assert targets == (root / "app.py",)
    assert metadata_targets == (root / "app.py",)


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

    prepared = prepare_comparison(root, None, producer)  # type: ignore[arg-type]

    assert prepared.current.selection == ("app.py",)
    assert observed == [((), (root / "app.py",))]
    assert prepared.new_snapshot.document.nodes == ()


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
    prepared = prepare_comparison(root, None, _produce_selection)

    assert calls == 2
    assert prepared.current.normalized_targets == ("app.py",)
    assert prepared.current.config.targets == ("app.py",)


def test_prepare_comparison_keeps_pinned_history_when_head_moves_during_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, old_sha, old_graph = _repository(tmp_path)
    original_entries = git.PinnedCommit.entries
    moved = False

    def move_head_once(pinned: git.PinnedCommit, relative: str = "") -> tuple[git.TreeEntry, ...]:
        nonlocal moved
        if not moved:
            moved = True
            _write(root, "new.py", "def new():\n    return 4\n")
            _commit(root, "advance current head during pair acquisition")
        return original_entries(pinned, relative)

    monkeypatch.setattr(git.PinnedCommit, "entries", move_head_once)
    prepared = prepare_comparison(root, None, _produce_selection)

    assert moved
    assert prepared.historical.commit == old_sha
    assert prepared.historical.graph_bytes == old_graph
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
