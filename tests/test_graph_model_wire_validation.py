"""Behavioral tests for structural graph-document validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import (
    GraphLoadError,
    graph_digest,
    load_graph_bytes,
    load_graph_file,
    stamp_path,
)
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


_GENERATED_AT_MESSAGE = "generated_at must be a valid RFC 3339 UTC timestamp ending in 'Z'"

_REJECTED_GENERATED_AT = [
    "2026-01-01T00:00Z",
    "2026-01-01 00:00:00Z",
    "٢٠٢٦-01-01T00:00:00Z",
    "2026-99-99T99:99:99Z",
]


def _document_with_generated_at(value: str) -> dict[str, object]:
    document = _example_document()
    document["generated_at"] = value
    return document


@pytest.mark.parametrize("value", _REJECTED_GENERATED_AT)
def test_parser_rejects_every_non_utc_rfc3339_generated_at(value: str) -> None:
    """One message for the whole rule, whatever part of the spelling is wrong."""
    with pytest.raises(ValueError, match=_GENERATED_AT_MESSAGE):
        GraphDocument.from_dict(_document_with_generated_at(value))


@pytest.mark.parametrize(
    "value", [*_REJECTED_GENERATED_AT, "2026-01-01T00:00:00Z\n", "2026-02-30T00:00:00Z"]
)
def test_bad_generated_at_fails_the_matching_sidecar_and_validate_loads(
    value: str, tmp_path: Path
) -> None:
    """The trusted path has no schema second opinion, so the model must reject.

    The sidecar matches the exact bytes, which authorizes the fast path that
    skips the JSON-schema pass; ``validate=True`` runs it. Both must refuse the
    file, matching the model's message on the trusted path.
    """
    content = json.dumps(_document_with_generated_at(value)).encode()
    graph_path = tmp_path / "minotaur-graph.json"
    graph_path.write_bytes(content)
    stamp_path(graph_path).write_text(graph_digest(content), encoding="utf-8")

    with pytest.raises(GraphLoadError, match=_GENERATED_AT_MESSAGE):
        load_graph_file(graph_path)

    with pytest.raises(GraphLoadError, match="generated_at"):
        load_graph_file(graph_path, validate=True)


def test_valid_generated_at_still_loads_on_the_trusted_path(tmp_path: Path) -> None:
    content = json.dumps(_document_with_generated_at("2026-08-16T20:00:00.123Z")).encode()
    graph_path = tmp_path / "minotaur-graph.json"
    graph_path.write_bytes(content)
    stamp_path(graph_path).write_text(graph_digest(content), encoding="utf-8")

    loaded = load_graph_file(graph_path)
    assert loaded.validated is False
    assert loaded.document.generated_at == "2026-08-16T20:00:00.123Z"
    assert load_graph_file(graph_path, validate=True).document.generated_at == (
        "2026-08-16T20:00:00.123Z"
    )


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
