"""Natural proof for current acquisition and prepared pair publication."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_comparison_history import _repository

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
