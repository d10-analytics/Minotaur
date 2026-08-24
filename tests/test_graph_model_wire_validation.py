"""Behavioral tests for structural graph-document validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import GraphLoadError, load_graph_bytes
from minotaur.graph_model.location import Location, Position, Range

EXAMPLE_PATH = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"


def _example_document() -> dict[str, object]:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def test_location_rejects_backslash_traversal() -> None:
    with pytest.raises(ValueError, match="repository-relative"):
        Location(r"..\\secrets.txt", Range(Position(0, 0), Position(0, 1)))


def test_relationship_rejects_malformed_endpoint_id() -> None:
    document = _example_document()
    relationships = document["relationships"]
    assert isinstance(relationships, list)
    relationships[0]["source"] = "not-a-node"

    with pytest.raises(ValueError, match="relationship 'source' must be a valid node ID"):
        GraphDocument.from_dict(document)


def test_parser_rejects_undeclared_evidence_field() -> None:
    document = _example_document()
    relationships = document["relationships"]
    assert isinstance(relationships, list)
    relationships[0]["evidence"][0]["source_note"] = "must not be discarded"

    with pytest.raises(ValueError, match="evidence has unsupported field"):
        GraphDocument.from_dict(document)


def test_parser_rejects_invalid_rfc3339_timestamp() -> None:
    document = _example_document()
    document["generated_at"] = "2026-99-99T99:99:99Z"

    with pytest.raises(ValueError, match="valid RFC 3339 UTC timestamp"):
        GraphDocument.from_dict(document)


def test_trusted_load_still_rejects_float_extension_values() -> None:
    document = _example_document()
    nodes = document["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["extensions"] = {"x": {"nested": {"value": 4.0}}}

    with pytest.raises(GraphLoadError, match=r"/x/nested/value"):
        load_graph_bytes(json.dumps(document).encode(), _skip_schema=True)


def test_trusted_load_still_rejects_non_bmp_extension_keys() -> None:
    document = _example_document()
    nodes = document["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["extensions"] = {"x": {"nested": {"\U0001f600": 1}}}

    with pytest.raises(GraphLoadError, match=r"/x/nested/\U0001f600"):
        load_graph_bytes(json.dumps(document).encode(), _skip_schema=True)
