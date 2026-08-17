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
