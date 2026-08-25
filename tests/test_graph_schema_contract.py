"""Contract tests for checked-in v1 graph schema fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from minotaur import cli
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import _FORMAT_CHECKER, GraphLoadError, load_graph_bytes, schema
from minotaur.graph_model.validation import IssueCode, validate_document

ROOT = Path(__file__).parents[1]
INVALID_FIXTURES = ROOT / "tests/fixtures/minotaur-graph-v1/invalid"


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(schema(), format_checker=_FORMAT_CHECKER)


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


# ---------------------------------------------------------------------------
# AC-15 — Writer output satisfies the published v1 schema
# ---------------------------------------------------------------------------

_MINIMAL_PYTHON_SOURCE = """\
def greet(name: str) -> str:
    return f"Hello, {name}!"
"""


def _analyze_workspace(root: Path, graph_path: Path, *targets: Path) -> None:
    """Run ``analyze`` to produce a graph JSON at *graph_path*."""
    args = [
        "analyze",
        "--root",
        str(root),
        "--output",
        str(graph_path),
        "--force",
        *(str(t) for t in targets),
    ]
    status = cli.main(args)
    assert status == 0, f"analyze failed with exit {status}"


def test_fresh_analyze_output_satisfies_published_v1_schema(tmp_path: Path) -> None:
    """``analyze`` output must pass the published JSON schema.

    ``R-02`` removes the only runtime moment the writer's output met the
    schema (``D-09``).  This test is the explicit CI replacement, using the
    same ``_FORMAT_CHECKER`` that the loader boundary uses so the contract
    test and the load boundary cannot disagree about ``date-time``.
    """
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(_MINIMAL_PYTHON_SOURCE, encoding="utf-8")

    graph_path = tmp_path / "minotaur-graph.json"
    _analyze_workspace(tmp_path, graph_path, tmp_path / "src")

    raw = json.loads(graph_path.read_text(encoding="utf-8"))

    # Schema validation with the shared format checker
    validator = jsonschema.Draft202012Validator(schema(), format_checker=_FORMAT_CHECKER)
    validator.validate(raw)

    # Sanity: the graph is non-trivial — at least one node was emitted
    assert len(raw["nodes"]) > 0, "analyze produced an empty graph"


def _wire_with_extensions(extensions: dict[str, dict[str, object]]) -> dict[str, object]:
    document = _small_workflow()
    nodes = _nodes(document)
    nodes[0]["extensions"] = extensions
    return document


@pytest.mark.parametrize(
    ("extensions", "instance_path"),
    [
        ({"x": {"f": 1.5}}, "On instance['nodes'][0]['extensions']['x']['f']"),
        ({"\U0001f600": {"f": 1}}, "On instance['nodes'][0]['extensions']"),
        ({"x": {"\U0001d11e": 1}}, "On instance['nodes'][0]['extensions']['x']"),
    ],
)
def test_schema_rejects_non_integer_or_non_bmp_extension_wire_values(
    extensions: dict[str, dict[str, object]], instance_path: str
) -> None:
    with pytest.raises(GraphLoadError) as error:
        load_graph_bytes(json.dumps(_wire_with_extensions(extensions)).encode())
    assert instance_path in str(error.value)


def test_model_guard_owns_float_rejection_when_schema_accepts_json_integer() -> None:
    document = _wire_with_extensions({"x": {"f": 4.0}})
    _validator().validate(document)

    with pytest.raises(GraphLoadError, match=r"/x/f"):
        load_graph_bytes(json.dumps(document).encode())


def test_schema_verdicts_are_unchanged_for_all_graph_fixtures() -> None:
    from test_graph_model_serialization import _collect_graph_fixtures

    expected_invalid = {
        "tests/fixtures/minotaur-graph-v1/invalid/invalid-generated-at.json": {"$.generated_at"},
        "tests/fixtures/minotaur-graph-v1/invalid/missing-curated-rule.json": {
            "$.relationships[0].evidence[0]"
        },
        "tests/fixtures/minotaur-graph-v1/invalid/unsafe-path.json": {"$.nodes[0].path"},
        "tests/fixtures/minotaur-graph-v1/invalid/wrong-position-type.json": {
            "$.nodes[0].location.range.start.character"
        },
    }
    fixtures = _collect_graph_fixtures()
    assert len(fixtures) == 9
    for fixture_path in fixtures:
        relative = str(fixture_path.relative_to(ROOT))
        errors = sorted(
            error.json_path
            for error in _validator().iter_errors(json.loads(fixture_path.read_text()))
        )
        assert errors == sorted(expected_invalid.get(relative, set())), relative


def test_format_document_states_the_extension_integer_range() -> None:
    """The extension-value format text must match the decoder's integer range."""
    format_document = (ROOT / "docs/formats/minotaur-graph-v1.md").read_text(encoding="utf-8")
    grammar_start = format_document.index("Extension values use a recursive grammar:")
    grammar_end = format_document.index("\n\n", grammar_start)
    grammar = format_document[grammar_start:grammar_end]
    assert "extension object" in grammar
    assert "[-2^63, 2^64-1]" in grammar


def test_extension_schema_identity_and_property_name_pattern_are_pinned() -> None:
    loaded_schema = schema()
    assert loaded_schema["$id"] == "urn:minotaur:schemas:minotaur-graph:0.1.0"
    defs = loaded_schema["$defs"]
    assert isinstance(defs, dict)
    assert defs["extensions"] == {
        "type": "object",
        "propertyNames": {"minLength": 1, "pattern": "^[\\u0000-\\uFFFF]*$"},
        "additionalProperties": {"$ref": "#/$defs/extensionObject"},
    }
    assert defs["extensionObject"] == {
        "type": "object",
        "propertyNames": {"minLength": 1, "pattern": "^[\\u0000-\\uFFFF]*$"},
        "additionalProperties": {"$ref": "#/$defs/extensionValue"},
    }
    assert defs["extensionValue"] == {
        "anyOf": [
            {"type": "string"},
            {"type": "integer"},
            {"type": "boolean"},
            {"type": "null"},
            {"type": "array", "items": {"$ref": "#/$defs/extensionValue"}},
            {"$ref": "#/$defs/extensionObject"},
        ]
    }

    accepted = _small_workflow()
    accepted["extensions"] = {"a\n": {"value": 1}}
    _validator().validate(accepted)
    rejected = _small_workflow()
    rejected["extensions"] = {"\U0001d11e\n": {"value": 1}}
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(rejected)


# ---------------------------------------------------------------------------
# R-02 / M-2: the trusted path and the schema reach the same verdict on keys
# ---------------------------------------------------------------------------
#
# `extensionObject.propertyNames.minLength: 1` is a schema rule, and the
# trusted load skips the schema.  Without the matching model-layer rule an
# empty extension key would be admitted by exactly the path that has no second
# opinion, so the two layers are asserted to agree case by case.

_KEY_SHAPE_CASES: list[tuple[str, dict[str, dict[str, object]], bool]] = [
    # (id, extensions, schema-valid?)
    ("empty_key_nested", {"x": {"": 1}}, False),
    ("empty_key_deep", {"x": {"y": {"": 1}}}, False),
    ("empty_key_in_list", {"x": {"values": [{"": 1}]}}, False),
    ("empty_top_level_name", {"": {"f": 1}}, False),
    ("non_bmp_key_nested", {"x": {"\U0001d11e": 1}}, False),
    ("ordinary_key", {"x": {"a": 1}}, True),
    ("newline_key", {"x": {"a\n": 1}}, True),
    ("astral_string_value", {"x": {"note": "\U0001d11e"}}, True),
]


@pytest.mark.parametrize(
    ("extensions", "schema_valid"),
    [(case[1], case[2]) for case in _KEY_SHAPE_CASES],
    ids=[case[0] for case in _KEY_SHAPE_CASES],
)
def test_extension_key_verdicts_agree_on_trusted_and_full_validation_paths(
    extensions: dict[str, dict[str, object]], schema_valid: bool
) -> None:
    document = _wire_with_extensions(extensions)
    content = json.dumps(document).encode()

    schema_errors = list(_validator().iter_errors(document))
    assert bool(schema_errors) is not schema_valid, [e.message for e in schema_errors]

    for skip_schema in (False, True):
        if schema_valid:
            load_graph_bytes(content, _skip_schema=skip_schema)
        else:
            with pytest.raises(GraphLoadError):
                load_graph_bytes(content, _skip_schema=skip_schema)
