"""Behavioral tests for shared interpreter node emission."""

from __future__ import annotations

from unittest.mock import Mock

from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import IdentityBasis, NodeClass, RelationshipKind, SymbolKind
from minotaur.language_interpreter.accumulation import RelationshipAccumulator
from minotaur.language_interpreter.emission import NodeEmitter, symbol_node


def test_unresolved_deduplicates_nodes_but_accumulates_each_relationship_call() -> None:
    location = Location("app.py", Range(Position(0, 0), Position(0, 3)))
    nodes: list[Node] = []
    accumulator = RelationshipAccumulator()
    recording = Mock(wraps=accumulator)
    emitter = NodeEmitter("minotaur-python", "python")
    origin = "node:sha256:" + "0" * 64

    first = emitter.unresolved(origin, "missing", location, nodes, recording)
    second = emitter.unresolved(origin, "missing", location, nodes, recording)

    assert first == second
    assert len(nodes) == 1
    assert nodes[0].node_class == NodeClass.UNRESOLVED_REFERENCE
    assert nodes[0].language == "python"
    assert recording.add.call_count == 2
    recording.add.assert_any_call(origin, first, RelationshipKind.REFERENCES.value, location)
    relationships = accumulator.documents(Producer(name="test"))
    assert len(relationships) == 1
    assert relationships[0].kind == RelationshipKind.REFERENCES.value


def test_symbol_node_preserves_source_identity_and_extensions() -> None:
    location = Location("app.js", Range(Position(1, 2), Position(3, 4)))
    namespace = "minotaur-javascript"
    extensions = {namespace: {"export_kind": "named"}}

    node = symbol_node(
        "app.Widget",
        SymbolKind.CLASS,
        location,
        namespace,
        "javascript",
        extensions,
    )

    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, namespace)
    assert node.id == compute_node_id(
        identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=SymbolKind.CLASS.value,
        location=location,
    )
    assert node.identity == identity
    assert node.node_class is NodeClass.SYMBOL
    assert node.label == "app.Widget"
    assert node.symbol_kind == SymbolKind.CLASS.value
    assert node.language == "javascript"
    assert node.location == location
    assert node.extensions == extensions
