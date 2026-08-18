"""Behavioral coverage for the graph slicing module (graph_model.slicing)."""

from __future__ import annotations

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence, Producer
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    Provenance,
    RelationshipKind,
    SymbolKind,
)
from minotaur.graph_model.relationship import Relationship
from minotaur.graph_model.slicing import SliceDirection, SliceResult, slice_document
from minotaur.graph_model.validation import validate_document

NS = "test"
_counter = 0


def _loc(path: str, sl: int, sc: int, el: int, ec: int) -> Location:
    return Location(path, Range(Position(sl, sc), Position(el, ec)))


def _symbol(label: str, path: str = "src/mod.py", line: int | None = None) -> Node:
    global _counter
    if line is None:
        _counter += 1
        line = _counter
    location = _loc(path, line, 0, line, 10)
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NS)
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=SymbolKind.FUNCTION.value,
        location=location,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=SymbolKind.FUNCTION.value,
        location=location,
    )


def _file(path: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, NS)
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.FILE.value,
        path=path,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.FILE,
        label=path,
        path=path,
    )


def _unresolved(origin: Node, text: str) -> Node:
    identity = NodeIdentity(
        IdentityBasis.UNRESOLVED_REFERENCE, NS, originating_node=origin.id
    )
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE.value,
        reference_text=text,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        label=text,
        reference_text=text,
    )


def _evidence(name: str = "test-tool") -> Evidence:
    return Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        producer=Producer(name=name, version="0.1"),
    )


def _rel(
    source: Node, target: Node, kind: str = RelationshipKind.CALLS.value,
) -> Relationship:
    return Relationship(source.id, target.id, kind, (_evidence(),))


def _doc(
    nodes: tuple[Node, ...], relationships: tuple[Relationship, ...] = (),
) -> GraphDocument:
    return GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=nodes,
        relationships=relationships,
    )


def _node_ids(result: SliceResult) -> set[str]:
    return {n.id for n in result.document.nodes}


def _rel_tuples(result: SliceResult) -> set[tuple[str, str, str]]:
    return {r.tuple_key for r in result.document.relationships}


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


def test_invalid_seed_raises_value_error() -> None:
    a = _symbol("a")
    doc = _doc((a,))
    with pytest.raises(ValueError, match="seed IDs not found"):
        slice_document(doc, {"nonexistent"})


def test_negative_depth_raises_value_error() -> None:
    a = _symbol("a")
    doc = _doc((a,))
    with pytest.raises(ValueError, match="max_depth must be non-negative"):
        slice_document(doc, {a.id}, max_depth=-1)


# --------------------------------------------------------------------------
# Empty and trivial cases
# --------------------------------------------------------------------------


def test_empty_seeds_returns_empty_document() -> None:
    a = _symbol("a")
    doc = _doc((a,))
    result = slice_document(doc, set())
    assert len(result.document.nodes) == 0
    assert len(result.document.relationships) == 0
    assert result.boundary_ids == frozenset()
    assert result.seed_ids == frozenset()


def test_single_isolated_node() -> None:
    a = _symbol("a")
    doc = _doc((a,))
    result = slice_document(doc, {a.id})
    assert _node_ids(result) == {a.id}
    assert len(result.document.relationships) == 0


# --------------------------------------------------------------------------
# Depth-limited traversal
# --------------------------------------------------------------------------


def test_depth_zero_returns_only_seeds() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    doc = _doc((a, b, c), (_rel(a, b), _rel(b, c)))
    result = slice_document(doc, {b.id}, max_depth=0)
    assert _node_ids(result) == {b.id}
    assert len(result.document.relationships) == 0


def test_depth_one_both_directions() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    d = _symbol("d")
    doc = _doc((a, b, c, d), (_rel(a, b), _rel(b, c), _rel(c, d)))
    result = slice_document(doc, {b.id}, max_depth=1)
    assert _node_ids(result) == {a.id, b.id, c.id}
    assert _rel_tuples(result) == {
        (a.id, b.id, RelationshipKind.CALLS.value),
        (b.id, c.id, RelationshipKind.CALLS.value),
    }


def test_unlimited_depth_returns_connected_component() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    d = _symbol("d")
    e = _symbol("e")
    doc = _doc(
        (a, b, c, d, e),
        (_rel(a, b), _rel(b, c), _rel(d, e)),
    )
    result = slice_document(doc, {a.id})
    assert _node_ids(result) == {a.id, b.id, c.id}
    assert d.id not in _node_ids(result)
    assert e.id not in _node_ids(result)


# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------


def test_outgoing_direction() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    doc = _doc((a, b, c), (_rel(a, b), _rel(b, c)))
    result = slice_document(doc, {a.id}, max_depth=1, direction=SliceDirection.OUTGOING)
    assert _node_ids(result) == {a.id, b.id}
    assert _rel_tuples(result) == {(a.id, b.id, RelationshipKind.CALLS.value)}


def test_incoming_direction() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    doc = _doc((a, b, c), (_rel(a, b), _rel(b, c)))
    result = slice_document(doc, {c.id}, max_depth=1, direction=SliceDirection.INCOMING)
    assert _node_ids(result) == {b.id, c.id}
    assert _rel_tuples(result) == {(b.id, c.id, RelationshipKind.CALLS.value)}


# --------------------------------------------------------------------------
# Boundary detection
# --------------------------------------------------------------------------


def test_boundary_ids_at_max_depth() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    d = _symbol("d")
    doc = _doc((a, b, c, d), (_rel(a, b), _rel(b, c), _rel(c, d)))
    result = slice_document(doc, {a.id}, max_depth=2, direction=SliceDirection.OUTGOING)
    assert _node_ids(result) == {a.id, b.id, c.id}
    assert result.boundary_ids == frozenset({c.id})


def test_no_boundary_when_depth_unlimited() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    doc = _doc((a, b, c), (_rel(a, b), _rel(b, c)))
    result = slice_document(doc, {a.id})
    assert result.boundary_ids == frozenset()


def test_integrity_added_node_not_in_boundary() -> None:
    seed = _symbol("seed", path="src/a.py", line=500)
    far = _symbol("far", path="src/c.py", line=600)
    far_neighbor = _symbol("far_neighbor", path="src/d.py", line=700)
    unresolved = _unresolved(far, "missing_far")

    doc = _doc(
        (seed, unresolved, far, far_neighbor),
        (
            Relationship(
                seed.id, unresolved.id, RelationshipKind.REFERENCES.value,
                (_evidence(),),
            ),
            _rel(far, far_neighbor),
        ),
    )
    result = slice_document(
        doc, {seed.id}, max_depth=1, direction=SliceDirection.OUTGOING,
    )
    assert far.id in _node_ids(result)
    assert far.id not in result.boundary_ids


def test_no_boundary_when_all_neighbors_included() -> None:
    a = _symbol("a")
    b = _symbol("b")
    doc = _doc((a, b), (_rel(a, b),))
    result = slice_document(doc, {a.id}, max_depth=1, direction=SliceDirection.OUTGOING)
    assert result.boundary_ids == frozenset()


# --------------------------------------------------------------------------
# Graph topologies
# --------------------------------------------------------------------------


def test_self_loop_at_depth_zero() -> None:
    a = _symbol("a")
    doc = _doc((a,), (_rel(a, a),))
    result = slice_document(doc, {a.id}, max_depth=0)
    assert _node_ids(result) == {a.id}
    assert _rel_tuples(result) == {(a.id, a.id, RelationshipKind.CALLS.value)}


def test_diamond_convergence() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    d = _symbol("d")
    doc = _doc(
        (a, b, c, d),
        (_rel(a, b), _rel(a, c), _rel(b, d), _rel(c, d)),
    )
    result = slice_document(doc, {a.id}, max_depth=1, direction=SliceDirection.OUTGOING)
    assert _node_ids(result) == {a.id, b.id, c.id}
    assert _rel_tuples(result) == {
        (a.id, b.id, RelationshipKind.CALLS.value),
        (a.id, c.id, RelationshipKind.CALLS.value),
    }


def test_multiple_seeds() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    d = _symbol("d")
    e = _symbol("e")
    doc = _doc(
        (a, b, c, d, e),
        (_rel(a, b), _rel(b, c), _rel(d, e)),
    )
    result = slice_document(
        doc, {a.id, d.id}, max_depth=1, direction=SliceDirection.OUTGOING,
    )
    assert _node_ids(result) == {a.id, b.id, d.id, e.id}


# --------------------------------------------------------------------------
# Evidence and provenance preservation
# --------------------------------------------------------------------------


def test_provenance_preservation() -> None:
    a = _symbol("a")
    b = _symbol("b")
    ev1 = Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        producer=Producer(name="tool-1", version="1.0"),
    )
    ev2 = Evidence(
        provenance=Provenance.IMPORTED_GRAPH,
        producer=Producer(name="tool-2", version="2.0"),
    )
    rel = Relationship(a.id, b.id, RelationshipKind.CALLS.value, (ev1, ev2))
    doc = _doc((a, b), (rel,))
    result = slice_document(doc, {a.id}, max_depth=1)
    sliced_rel = result.document.relationships[0]
    assert len(sliced_rel.evidence) == 2
    assert sliced_rel.evidence[0] is ev1
    assert sliced_rel.evidence[1] is ev2


# --------------------------------------------------------------------------
# Unresolved-reference integrity
# --------------------------------------------------------------------------


def test_unresolved_reference_pulls_in_originating_node() -> None:
    origin = _symbol("origin", path="src/a.py", line=100)
    bridge = _symbol("bridge", path="src/b.py", line=200)
    unresolved = _unresolved(origin, "missing_func")

    rel_origin_bridge = _rel(origin, bridge)
    rel_bridge_unresolved = Relationship(
        bridge.id,
        unresolved.id,
        RelationshipKind.REFERENCES.value,
        (_evidence(),),
    )

    doc = _doc(
        (origin, bridge, unresolved),
        (rel_origin_bridge, rel_bridge_unresolved),
    )

    result = slice_document(
        doc, {bridge.id}, max_depth=1, direction=SliceDirection.OUTGOING,
    )
    assert unresolved.id in _node_ids(result)
    assert origin.id in _node_ids(result)


# --------------------------------------------------------------------------
# Envelope preservation
# --------------------------------------------------------------------------


def test_envelope_metadata_preserved() -> None:
    a = _symbol("a")
    doc = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_16,
        nodes=(a,),
        generated_by=Producer(name="test-gen", version="1.0"),
        generated_at="2026-08-17T12:00:00Z",
    )
    result = slice_document(doc, {a.id})
    assert result.document.coordinate_encoding == CoordinateEncoding.UTF_16
    assert result.document.generated_by is not None
    assert result.document.generated_by.name == "test-gen"
    assert result.document.generated_at == "2026-08-17T12:00:00Z"


# --------------------------------------------------------------------------
# Result metadata
# --------------------------------------------------------------------------


def test_result_metadata() -> None:
    a = _symbol("a")
    b = _symbol("b")
    doc = _doc((a, b), (_rel(a, b),))
    result = slice_document(doc, {a.id}, max_depth=1)
    assert result.seed_ids == frozenset({a.id})
    assert result.depth == 1
    assert isinstance(result, SliceResult)


# --------------------------------------------------------------------------
# Sliced document validity
# --------------------------------------------------------------------------


def test_sliced_document_passes_semantic_validation() -> None:
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    d = _symbol("d")
    unresolved = _unresolved(c, "unknown_thing")

    doc = _doc(
        (a, b, c, d, unresolved),
        (
            _rel(a, b),
            _rel(b, c),
            _rel(c, d),
            Relationship(
                c.id,
                unresolved.id,
                RelationshipKind.REFERENCES.value,
                (_evidence(),),
            ),
        ),
    )
    original_report = validate_document(doc)
    assert original_report.is_valid

    result = slice_document(doc, {b.id}, max_depth=1)
    report = validate_document(result.document)
    assert report.is_valid, [
        (i.code.value, i.message) for i in report
    ]
