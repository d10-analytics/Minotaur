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
from minotaur.query.system import (
    ConsumersRecord,
    RelationshipDetail,
    ReportingSnapshot,
    SurfaceRecord,
    SystemDepsRecord,
    TargetDetail,
)
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
    nodes: tuple[Node, ...],
    relationships: tuple[Relationship, ...],
    systems: tuple,
    coordinate_encoding: CoordinateEncoding = CoordinateEncoding.UTF_8,
) -> ReportingSnapshot:
    document = GraphDocument(
        coordinate_encoding=coordinate_encoding,
        nodes=nodes,
        relationships=relationships,
    )
    return ReportingSnapshot.prepare(document, systems)


def _call(source: Node, target: Node) -> Relationship:
    return Relationship(source.id, target.id, "calls", (Evidence(Provenance.STATIC_ANALYSIS),))


def _reference(source: Node, target: Node) -> Relationship:
    return Relationship(source.id, target.id, "references", (Evidence(Provenance.STATIC_ANALYSIS),))


def _upstream(
    label: str,
    identifier: str = "remote",
    path: str | None = None,
    symbol_kind: str = "function",
) -> Node:
    identity = NodeIdentity(
        IdentityBasis.UPSTREAM_IDENTIFIER,
        "test",
        upstream_identifier=identifier,
    )
    return Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind=symbol_kind,
            location=(
                Location(path, Range(Position(0, 0), Position(0, 1))) if path is not None else None
            ),
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=symbol_kind,
        location=(
            Location(path, Range(Position(0, 0), Position(0, 1))) if path is not None else None
        ),
    )


def _file(label: str, path: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, "test")
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path=path),
        identity=identity,
        node_class=NodeClass.FILE,
        label=label,
        path=path,
    )


def _resource(label: str, resource_key: str = "resource") -> Node:
    identity = NodeIdentity(IdentityBasis.RESOURCE_KEY, "test", resource_key=resource_key)
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.RESOURCE.value),
        identity=identity,
        node_class=NodeClass.RESOURCE,
        label=label,
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
    boundary = result.boundary_changes[0]
    assert boundary.old["categories"] == ("system: A", "system: B")  # type: ignore[index]
    assert boundary.new["categories"] == ("system: A", "system: A")  # type: ignore[index]
    assert boundary.old["relationships"][0].source.label == "caller"  # type: ignore[index]
    assert boundary.new["relationships"][0].target.label == "entry"  # type: ignore[index]


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
    expected_surface = SurfaceRecord("system: B", ("calls",), "b.py", "entry")
    expected_targets = (TargetDetail("entry", "b.py", "calls"),)
    assert result.surface_changes[0].new["record"] == expected_surface  # type: ignore[index]
    assert result.consumer_changes[0].new["record"] == ConsumersRecord(
        "system: A", "a.py", ("calls",), expected_targets
    )  # type: ignore[index]
    assert result.dependency_changes[0].new["record"] == SystemDepsRecord(
        "system: B", expected_targets
    )  # type: ignore[index]
    for change in (
        result.surface_changes[0],
        result.consumer_changes[0],
        result.dependency_changes[0],
    ):
        assert change.involved_systems == ("A", "B")
        assert isinstance(change.new["relationships"][0], RelationshipDetail)  # type: ignore[index]
    assert result.boundary_changes[0].involved_systems == ("A", "B")
    assert isinstance(result.boundary_changes[0].new["relationships"][0], RelationshipDetail)  # type: ignore[index]
    assert result.boundary_changes[0].kind == "added"
    assert result.boundary_changes[0].new["categories"] == ("system: A", "system: B")  # type: ignore[index]
    assert result.boundary_changes[0].new["kind"] == "calls"  # type: ignore[index]


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
    assert [item.target.label for item in endpoint[0].new["relationships"]] == [  # type: ignore[index]
        "missing",
        "different",
    ]
    target_projections = {
        pair[1]["label"]
        for pair in endpoint[0].new["projections"]  # type: ignore[index]
    }
    assert target_projections == {"missing", "different"}


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


@pytest.mark.parametrize("kind", ("contains", "inherits", "implements", "python:decorates"))
def test_unsupported_relationship_kinds_never_create_structural_changes(kind: str) -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    unsupported = Relationship(source.id, target.id, kind, (Evidence(Provenance.STATIC_ANALYSIS),))
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
    assert result.boundary_changes[0].old["target_endpoint"]["label"] == "old label"  # type: ignore[index]
    assert result.boundary_changes[0].new["target_endpoint"]["label"] == "new label"  # type: ignore[index]
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


def test_pathless_inbound_label_change_keeps_native_surface_row_equal() -> None:
    old_source, new_source = _upstream("old"), _upstream("new")
    target = _symbol("target", "a.py")
    systems = _systems(("a.toml", "A", ("a.py",)))
    old = _snapshot((old_source, target), (_call(old_source, target),), systems)
    new = _snapshot((new_source, target), (_call(new_source, target),), systems)

    result = compare_systems(old, new)

    assert not result.surface_changes
    assert not result.consumer_changes
    assert not result.dependency_changes
    assert len(result.boundary_changes) == 1
    assert result.boundary_changes[0].kind == "endpoint"
    assert result.boundary_changes[0].old["source_endpoint"]["label"] == "old"  # type: ignore[index]
    assert result.boundary_changes[0].new["source_endpoint"]["label"] == "new"  # type: ignore[index]
    assert result.boundary_changes[0].old["relationships"][0].target.label == "target"  # type: ignore[index]
    assert result.boundary_changes[0].new["relationships"][0].target.label == "target"  # type: ignore[index]


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
    assert result.boundary_changes[0].old["categories"] == ("system: A", "system: B")  # type: ignore[index]
    assert result.boundary_changes[0].new["categories"] == ("system: A", "system: A")  # type: ignore[index]
    assert result.boundary_changes[1].old["target_endpoint"]["label"] == "old"  # type: ignore[index]
    assert result.boundary_changes[1].new["target_endpoint"]["label"] == "new"  # type: ignore[index]
    for aspect in result.boundary_changes:
        assert aspect.involved_systems == ("A", "B")
        assert aspect.old["relationships"][0].source.label == "source"  # type: ignore[index]
        assert aspect.new["relationships"][0].source.label == "source"  # type: ignore[index]


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
    assert result.surface_changes[0].new["record"] == SurfaceRecord(  # type: ignore[index]
        "system: B", ("calls",), "b.py", "second"
    )
    assert result.surface_changes[0].new["relationships"][0].target.label == "second"  # type: ignore[index]
    assert [change.key for change in result.consumer_changes] == [("B", "outside.py")]
    assert result.consumer_changes[0].old["record"] == ConsumersRecord(  # type: ignore[index]
        "no_system", "outside.py", ("calls",), (TargetDetail("first", "b.py", "calls"),)
    )
    assert result.consumer_changes[0].new["record"] == ConsumersRecord(  # type: ignore[index]
        "no_system",
        "outside.py",
        ("calls",),
        (TargetDetail("first", "b.py", "calls"), TargetDetail("second", "b.py", "calls")),
    )
    assert all(
        isinstance(item, RelationshipDetail)
        for item in result.consumer_changes[0].old["relationships"]  # type: ignore[index]
    )
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
    shadowed = replace(target, path="shadowed.py")
    assert not compare_systems(
        _snapshot((source, target), (_call(source, target),), systems),
        _snapshot((source, shadowed), (_call(source, shadowed),), systems),
    ).changed


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
    old_nodes, old_relationships = old.document.nodes, old.document.relationships
    new_nodes, new_relationships = new.document.nodes, new.document.relationships

    assert compare_systems(old, new).to_dict() == compare_systems(old, permuted).to_dict()
    assert old.document.nodes == old_nodes
    assert old.document.relationships == old_relationships
    assert new.document.nodes == new_nodes
    assert new.document.relationships == new_relationships
    original = system_query._endpoint_detail

    def substituted(document: GraphDocument, node: Node) -> system_query.EndpointDetail:
        detail = original(document, node)
        if node.id == target.id and document is new.document:
            return replace(detail, label="owner-substitution")
        return detail

    monkeypatch.setattr(system_query, "_endpoint_detail", substituted)
    assert any(change.kind == "endpoint" for change in compare_systems(old, new).boundary_changes)


def test_changed_dependency_row_has_exact_typed_neighbors_and_contributors() -> None:
    source = _symbol("source", "a.py")
    old_target = _symbol("old", "outside.py")
    new_target = _symbol("new", "outside.py", 1)
    systems = _systems(("a.toml", "A", ("a.py",)))
    old = _snapshot((source, old_target), (_call(source, old_target),), systems)
    new = _snapshot((source, new_target), (_call(source, new_target),), systems)

    result = compare_systems(old, new)

    assert [change.key for change in result.dependency_changes] == [("A", "no_system")]
    change = result.dependency_changes[0]
    assert change.kind == "changed"
    assert change.old["record"] == SystemDepsRecord(  # type: ignore[index]
        "no_system", (TargetDetail("old", "outside.py", "calls"),)
    )
    assert change.new["record"] == SystemDepsRecord(  # type: ignore[index]
        "no_system", (TargetDetail("new", "outside.py", "calls"),)
    )
    assert [item.target.label for item in change.old["relationships"]] == ["old"]  # type: ignore[index]
    assert [item.target.label for item in change.new["relationships"]] == ["new"]  # type: ignore[index]
    assert change.involved_systems == ("A",)


@pytest.mark.parametrize(
    ("factory", "old_label", "new_label"),
    (("file", "old file", "new file"), ("resource", "old resource", "new resource")),
)
def test_stable_file_and_resource_keys_compare_projected_labels(
    factory: str, old_label: str, new_label: str
) -> None:
    source = _symbol("source", "a.py")
    if factory == "file":
        old_target, new_target = _file(old_label, "outside.py"), _file(new_label, "outside.py")
    else:
        old_target, new_target = _resource(old_label), _resource(new_label)
    systems = _systems(("a.toml", "A", ("a.py",)))
    old = _snapshot((source, old_target), (_call(source, old_target),), systems)
    new = _snapshot((source, new_target), (_call(source, new_target),), systems)

    result = compare_systems(old, new)

    assert [change.kind for change in result.boundary_changes] == ["endpoint"]
    change = result.boundary_changes[0]
    assert change.old["target_endpoint"]["label"] == old_label  # type: ignore[index]
    assert change.new["target_endpoint"]["label"] == new_label  # type: ignore[index]
    assert not result.surface_changes
    assert not result.consumer_changes
    category = "no_system" if factory == "file" else "external"
    path = "outside.py" if factory == "file" else None
    assert result.dependency_changes[0].new["record"] == SystemDepsRecord(  # type: ignore[index]
        category, (TargetDetail(new_label, path, "calls"),)
    )


def test_source_symbol_kind_key_change_is_addition_and_removal() -> None:
    source = _symbol("source", "a.py")
    old_target = _upstream("same", symbol_kind="function")
    new_target = _upstream("same", symbol_kind="class")
    systems = _systems(("a.toml", "A", ("a.py",)))
    result = compare_systems(
        _snapshot((source, old_target), (_call(source, old_target),), systems),
        _snapshot((source, new_target), (_call(source, new_target),), systems),
    )

    assert [
        (change.kind, change.old is None, change.new is None) for change in result.boundary_changes
    ] == [
        ("added", True, False),
        ("removed", False, True),
    ]
    assert any("'function'" in part for change in result.boundary_changes for part in change.key)
    assert any("'class'" in part for change in result.boundary_changes for part in change.key)


def test_unrelated_duplicate_candidate_does_not_block_other_boundary() -> None:
    source = _symbol("source", "a.py")
    duplicate_a = _symbol("duplicate", "b.py", 0)
    duplicate_b = _symbol("duplicate", "b.py", 1)
    external = _upstream("external")
    systems = _systems(("a.toml", "A", ("a.py",)))
    edge = _call(source, external)
    result = compare_systems(
        _snapshot((source, duplicate_a, duplicate_b, external), (edge,), systems),
        _snapshot((source, duplicate_a, duplicate_b, external), (edge,), systems),
    )

    assert not result.changed
    assert result.boundary_changes == ()


def test_node_and_relationship_observation_changes_remain_neutral() -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    observed_source = replace(
        source,
        language="python",
        expected_symbol_kind="function",
        extensions={"tool": {"revision": "new"}},
    )
    relationship = _call(source, target)
    observed_relationship = replace(
        relationship,
        extensions={"tool": {"revision": "new"}},
        evidence=(Evidence(Provenance.STATIC_ANALYSIS, extensions={"site": {"revision": "new"}}),),
    )
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))

    result = compare_systems(
        _snapshot((source, target), (relationship,), systems),
        _snapshot(
            (observed_source, target),
            (observed_relationship,),
            systems,
            CoordinateEncoding.UTF_16,
        ),
    )

    assert not result.changed
    assert result.old_coverage == result.new_coverage
    assert result.old_selection == result.new_selection
