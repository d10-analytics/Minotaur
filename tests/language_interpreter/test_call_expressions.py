"""Shared behavioral checks for immutable call-expression observations."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.language_interpreter.call_expressions import (
    CallExpressionObservation,
    javascript_call_fingerprint,
)
from minotaur.language_interpreter.contract import AnalysisResult
from minotaur.language_interpreter.javascript import analyze_javascript_files
from minotaur.language_interpreter.workspace import Workspace


def test_analysis_result_call_observations_are_default_empty_and_immutable() -> None:
    result = AnalysisResult(GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8))

    assert result.call_expressions == ()
    with pytest.raises(FrozenInstanceError):
        result.call_expressions = ()  # type: ignore[misc]


def test_missing_call_evidence_is_not_invented_for_unresolved_javascript_calls(tmp_path) -> None:
    path = tmp_path / "app.js"
    path.write_text('function run() { missing("value"); }\n', encoding="utf-8")

    result = analyze_javascript_files(Workspace(tmp_path), (path,))

    assert result.call_expressions == ()


def test_unavailable_fingerprint_is_explicit_on_an_observation() -> None:
    observation = CallExpressionObservation(
        language="javascript",
        callee_location=Location("app.js", Range(Position(0, 0), Position(0, 1))),
        expression_location=Location("app.js", Range(Position(0, 0), Position(0, 1))),
        fingerprint=None,
    )

    assert observation.available is False
    assert observation.structural_fingerprint is None
    assert javascript_call_fingerprint(object()) is None
