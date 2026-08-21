"""Behavioral coverage for the implemented v1 graph-model modules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur.graph_model.document import GraphDocument, SourceControl
from minotaur.graph_model.evidence import Evidence, Producer, Rule
from minotaur.graph_model.identity import NodeIdentity, compute_node_id, verify_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    Provenance,
    RelationshipKind,
    SymbolKind,
    resolve_relationship_kind,
    resolve_symbol_kind,
)
from minotaur.graph_model.relationship import Relationship

EXAMPLES = Path(__file__).parents[1] / "examples/synthetic-graphs"
PYTHON_WORKFLOW = Path(__file__).parents[1] / "examples/python-workflow"


def test_synthetic_documents_round_trip_and_verify_every_node_id() -> None:
    for path in sorted(EXAMPLES.glob("*.json")):
        source = json.loads(path.read_text(encoding="utf-8"))
        document = GraphDocument.from_dict(source)

        assert document.to_dict() == source
        assert all(
            verify_node_id(
                node.id,
                node.identity,
                node_class=node.node_class.value,
                symbol_kind=node.symbol_kind,
                path=node.path,
                location=node.location,
                reference_text=node.reference_text,
            )
            for node in document.nodes
        )


def test_location_sort_key_orders_call_sites_and_rejects_dot_segments() -> None:
    later = Location("src/a.py", Range(Position(3, 0), Position(3, 1)))
    earlier = Location("src/a.py", Range(Position(2, 5), Position(2, 8)))

    assert sorted((later, earlier), key=lambda location: location.sort_key) == [earlier, later]
    with pytest.raises(ValueError, match="repository-relative"):
        Location("src/../secret.py", Range(Position(0, 0), Position(0, 1)))


def test_controlled_vocabularies_allow_core_and_namespaced_extensions_only() -> None:
    assert resolve_symbol_kind("function") == SymbolKind.FUNCTION
    assert resolve_symbol_kind("example.org:template") == "example.org:template"
    assert resolve_relationship_kind("calls") == RelationshipKind.CALLS

    with pytest.raises(ValueError, match="namespaced extension"):
        resolve_relationship_kind("depends-on")


def test_curated_evidence_requires_rule_and_preserves_attribution() -> None:
    rule = Rule("framework-route", "1")
    evidence = Evidence(
        provenance=Provenance.CURATED_RULE,
        producer=Producer("minotaur-policy", "1.0"),
        rule=rule,
    )

    assert Evidence.from_dict(evidence.to_dict()) == evidence
    with pytest.raises(ValueError, match="requires a 'rule'"):
        Evidence(provenance=Provenance.CURATED_RULE)


@pytest.mark.parametrize(
    ("basis", "kwargs"),
    [
        (
            IdentityBasis.UPSTREAM_IDENTIFIER,
            {"upstream_identifier": "external:entity-17"},
        ),
        (IdentityBasis.RESOURCE_KEY, {"resource_key": "postgres://orders"}),
    ],
)
def test_non_source_identity_bases_produce_repeatable_node_ids(
    basis: IdentityBasis, kwargs: dict[str, str]
) -> None:
    identity = NodeIdentity(basis=basis, namespace="example", **kwargs)
    node_id = compute_node_id(identity, node_class=NodeClass.RESOURCE.value)

    assert verify_node_id(node_id, identity, node_class=NodeClass.RESOURCE.value)


def test_node_shape_requirements_keep_file_and_unresolved_facts_distinct() -> None:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, "example")
    file_id = compute_node_id(identity, node_class=NodeClass.FILE.value, path="src/main.py")
    file_node = Node(
        id=file_id,
        identity=identity,
        node_class=NodeClass.FILE,
        label="main.py",
        path="src/main.py",
    )

    assert file_node.path == "src/main.py"
    with pytest.raises(ValueError, match="reference_text"):
        Node(
            id=file_id,
            identity=identity,
            node_class=NodeClass.UNRESOLVED_REFERENCE,
            label="missing",
        )


def test_document_keeps_snapshot_context_without_affecting_node_lookup() -> None:
    source = json.loads((EXAMPLES / "small-workflow.json").read_text(encoding="utf-8"))
    source["generated_at"] = "2026-08-16T20:00:00.123Z"
    source["source_control"] = {
        "system": "git",
        "commit": "a" * 40,
        "branch": "main",
    }
    document = GraphDocument.from_dict(source)

    assert document.source_control == SourceControl("git", "a" * 40, "main")
    assert document.node_by_id(document.nodes[0].id) == document.nodes[0]
    assert document.coordinate_encoding == CoordinateEncoding.UTF_8


def test_relationship_tuple_key_keeps_distinct_evidence_on_one_edge() -> None:
    source = "node:sha256:" + "a" * 64
    target = "node:sha256:" + "b" * 64
    relationship = Relationship(
        source=source,
        target=target,
        kind="calls",
        evidence=(Evidence(Provenance.STATIC_ANALYSIS), Evidence(Provenance.IMPORTED_GRAPH)),
    )

    assert relationship.tuple_key == (source, target, "calls")
    assert len(relationship.to_dict()["evidence"]) == 2


def test_from_dict_round_trip_equality_with_memo(
    # AC-14: round-trip equality over all example JSON files.
) -> None:
    """GraphDocument.from_dict produces identical round-trip dicts and equal
    documents for every example graph, proving the memo is invisible."""
    example_paths = sorted(EXAMPLES.glob("*.json")) + [
        PYTHON_WORKFLOW / "minotaur-graph.json",
    ]
    assert len(example_paths) >= 4, "expected at least 4 example files"
    for path in example_paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        doc1 = GraphDocument.from_dict(raw)
        doc2 = GraphDocument.from_dict(raw)
        assert doc1.to_dict() == raw, f"round-trip failed for {path.name}"
        assert doc1 == doc2, f"double-parse equality failed for {path.name}"


def _make_guard_test_document(
    valid_location: dict[str, object],
    malformed_location: dict[str, object],
) -> dict[str, object]:
    """Build a minimal document where the FIRST relationship evidence has
    the valid location (populating the memo) and the SECOND relationship
    evidence has the malformed location."""
    source_id = "node:sha256:" + "a" * 64
    target_id = "node:sha256:" + "b" * 64
    return {
        "format": "minotaur-graph",
        "format_version": "0.1.0",
        "coordinate_encoding": "utf-8",
        "nodes": [
            {
                "id": source_id,
                "identity": {"basis": "file-path", "namespace": "test"},
                "node_class": "file",
                "label": "a.py",
                "path": "a.py",
            },
            {
                "id": target_id,
                "identity": {"basis": "file-path", "namespace": "test"},
                "node_class": "file",
                "label": "b.py",
                "path": "b.py",
            },
        ],
        "relationships": [
            {
                "source": source_id,
                "target": target_id,
                "kind": "calls",
                "evidence": [
                    {
                        "provenance": "static-analysis",
                        "locations": [valid_location],
                    },
                ],
            },
            {
                "source": source_id,
                "target": target_id,
                "kind": "imports",
                "evidence": [
                    {
                        "provenance": "static-analysis",
                        "locations": [malformed_location],
                    },
                ],
            },
        ],
    }


_VALID_LOCATION: dict[str, object] = {
    "path": "a.py",
    "range": {
        "start": {"line": 1, "character": 0},
        "end": {"line": 1, "character": 5},
    },
}


def test_memo_guard_rejects_bool_position_value() -> None:
    """AC-17(a): {"line": true} where the memo has {"line": 1} must still
    raise ValueError, not return the cached Location."""
    malformed = {
        "path": "a.py",
        "range": {
            "start": {"line": True, "character": 0},
            "end": {"line": 1, "character": 5},
        },
    }
    doc_data = _make_guard_test_document(_VALID_LOCATION, malformed)
    with pytest.raises(ValueError, match="must be an integer"):
        GraphDocument.from_dict(doc_data)


def test_memo_guard_rejects_extra_field_in_location() -> None:
    """AC-17(b): a location with an extra field whose path/range match a
    memoized location must still raise ValueError from reject_unknown_fields."""
    malformed = {
        "path": "a.py",
        "range": {
            "start": {"line": 1, "character": 0},
            "end": {"line": 1, "character": 5},
        },
        "x": 1,
    }
    doc_data = _make_guard_test_document(_VALID_LOCATION, malformed)
    with pytest.raises(ValueError, match="unsupported field"):
        GraphDocument.from_dict(doc_data)


def test_memo_guard_rejects_non_dict_range() -> None:
    """AC-17(c): range is not a dict -- must raise today's ValueError
    ('location requires a range object'), not AttributeError."""
    malformed = {"path": "a.py", "range": 4}
    doc_data = _make_guard_test_document(_VALID_LOCATION, malformed)
    with pytest.raises(ValueError, match="location requires a 'range' object"):
        GraphDocument.from_dict(doc_data)


def test_memo_guard_rejects_non_dict_start() -> None:
    """AC-17(c): range.start is not a dict -- must raise today's ValueError
    ('range requires a start object'), not AttributeError."""
    malformed = {
        "path": "a.py",
        "range": {
            "start": "not-a-dict",
            "end": {"line": 1, "character": 5},
        },
    }
    doc_data = _make_guard_test_document(_VALID_LOCATION, malformed)
    with pytest.raises(ValueError, match="range requires a 'start' object"):
        GraphDocument.from_dict(doc_data)


# --- Adversarial memo guard tests (reviewer-added) ---
# These exercise guard paths not covered by AC-17's named scenarios,
# specifically to confirm that no combination of valid-looking but
# malformed input can bypass a guard and be served from the memo.


def test_memo_guard_rejects_bool_in_character_position() -> None:
    """Adversarial: {"character": True} must be rejected by type(sc) is int
    guard, even when the line value matches a memoized location."""
    malformed = {
        "path": "a.py",
        "range": {
            "start": {"line": 1, "character": True},
            "end": {"line": 1, "character": 5},
        },
    }
    doc_data = _make_guard_test_document(_VALID_LOCATION, malformed)
    with pytest.raises(ValueError, match="must be an integer"):
        GraphDocument.from_dict(doc_data)


def test_memo_guard_rejects_non_dict_end() -> None:
    """Adversarial: range.end is not a dict -- must raise ValueError
    ('range requires an end object'), not AttributeError. Complements the
    existing non-dict-start test to cover the isinstance(end_raw, dict) guard."""
    malformed = {
        "path": "a.py",
        "range": {
            "start": {"line": 1, "character": 0},
            "end": "not-a-dict",
        },
    }
    doc_data = _make_guard_test_document(_VALID_LOCATION, malformed)
    with pytest.raises(ValueError, match="range requires an 'end' object"):
        GraphDocument.from_dict(doc_data)


def test_memo_guard_rejects_extra_field_in_position() -> None:
    """Adversarial: a position dict with an extra field {"line", "character", "z"}
    whose line/character values match a memoized location must be rejected by the
    key-set guard (start_raw.keys() == _POSITION_FIELDS)."""
    malformed = {
        "path": "a.py",
        "range": {
            "start": {"line": 1, "character": 0, "z": 99},
            "end": {"line": 1, "character": 5},
        },
    }
    doc_data = _make_guard_test_document(_VALID_LOCATION, malformed)
    with pytest.raises(ValueError, match="unsupported field"):
        GraphDocument.from_dict(doc_data)


def test_memo_not_poisoned_by_failed_construction() -> None:
    """Adversarial: a location with a negative line passes all memo guards
    (type(-1) is int is True) but fails at Position.__post_init__. The
    second relationship carries the same location shape with line: -1,
    and a third relationship carries the same shape with line: 0.
    This proves: (1) failed construction does not poison the memo, and
    (2) a subsequent valid parse still succeeds."""
    source_id = "node:sha256:" + "a" * 64
    target_id = "node:sha256:" + "b" * 64

    valid_loc: dict[str, object] = {
        "path": "b.py",
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        },
    }
    bad_loc: dict[str, object] = {
        "path": "a.py",
        "range": {
            "start": {"line": -1, "character": 0},
            "end": {"line": 1, "character": 5},
        },
    }
    doc_data = {
        "format": "minotaur-graph",
        "format_version": "0.1.0",
        "coordinate_encoding": "utf-8",
        "nodes": [
            {
                "id": source_id,
                "identity": {"basis": "file-path", "namespace": "test"},
                "node_class": "file",
                "label": "a.py",
                "path": "a.py",
            },
            {
                "id": target_id,
                "identity": {"basis": "file-path", "namespace": "test"},
                "node_class": "file",
                "label": "b.py",
                "path": "b.py",
            },
        ],
        "relationships": [
            {
                "source": source_id,
                "target": target_id,
                "kind": "calls",
                "evidence": [
                    {"provenance": "static-analysis", "locations": [valid_loc]},
                ],
            },
            {
                "source": source_id,
                "target": target_id,
                "kind": "imports",
                "evidence": [
                    {"provenance": "static-analysis", "locations": [bad_loc]},
                ],
            },
        ],
    }
    with pytest.raises(ValueError, match="non-negative"):
        GraphDocument.from_dict(doc_data)


def test_extensions_are_deeply_immutable_but_serialize_as_json() -> None:
    extensions = {"example": {"nested": {"values": ["one"]}}}
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        extensions=extensions,
    )

    extensions["example"]["nested"]["values"].append("mutated")

    assert document.to_dict()["extensions"] == {"example": {"nested": {"values": ["one"]}}}
    assert document.extensions is not None
    with pytest.raises(TypeError):
        document.extensions["other"] = {}
