"""Contract tests for checked-in v1 graph schema fixtures."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.validation import IssueCode, validate_document

ROOT = Path(__file__).parents[1]
INVALID_FIXTURES = ROOT / "tests/fixtures/minotaur-graph-v1/invalid"
FORMAT_CHECKER = jsonschema.FormatChecker()


@FORMAT_CHECKER.checks("date-time")
def _is_valid_rfc3339_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    return True


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((ROOT / "schemas/minotaur-graph/v1.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "invalid-generated-at.json",
        "missing-curated-rule.json",
        "unsafe-path.json",
        "wrong-position-type.json",
    ],
)
def test_structurally_invalid_fixtures_fail_schema_and_model_loading(fixture_name: str) -> None:
    data = json.loads((INVALID_FIXTURES / fixture_name).read_text(encoding="utf-8"))

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(data)
    with pytest.raises(ValueError):
        GraphDocument.from_dict(data)


def test_dangling_endpoint_passes_schema_and_model_but_fails_semantic_validation() -> None:
    data = json.loads((INVALID_FIXTURES / "dangling-relationship.json").read_text(encoding="utf-8"))

    _validator().validate(data)
    document = GraphDocument.from_dict(data)
    assert document.to_dict() == data

    report = validate_document(document)
    assert [(issue.code, issue.path) for issue in report] == [
        (IssueCode.RELATIONSHIP_ENDPOINT_MISSING, ("relationships", 0, "source")),
        (IssueCode.RELATIONSHIP_ENDPOINT_MISSING, ("relationships", 0, "target")),
    ]


def test_valid_examples_pass_semantic_validation() -> None:
    for example in sorted((ROOT / "examples/synthetic-graphs").glob("*.json")):
        data = json.loads(example.read_text(encoding="utf-8"))
        report = validate_document(GraphDocument.from_dict(data))
        assert report.is_valid, (example.name, [issue.json_pointer for issue in report])


def _small_workflow() -> dict[str, object]:
    return json.loads(
        (ROOT / "examples/synthetic-graphs/small-workflow.json").read_text(encoding="utf-8")
    )


def _nodes(document: dict[str, object]) -> list[dict[str, object]]:
    nodes = document["nodes"]
    assert isinstance(nodes, list)
    return nodes


def _assert_rejected_by_schema_and_model(document: dict[str, object], model_match: str) -> None:
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(document)
    with pytest.raises(ValueError, match=model_match):
        GraphDocument.from_dict(document)


_ALL_BASES = [
    "source-location",
    "file-path",
    "upstream-identifier",
    "unresolved-reference",
    "resource-key",
]
_ALL_CLASSES = ["symbol", "file", "resource", "unresolved-reference"]
_CONDITIONAL_FIELDS = {"upstream_identifier", "originating_node", "resource_key"}
# Mirrors the accepted 2026-08-17 decision; the model's private tables are the
# implementation, this table is the contract the tests hold both layers to.
_BASIS_USES = {
    "source-location": set(),
    "file-path": set(),
    "upstream-identifier": {"upstream_identifier"},
    "unresolved-reference": {"originating_node"},
    "resource-key": {"resource_key"},
}
_PERMITTED_BASES = {
    "symbol": {"source-location", "upstream-identifier"},
    "file": {"file-path"},
    "resource": {"resource-key", "upstream-identifier", "source-location"},
    "unresolved-reference": {"unresolved-reference"},
}
_FIELD_VALUES = {
    "upstream_identifier": "ext:1",
    "originating_node": "node:sha256:" + "a" * 64,
    "resource_key": "postgres://orders",
}


def _shaped_node(node_class: str, basis: str) -> dict[str, object]:
    """A node of ``node_class`` with a well-formed identity of ``basis``.

    The digest is deliberately not recomputed: these tests exercise the
    structural layer, which never verifies digests.
    """
    location = _nodes(_small_workflow())[0]["location"]
    identity: dict[str, object] = {"basis": basis, "namespace": "example"}
    for field_name in _BASIS_USES[basis]:
        identity[field_name] = _FIELD_VALUES[field_name]
    node: dict[str, object] = {
        "id": "node:sha256:" + "c" * 64,
        "identity": identity,
        "node_class": node_class,
        "label": "shaped",
    }
    if node_class == "symbol":
        node["symbol_kind"] = "function"
    if node_class == "file":
        node["path"] = "src/checkout.py"
    if node_class == "unresolved-reference":
        node["reference_text"] = "missing"
    if basis == "source-location":
        node["location"] = location
    return node


def _document_with_node(node: dict[str, object]) -> dict[str, object]:
    document = _small_workflow()
    document["nodes"] = [node]
    document["relationships"] = []
    return document


def _accepted_by_schema_and_model(document: dict[str, object]) -> None:
    _validator().validate(document)
    GraphDocument.from_dict(document)


@pytest.mark.parametrize("basis", _ALL_BASES)
@pytest.mark.parametrize("extra_field", sorted(_CONDITIONAL_FIELDS))
def test_identity_basis_forbids_every_unused_conditional_field(
    basis: str, extra_field: str
) -> None:
    node_class = next(c for c in _ALL_CLASSES if basis in _PERMITTED_BASES[c])
    node = _shaped_node(node_class, basis)
    if extra_field in _BASIS_USES[basis]:
        _accepted_by_schema_and_model(_document_with_node(node))
        return
    identity = node["identity"]
    assert isinstance(identity, dict)
    identity[extra_field] = _FIELD_VALUES[extra_field]

    _assert_rejected_by_schema_and_model(
        _document_with_node(node), f"{basis} basis does not permit '{extra_field}'"
    )


@pytest.mark.parametrize("node_class", _ALL_CLASSES)
@pytest.mark.parametrize("basis", _ALL_BASES)
def test_node_class_and_identity_basis_coupling_agrees_across_layers(
    node_class: str, basis: str
) -> None:
    document = _document_with_node(_shaped_node(node_class, basis))
    if basis in _PERMITTED_BASES[node_class]:
        _accepted_by_schema_and_model(document)
    else:
        _assert_rejected_by_schema_and_model(document, "do not permit identity basis")


def test_originating_node_must_have_node_id_syntax_in_both_layers() -> None:
    node = _shaped_node("unresolved-reference", "unresolved-reference")
    identity = node["identity"]
    assert isinstance(identity, dict)
    identity["originating_node"] = "not-a-node-id"

    _assert_rejected_by_schema_and_model(_document_with_node(node), "originating_node")


@pytest.mark.parametrize("node_class", _ALL_CLASSES)
def test_symbol_kind_is_vocabulary_checked_on_every_node_class(node_class: str) -> None:
    basis = sorted(_PERMITTED_BASES[node_class])[0]
    node = _shaped_node(node_class, basis)
    node["symbol_kind"] = "NOT A KIND"

    _assert_rejected_by_schema_and_model(_document_with_node(node), "symbol_kind|symbol kind")


@pytest.mark.parametrize("node_class", _ALL_CLASSES)
def test_reference_text_must_be_non_empty_on_every_node_class(node_class: str) -> None:
    basis = sorted(_PERMITTED_BASES[node_class])[0]
    node = _shaped_node(node_class, basis)
    node["reference_text"] = ""

    _assert_rejected_by_schema_and_model(_document_with_node(node), "reference_text")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda node: node["identity"].__setitem__("namespace", "\ud800ns"),
        lambda node: node.__setitem__("reference_text", "\ud800.thing"),
        lambda node: node["location"].__setitem__("path", "src/\ud800.py"),
    ],
    ids=["identity-namespace", "reference_text", "location-path"],
)
def test_unpaired_surrogates_in_identity_inputs_are_rejected_at_construction(
    mutate: object,
) -> None:
    # A lone surrogate is valid JSON and passes the schema, but RFC 8785 JCS
    # has no canonical UTF-8 form for it, so the identity input could never
    # be hashed. The model rejects it structurally so that every loaded node
    # is reconstructible by the semantic validator.
    node = _shaped_node("symbol", "source-location")
    assert callable(mutate)
    mutate(node)
    document = _document_with_node(node)

    _validator().validate(document)  # schema cannot express this rule
    with pytest.raises(ValueError, match="unpaired surrogate"):
        GraphDocument.from_dict(document)


@pytest.mark.parametrize("node_class", _ALL_CLASSES)
def test_path_is_safety_checked_on_every_node_class(node_class: str) -> None:
    basis = sorted(_PERMITTED_BASES[node_class])[0]
    node = _shaped_node(node_class, basis)
    node["path"] = "../../etc/passwd"

    _assert_rejected_by_schema_and_model(_document_with_node(node), "safe repository-relative path")


def test_source_location_basis_requires_node_location() -> None:
    document = _small_workflow()
    node = _nodes(document)[0]
    node.pop("location")

    _assert_rejected_by_schema_and_model(document, "requires a node 'location'")


def test_unresolved_reference_node_may_carry_only_its_own_basis() -> None:
    document = json.loads(
        (ROOT / "examples/synthetic-graphs/unresolved-reference-demo.json").read_text(
            encoding="utf-8"
        )
    )
    unresolved = next(n for n in _nodes(document) if n["node_class"] == "unresolved-reference")
    unresolved["identity"] = {"basis": "resource-key", "namespace": "example", "resource_key": "k"}

    _assert_rejected_by_schema_and_model(document, "do not permit identity basis")
