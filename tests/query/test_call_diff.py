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


def test_pure_duplicate_add_reports_multiplicity_without_expression_change() -> None:
    """A duplicate occurrence is a multiplicity change, not an expression edit."""
    comparison = compare_call_observations(
        (_observation("same"),),
        (_observation("same"), _observation("same")),
    )

    assert len(comparison.changes) == 1
    assert comparison.changes[0].status == "changed"
    assert comparison.changes[0].reasons == ("multiplicity_changed",)


@pytest.mark.parametrize("second_end", (Position(2, 6), Position(3, 5)))
def test_callee_sites_with_shared_start_and_distinct_ends_resolve_separately(
    second_end: Position,
) -> None:
    from dataclasses import replace

    from test_system_diff import _snapshot, _symbol, _systems

    from minotaur.graph_model.evidence import Evidence
    from minotaur.graph_model.provenance import Provenance
    from minotaur.graph_model.relationship import Relationship
    from minotaur.query.correspondence import prepare_whole_graph
    from minotaur.query.graph_comparison import relationship_display_id

    source = _symbol("source", "a.py")
    targets = (_symbol("first", "b.py"), _symbol("second", "c.py"))
    sites = tuple(
        Location("a.py", Range(Position(2, 0), end)) for end in (Position(2, 5), second_end)
    )
    snapshot = _snapshot(
        (source, *targets),
        tuple(
            Relationship(
                source.id,
                target.id,
                "calls",
                (Evidence(Provenance.STATIC_ANALYSIS, locations=(site,)),),
            )
            for target, site in zip(targets, sites, strict=True)
        ),
        _systems(("a.toml", "A", ("a.py",))),
    )
    index = prepare_whole_graph(snapshot.document, side="old")
    observations = tuple(
        replace(_observation(str(i)), callee_location=site) for i, site in enumerate(sites)
    )
    result = compare_call_observations(observations, observations, old_index=index, new_index=index)
    expected = {
        relationship_display_id(key): occurrences[0].relationship.evidence[0].locations[0].to_dict()
        for key, occurrences in index.relationships_by_key.items()
    }
    assert {
        change.relationship_id: change.to_dict()["before"][0]["callee"] for change in result.changes
    } == expected
    assert [change.status for change in result.changes] == ["unchanged", "unchanged"]


def test_call_memberships_prepare_system_files_without_contains_scans() -> None:
    from dataclasses import replace

    from test_system_diff import _snapshot, _symbol, _systems

    from minotaur.graph_model.evidence import Evidence
    from minotaur.graph_model.provenance import Provenance
    from minotaur.graph_model.relationship import Relationship
    from minotaur.query.correspondence import prepare_whole_graph

    scans = 0
    iterations = 0

    class CountedFiles(tuple):
        def __contains__(self, value):
            nonlocal scans
            scans += 1
            return super().__contains__(value)

        def __iter__(self):
            nonlocal iterations
            iterations += 1
            return super().__iter__()

    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    observation = _observation("call")
    snapshot = _snapshot(
        (source, target),
        (
            Relationship(
                source.id,
                target.id,
                "calls",
                (Evidence(Provenance.STATIC_ANALYSIS, locations=(observation.callee_location,)),),
            ),
        ),
        (),
    )
    index = prepare_whole_graph(snapshot.document, side="old")
    systems = tuple(
        replace(system, files=CountedFiles(system.files))
        for system in _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    )
    result = compare_call_observations((observation,), (), old_index=index, old_systems=systems)
    assert len(result.changes) == 1
    assert result.changes[0].status == "removed"
    assert result.changes[0].involved_systems == ("A", "B")
    assert scans == 0
    assert iterations == len(systems)
