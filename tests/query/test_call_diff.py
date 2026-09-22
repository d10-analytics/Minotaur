"""Behavioral proof for multiset call residual classification."""

from __future__ import annotations

import pytest

from minotaur.graph_model.location import Location, Position, Range
from minotaur.language_interpreter.call_expressions import CallExpressionObservation
from minotaur.query.call_diff import (
    CallCorrespondenceAmbiguityError,
    compare_call_observations,
)


def _observation(fingerprint: str | None, *, line: int = 1) -> CallExpressionObservation:
    callee = Location("app.py", Range(Position(line, 4), Position(line, 10)))
    expression = Location("app.py", Range(Position(line, 0), Position(line, 14)))
    return CallExpressionObservation("python", callee, expression, fingerprint)


def test_call_residuals_are_multisets_and_ignore_observation_order() -> None:
    old = (_observation("same"), _observation("same", line=2), _observation("other", line=3))
    new = (_observation("other", line=3), _observation("same", line=2), _observation("same"))

    comparison = compare_call_observations(old, new)

    assert [item.status for item in comparison.changes] == ["unchanged"] * 3
    assert comparison.limitations == ()
    assert not comparison.changed


def test_expression_change_and_multiplicity_do_not_pair_by_position() -> None:
    comparison = compare_call_observations(
        (_observation("old"), _observation("old")),
        (_observation("new"),),
    )

    assert len(comparison.changes) == 1
    assert comparison.changes[0].status == "changed"
    assert comparison.changes[0].reasons == ("expression_changed", "multiplicity_changed")


def test_unavailable_expression_is_a_limitation_not_proven_equality() -> None:
    comparison = compare_call_observations((_observation(None),), (_observation("new"),))

    assert comparison.changes[0].status == "unavailable"
    assert comparison.limitations[0].side == "old"
    assert comparison.limitations[0].code == "call-expression-unavailable"


def test_same_callee_site_with_multiple_graph_edges_fails_ambiguously() -> None:
    from dataclasses import replace

    from test_system_diff import _snapshot, _symbol, _systems

    from minotaur.graph_model.evidence import Evidence
    from minotaur.graph_model.provenance import Provenance
    from minotaur.graph_model.relationship import Relationship
    from minotaur.query.correspondence import prepare_whole_graph

    source = _symbol("source", "a.py")
    first = _symbol("first", "b.py")
    second = _symbol("second", "c.py")
    site = Location("a.py", Range(Position(2, 0), Position(2, 5)))
    evidence = (Evidence(Provenance.STATIC_ANALYSIS, locations=(site,)),)
    document_snapshot = _snapshot(
        (source, first, second),
        (
            Relationship(source.id, first.id, "calls", evidence),
            Relationship(source.id, second.id, "calls", evidence),
        ),
        _systems(
            ("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)), ("c.toml", "C", ("c.py",))
        ),
    )
    index = prepare_whole_graph(document_snapshot.document, side="old")
    observation = _observation("value", line=2)
    observation = replace(observation, callee_location=site)

    with pytest.raises(CallCorrespondenceAmbiguityError):
        compare_call_observations((observation,), (), old_index=index)
