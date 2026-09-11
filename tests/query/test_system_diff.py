"""Behavioral proof for the complete typed system comparison."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    Provenance,
)
from minotaur.graph_model.relationship import Relationship
from minotaur.query import system as system_query
from minotaur.query.system import ReportingSnapshot
from minotaur.query.system_diff import compare_systems
from minotaur.system import load_systems_data


def _symbol(label: str, path: str, line: int = 0) -> Node:
    location = Location(path, Range(Position(line, 0), Position(line, 1)))
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, "test")
    return Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind="function",
            location=location,
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind="function",
        location=location,
    )


def _systems(*definitions: tuple[str, str, tuple[str, ...]]) -> tuple:
    data = {
        Path(source): {"schema_version": 1, "name": name, "files": list(files)}
        for source, name, files in definitions
    }
    return load_systems_data(data)


def _snapshot(
    nodes: tuple[Node, ...], relationships: tuple[Relationship, ...], systems: tuple
) -> ReportingSnapshot:
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=nodes,
        relationships=relationships,
    )
    return ReportingSnapshot.prepare(document, systems)


def _call(source: Node, target: Node) -> Relationship:
    return Relationship(source.id, target.id, "calls", (Evidence(Provenance.STATIC_ANALYSIS),))


def _reference(source: Node, target: Node) -> Relationship:
    return Relationship(source.id, target.id, "references", (Evidence(Provenance.STATIC_ANALYSIS),))


def _upstream(label: str, identifier: str = "remote", path: str | None = None) -> Node:
    identity = NodeIdentity(
        IdentityBasis.UPSTREAM_IDENTIFIER,
        "test",
        upstream_identifier=identifier,
    )
    return Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind="function",
            location=(
                Location(path, Range(Position(0, 0), Position(0, 1))) if path is not None else None
            ),
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind="function",
        location=(
            Location(path, Range(Position(0, 0), Position(0, 1))) if path is not None else None
        ),
    )


def _unresolved(
    origin: Node, path: str, line: int, text: str = "missing", label: str | None = None
) -> Node:
    location = Location(path, Range(Position(line, 0), Position(line, 1)))
    identity = NodeIdentity(
        IdentityBasis.UNRESOLVED_REFERENCE,
        "test",
        originating_node=origin.id,
    )
    return Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.UNRESOLVED_REFERENCE.value,
            reference_text=text,
            location=location,
        ),
        identity=identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        label=text if label is None else label,
        reference_text=text,
        location=location,
    )


def test_identical_snapshots_are_complete_and_immutable() -> None:
    source = _symbol("caller", "a.py")
    target = _symbol("entry", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    snapshot = _snapshot((source, target), (_call(source, target),), systems)

    result = compare_systems(snapshot, snapshot)

    assert not result.changed
    assert result.exit_code == 0
    assert result.old_system_names == ("A", "B")
    assert result.new_system_names == ("A", "B")
    with pytest.raises(TypeError):
        result.old_coverage["new"] = "mutation"  # type: ignore[index]


def test_membership_and_rows_retain_exact_side_evidence() -> None:
    source = _symbol("caller", "a.py")
    target = _symbol("entry", "b.py")
    old_systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    new_systems = _systems(
        ("a.toml", "A", ("a.py", "b.py")),
    )
    old = _snapshot((source, target), (_call(source, target),), old_systems)
    new = _snapshot((source, target), (_call(source, target),), new_systems)

    result = compare_systems(old, new)

    assert [(change.key, change.old, change.new) for change in result.membership_changes] == [
        (("b.py",), {"file": "b.py", "system": "B"}, {"file": "b.py", "system": "A"})
    ]
    assert [change.kind for change in result.boundary_changes] == ["membership"]
    assert result.boundary_changes[0].involved_systems == ("A", "B")
    assert result.boundary_changes[0].old["relationships"]  # type: ignore[index]
    assert result.boundary_changes[0].new["relationships"]  # type: ignore[index]


def test_boundary_addition_updates_all_native_rows_with_side_local_contributors() -> None:
    source = _symbol("caller", "a.py")
    target = _symbol("entry", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    old = _snapshot((source, target), (), systems)
    new = _snapshot((source, target), (_call(source, target),), systems)

    result = compare_systems(old, new)

    assert len(result.surface_changes) == 1
    assert len(result.consumer_changes) == 1
    assert len(result.dependency_changes) == 1
    assert len(result.boundary_changes) == 1
    assert result.surface_changes[0].involved_systems == ("A", "B")
    assert result.consumer_changes[0].new["relationships"]  # type: ignore[index]
    assert result.dependency_changes[0].new["relationships"]  # type: ignore[index]
    assert result.boundary_changes[0].kind == "added"


def test_required_ambiguity_is_attributed_to_side_and_key() -> None:
    source = _symbol("caller", "outside.py")
    first = _symbol("entry", "b.py", 0)
    second = _symbol("entry", "b.py", 1)
    systems = _systems(("b.toml", "B", ("b.py",)))
    snapshot = _snapshot((source, first, second), (_call(source, first),), systems)

    with pytest.raises(ValueError, match="ambiguous old target"):
        compare_systems(snapshot, snapshot)


def test_context_and_provenance_only_changes_are_neutral() -> None:
    source = _symbol("caller", "a.py")
    target = _symbol("entry", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    relationship = _call(source, target)
    old = _snapshot((source, target), (relationship,), systems)
    # Evidence is observational; the structural result remains unchanged.
    new_relationship = Relationship(
        source.id,
        target.id,
        "calls",
        (Evidence(Provenance.STATIC_ANALYSIS, extensions={"site": {"value": "new"}}),),
    )
    new = _snapshot((source, target), (new_relationship,), systems)

    result = compare_systems(old, new)

    assert not result.changed


def test_repeated_unresolved_occurrence_count_is_neutral_but_new_pair_changes() -> None:
    origin = _symbol("caller", "a.py")
    first = _unresolved(origin, "b.py", 0)
    repeated = _unresolved(origin, "b.py", 1)
    changed = _unresolved(origin, "b.py", 2, label="different")
    systems = _systems(("a.toml", "A", ("a.py",)))
    old = _snapshot((origin, first), (_reference(origin, first),), systems)
    same = _snapshot(
        (origin, first, repeated),
        (_reference(origin, first), _reference(origin, repeated)),
        systems,
    )
    distinct = _snapshot(
        (origin, first, changed),
        (_reference(origin, first), _reference(origin, changed)),
        systems,
    )

    assert not compare_systems(old, same).changed
    endpoint = compare_systems(old, distinct).boundary_changes
    assert len(endpoint) == 1
    assert endpoint[0].kind == "endpoint"
    assert len(endpoint[0].new["relationships"]) == 2  # type: ignore[index]


def test_admission_of_both_sides_precedes_any_eligibility_error() -> None:
    ordinary = _symbol("ordinary", "a.py")
    first = _unresolved(ordinary, "a.py", 1, "outer")
    chained = _unresolved(first, "a.py", 2, "inner")
    valid = _snapshot((ordinary, first, chained), (_reference(ordinary, chained),), ())
    invalid_relationship = Relationship(
        "node:sha256:" + "a" * 64,
        "node:sha256:" + "b" * 64,
        "references",
        (Evidence(Provenance.STATIC_ANALYSIS),),
    )
    invalid = _snapshot((ordinary,), (invalid_relationship,), ())

    with pytest.raises(ValueError) as raised:
        compare_systems(valid, invalid)
    assert raised.value.__class__.__name__ == "CorrespondenceAdmissionError"
    assert raised.value.side == "new"  # type: ignore[attr-defined]


def test_system_declarations_report_add_remove_rename_and_absent_files() -> None:
    source = _symbol("entry", "present.py")
    old_systems = _systems(
        ("a.toml", "A", ("present.py", "old_only.py")),
        ("gone.toml", "Gone", ("gone.py",)),
    )
    new_systems = _systems(
        ("a.toml", "Renamed", ("present.py", "new_only.py")),
        ("added.toml", "Added", ("added.py",)),
    )
    old = _snapshot((source,), (), old_systems)
    new = _snapshot((source,), (), new_systems)

    result = compare_systems(old, new)

    assert result.added_systems == ("Added", "Renamed")
    assert result.removed_systems == ("A", "Gone")
    assert {change.key for change in result.membership_changes} == {
        ("added.py",),
        ("gone.py",),
        ("new_only.py",),
        ("old_only.py",),
        ("present.py",),
    }


def test_unsupported_relationship_kinds_never_create_structural_changes() -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    unsupported = Relationship(
        source.id, target.id, "inherits", (Evidence(Provenance.STATIC_ANALYSIS),)
    )
    old = _snapshot((source, target), (), systems)
    new = _snapshot((source, target), (unsupported,), systems)

    result = compare_systems(old, new)

    assert not result.changed


def test_boundary_internal_transition_is_matched_in_both_directions() -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    boundary_systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    internal_systems = _systems(("a.toml", "A", ("a.py", "b.py")))
    boundary = _snapshot((source, target), (_call(source, target),), boundary_systems)
    internal = _snapshot((source, target), (_call(source, target),), internal_systems)

    forward = compare_systems(boundary, internal).boundary_changes
    reverse = compare_systems(internal, boundary).boundary_changes

    assert [change.kind for change in forward] == ["membership"]
    assert [change.kind for change in reverse] == ["membership"]
    assert forward[0].old["categories"] == ("system: A", "system: B")  # type: ignore[index]
    assert reverse[0].new["categories"] == ("system: A", "system: B")  # type: ignore[index]


def test_stable_upstream_key_label_change_is_endpoint_change() -> None:
    source = _symbol("source", "a.py")
    old_target = _upstream("old label")
    new_target = _upstream("new label")
    systems = _systems(("a.toml", "A", ("a.py",)))
    old = _snapshot((source, old_target), (_call(source, old_target),), systems)
    new = _snapshot((source, new_target), (_call(source, new_target),), systems)

    result = compare_systems(old, new)

    assert [(change.kind, change.domain) for change in result.boundary_changes] == [
        ("endpoint", "boundary")
    ]
    assert not result.surface_changes
    assert not result.consumer_changes


def test_source_key_change_is_addition_and_removal_without_continuity() -> None:
    old_source = _symbol("old source", "a.py")
    new_source = _symbol("new source", "a.py")
    target = _symbol("target", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    old = _snapshot((old_source, target), (_call(old_source, target),), systems)
    new = _snapshot((new_source, target), (_call(new_source, target),), systems)

    result = compare_systems(old, new)

    assert {change.kind for change in result.boundary_changes} == {"added", "removed"}


def test_pathless_endpoint_label_change_is_retained_without_report_row() -> None:
    source = _symbol("source", "a.py")
    old_target, new_target = _upstream("old"), _upstream("new")
    systems = _systems(("a.toml", "A", ("a.py",)))
    old = _snapshot((source, old_target), (_call(source, old_target),), systems)
    new = _snapshot((source, new_target), (_call(source, new_target),), systems)

    result = compare_systems(old, new)

    assert len(result.boundary_changes) == 1
    change = result.boundary_changes[0]
    assert change.kind == "endpoint"
    assert change.old["target_membership"] == "external"  # type: ignore[index]
    assert change.new["target_membership"] == "external"  # type: ignore[index]


def test_membership_and_endpoint_aspects_are_separate() -> None:
    source = _symbol("source", "a.py")
    old_target = _upstream("old", "remote", path="b.py")
    new_target = _upstream("new", "remote", path="a.py")
    old_systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    new_systems = _systems(
        ("a.toml", "A", ("a.py", "b.py")),
    )
    old = _snapshot((source, old_target), (_call(source, old_target),), old_systems)
    new = _snapshot((source, new_target), (_call(source, new_target),), new_systems)

    result = compare_systems(old, new)

    assert [change.kind for change in result.boundary_changes] == ["membership", "endpoint"]


@pytest.mark.parametrize("kind", ("calls", "references", "imports"))
def test_each_supported_relationship_kind_is_considered(kind: str) -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    edge = Relationship(source.id, target.id, kind, (Evidence(Provenance.STATIC_ANALYSIS),))

    result = compare_systems(
        _snapshot((source, target), (), systems),
        _snapshot((source, target), (edge,), systems),
    )

    assert len(result.boundary_changes) == 1
    assert result.boundary_changes[0].new["kind"] == kind  # type: ignore[index]
    if kind == "imports":
        assert not result.surface_changes
    else:
        assert len(result.surface_changes) == 1


def test_changed_rows_keep_disjoint_contributors_and_neighbor_absent() -> None:
    source = _symbol("source", "outside.py")
    first = _symbol("first", "b.py")
    second = _symbol("second", "b.py", 1)
    systems = _systems(("b.toml", "B", ("b.py",)))
    first_edge = _call(source, first)
    second_edge = _call(source, second)
    old = _snapshot((source, first, second), (first_edge,), systems)
    new = _snapshot((source, first, second), (first_edge, second_edge), systems)

    result = compare_systems(old, new)

    assert [change.key for change in result.surface_changes] == [("B", "b.py", "second")]
    assert result.surface_changes[0].new["relationships"][0].target.label == "second"  # type: ignore[index]
    assert [change.key for change in result.consumer_changes] == [("B", "outside.py")]
    assert len(result.consumer_changes[0].old["relationships"]) == 1  # type: ignore[index]
    assert len(result.consumer_changes[0].new["relationships"]) == 2  # type: ignore[index]
    assert not result.dependency_changes


def test_same_derived_file_shadowed_path_and_range_only_edits_are_neutral() -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    moved_range = replace(
        target,
        id=compute_node_id(
            target.identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind="function",
            location=Location("b.py", Range(Position(4, 0), Position(4, 1))),
        ),
        location=Location("b.py", Range(Position(4, 0), Position(4, 1))),
    )
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    old = _snapshot((source, target), (_call(source, target),), systems)
    new = _snapshot((source, moved_range), (_call(source, moved_range),), systems)

    assert not compare_systems(old, new).changed


def test_input_permutations_and_canonical_owner_substitution_are_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    edge = _call(source, target)
    old = _snapshot((source, target), (edge,), systems)
    new = _snapshot((source, target), (edge,), systems)
    permuted = _snapshot((target, source), (edge,), tuple(reversed(systems)))

    assert compare_systems(old, new).to_dict() == compare_systems(old, permuted).to_dict()
    original = system_query._endpoint_detail

    def substituted(document: GraphDocument, node: Node) -> system_query.EndpointDetail:
        detail = original(document, node)
        if node.id == target.id and document is new.document:
            return replace(detail, label="owner-substitution")
        return detail

    monkeypatch.setattr(system_query, "_endpoint_detail", substituted)
    assert any(change.kind == "endpoint" for change in compare_systems(old, new).boundary_changes)
