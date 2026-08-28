from __future__ import annotations

from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.provenance import Provenance, RelationshipKind
from minotaur.language_interpreter.accumulation import RelationshipAccumulator


def _location(column: int) -> Location:
    return Location("app.py", Range(Position(0, column), Position(0, column + 1)))


def test_add_preserves_location_lists_and_creates_empty_entries() -> None:
    source = "node:sha256:" + "1" * 64
    target = "node:sha256:" + "2" * 64
    accumulator = RelationshipAccumulator()
    location = _location(2)

    accumulator.add(source, target, RelationshipKind.CONTAINS.value, None)
    accumulator.add(source, target, RelationshipKind.CALLS.value, location)

    assert accumulator._relationships == {
        (source, target, RelationshipKind.CONTAINS.value): [],
        (source, target, RelationshipKind.CALLS.value): [location],
    }


def test_documents_deduplicates_locations_in_insertion_order_and_attributes_producer() -> None:
    source = "node:sha256:" + "1" * 64
    target = "node:sha256:" + "2" * 64
    first = _location(2)
    second = _location(5)
    accumulator = RelationshipAccumulator()
    accumulator.add(source, target, RelationshipKind.REFERENCES.value, first)
    accumulator.add(source, target, RelationshipKind.REFERENCES.value, second)
    accumulator.add(source, target, RelationshipKind.REFERENCES.value, first)

    producer = Producer(name="test-interpreter")
    documents = accumulator.documents(producer)

    assert isinstance(documents, tuple)
    assert len(documents) == 1
    relationship = documents[0]
    assert relationship.tuple_key == (source, target, RelationshipKind.REFERENCES.value)
    evidence = relationship.evidence[0]
    assert evidence.provenance is Provenance.STATIC_ANALYSIS
    assert evidence.producer == producer
    assert evidence.locations == (first, second)
