"""Behavioral proof for the complete typed system comparison."""

from __future__ import annotations

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
