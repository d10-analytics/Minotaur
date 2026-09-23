from __future__ import annotations

from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.provenance import Provenance, RelationshipKind
from minotaur.language_interpreter.accumulation import RelationshipAccumulator


def _location(column: int) -> Location:
    return Location("app.py", Range(Position(0, column), Position(0, column + 1)))


def test_add_preserves_empty_and_located_relationships() -> None:
    source = "node:sha256:" + "1" * 64
    target = "node:sha256:" + "2" * 64
    accumulator = RelationshipAccumulator()
    location = _location(2)

    accumulator.add(source, target, RelationshipKind.CONTAINS.value, None)
    accumulator.add(source, target, RelationshipKind.CALLS.value, location)

    documents = accumulator.documents(Producer(name="test-interpreter"))

    assert len(documents) == 2
    assert documents[0].evidence[0].locations == ()
    assert documents[0].evidence[0].extensions is None
    assert documents[1].evidence[0].locations == (location,)
    assert documents[1].evidence[0].extensions is None


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


def test_documents_groups_equal_extensions_and_keeps_distinct_payloads() -> None:
    source = "node:sha256:" + "1" * 64
    target = "node:sha256:" + "2" * 64
    first = _location(2)
    second = _location(5)
    third = _location(8)
    equal_payload = {
        "minotaur-sql": {
            "foreign_key_columns": [
                {"local": "account_id", "referenced": "id"},
            ]
        }
    }
    distinct_payload = {
        "minotaur-sql": {
            "foreign_key_columns": [
                {"local": "account_id", "referenced": "id"},
                {"local": "region_id", "referenced": "region_id"},
            ]
        }
    }
    accumulator = RelationshipAccumulator()
    accumulator.add(
        source,
        target,
        RelationshipKind.REFERENCES.value,
        first,
        equal_payload,
    )
    accumulator.add(
        source,
        target,
        RelationshipKind.REFERENCES.value,
        second,
        equal_payload,
    )
    accumulator.add(
        source,
        target,
        RelationshipKind.REFERENCES.value,
        third,
        distinct_payload,
    )
    accumulator.add(source, target, RelationshipKind.REFERENCES.value, None)

    relationship = accumulator.documents(Producer(name="test-interpreter"))[0]

    assert relationship.tuple_key == (source, target, RelationshipKind.REFERENCES.value)
    assert len(relationship.evidence) == 3
    assert relationship.evidence[0].locations == (first, second)
    assert relationship.evidence[0].to_dict()["extensions"] == equal_payload
    assert relationship.evidence[1].locations == (third,)
    assert relationship.evidence[1].to_dict()["extensions"] == distinct_payload
    assert relationship.evidence[2].locations == ()
    assert relationship.evidence[2].extensions is None
