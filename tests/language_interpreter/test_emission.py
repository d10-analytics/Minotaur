"""Behavioral tests for shared interpreter node emission."""

from __future__ import annotations

from unittest.mock import Mock

from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import NodeClass, RelationshipKind
from minotaur.language_interpreter.accumulation import RelationshipAccumulator
from minotaur.language_interpreter.emission import NodeEmitter


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
