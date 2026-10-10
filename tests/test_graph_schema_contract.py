"""Contract tests for checked-in v1 graph schema fixtures."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import jsonschema
import pytest

from minotaur import cli
from minotaur.graph_model.document import _RFC3339_UTC_RE, GraphDocument
from minotaur.graph_model.loading import (
    GraphLoadError,
    _validate_wire_shape,
    graph_digest,
    load_graph_blob,
    load_graph_bytes,
    load_graph_file,
    schema,
    stamp_path,
)
from minotaur.graph_model.validation import IssueCode, validate_document

ROOT = Path(__file__).parents[1]
INVALID_FIXTURES = ROOT / "tests/fixtures/minotaur-graph-v1/invalid"


def _validator() -> jsonschema.Draft202012Validator:
    """The published schema read exactly as the loader reads it.

    ``format: date-time`` is an annotation only: the generated_at rule lives
    in the schema ``pattern``, which is the model's own pattern anchored with
    ``^``…``$``. No ``format_checker`` is supplied, so the validator's verdict
    is the schema's alone.
    """
    return jsonschema.Draft202012Validator(schema())


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
# generated_at — one rule shared by the schema and the model
# ---------------------------------------------------------------------------

_GENERATED_AT_MESSAGE = "generated_at must be a valid RFC 3339 UTC timestamp ending in 'Z'"

_REJECTED_GENERATED_AT = [
    "2026-01-01T00:00Z",  # no seconds
    "2026-01-01 00:00:00Z",  # space instead of "T"
    "٢٠٢٦-01-01T00:00:00Z",  # Arabic-Indic digits ("\d" would accept them)
    "2026-99-99T99:99:99Z",  # every field out of range
]

_ACCEPTED_GENERATED_AT = [
    "2026-01-01T00:00:00Z",
    "2026-08-16T20:00:00.123Z",
]


def _wire_with_generated_at(value: str) -> dict[str, object]:
    document = _small_workflow()
    document["generated_at"] = value
    return document


def test_generated_at_schema_pattern_is_the_anchored_model_pattern() -> None:
    """The schema and the model cannot drift: one pattern, one anchor rule.

    Fails if either the schema ``pattern`` or ``_RFC3339_UTC_RE`` changes
    without the other.
    """
    properties = schema()["properties"]
    assert isinstance(properties, dict)
    generated_at = properties["generated_at"]
    assert isinstance(generated_at, dict)
    assert generated_at["pattern"] == "^" + _RFC3339_UTC_RE.pattern + "$"
    assert generated_at["format"] == "date-time"


def test_generated_at_schema_pattern_rejects_trailing_newline_under_ecma_anchoring() -> None:
    """``$`` is read with ECMA semantics (as ``\\Z``), as the path rule is.

    ``jsonschema`` evaluates ``pattern`` with ``re.search``, where Python's
    ``$`` also matches before a final newline; the model's ``re.fullmatch``
    does not. Read the way the ecosystem reads it (``$`` as ``\\Z``), the
    schema pattern agrees with the model and rejects the trailing newline.
    """
    generated_at = schema()["properties"]["generated_at"]
    assert isinstance(generated_at, dict)
    pattern = generated_at["pattern"]
    assert isinstance(pattern, str)
    assert pattern.endswith("$")
    ecma_pattern = pattern[:-1] + r"\Z"
    assert re.search(ecma_pattern, "2026-01-01T00:00:00Z\n") is None
    assert re.search(ecma_pattern, "2026-01-01T00:00:00Z") is not None


@pytest.mark.parametrize("value", _REJECTED_GENERATED_AT)
def test_generated_at_rejection_agrees_across_schema_model_and_load_paths(
    value: str, tmp_path: Path
) -> None:
    document = _wire_with_generated_at(value)

    schema_errors = list(_validator().iter_errors(document))
    assert [error.json_path for error in schema_errors] == ["$.generated_at"]

    with pytest.raises(ValueError, match=_GENERATED_AT_MESSAGE):
        GraphDocument.from_dict(document)

    graph_path = tmp_path / "minotaur-graph.json"
    graph_path.write_text(json.dumps(document), encoding="utf-8")
    # The sidecar matches the exact bytes, so the schema pass is skipped and
    # the model owns the verdict on the trusted path.
    stamp_path(graph_path).write_text(graph_digest(graph_path.read_bytes()), encoding="utf-8")
    with pytest.raises(GraphLoadError, match=_GENERATED_AT_MESSAGE):
        load_graph_file(graph_path)
    with pytest.raises(GraphLoadError, match="generated_at"):
        load_graph_file(graph_path, validate=True)


@pytest.mark.parametrize("value", _REJECTED_GENERATED_AT)
def test_load_graph_bytes_rejects_generated_at_and_names_the_field(value: str) -> None:
    """The bounded schema error names ``$.generated_at`` rather than dumping."""
    with pytest.raises(GraphLoadError, match=r"\$\.generated_at"):
        load_graph_bytes(json.dumps(_wire_with_generated_at(value)).encode())


def test_trailing_newline_generated_at_is_rejected_on_every_path(tmp_path: Path) -> None:
    """Python's ``$`` accepts a final newline; the model and ECMA ``$`` do not.

    E.g. ``"2026-01-01T00:00:00Z\\n"`` passes ``Draft202012Validator`` (which
    uses ``re.search``), so the model's ``re.fullmatch`` is what rejects it —
    on the matching-sidecar and ``validate=True`` paths alike.
    """
    document = _wire_with_generated_at("2026-01-01T00:00:00Z\n")
    _validator().validate(document)  # the plain validator accepts it

    with pytest.raises(ValueError, match=_GENERATED_AT_MESSAGE):
        GraphDocument.from_dict(document)

    graph_path = tmp_path / "minotaur-graph.json"
    graph_path.write_text(json.dumps(document), encoding="utf-8")
    stamp_path(graph_path).write_text(graph_digest(graph_path.read_bytes()), encoding="utf-8")
    with pytest.raises(GraphLoadError, match=_GENERATED_AT_MESSAGE):
        load_graph_file(graph_path)
    with pytest.raises(GraphLoadError, match=_GENERATED_AT_MESSAGE):
        load_graph_file(graph_path, validate=True)


@pytest.mark.parametrize("value", _ACCEPTED_GENERATED_AT)
def test_generated_at_accepted_by_schema_and_model(value: str) -> None:
    document = _wire_with_generated_at(value)
    _validator().validate(document)
    assert GraphDocument.from_dict(document).generated_at == value


def test_calendar_invalid_generated_at_is_the_model_s_alone() -> None:
    """``2026-02-30`` is a well-formed spelling but not a real day."""
    document = _wire_with_generated_at("2026-02-30T00:00:00Z")

    _validator().validate(document)  # the schema cannot express day validity
    _validate_wire_shape(document)  # shape pass agrees

    with pytest.raises(ValueError, match=_GENERATED_AT_MESSAGE):
        GraphDocument.from_dict(document)


@pytest.mark.parametrize(
    ("value", "schema_accepts"),
    [
        ("2026-01-01T00:00:00Z", True),
        ("2026-08-16T20:00:00.123Z", True),
        # Fails if a date-time format checker is reintroduced at the boundary.
        ("2026-02-30T00:00:00Z", True),
        ("2026-01-01T00:00Z", False),
        ("2026-01-01 00:00:00Z", False),
        # Fails if the schema pattern (or the model regex) falls back to "\d".
        ("٢٠٢٦-01-01T00:00:00Z", False),
        ("2026-99-99T99:99:99Z", False),
    ],
)
def test_validate_wire_shape_generated_at_verdicts(value: str, schema_accepts: bool) -> None:
    document = _wire_with_generated_at(value)
    if schema_accepts:
        _validate_wire_shape(document)
    else:
        with pytest.raises(jsonschema.ValidationError):
            _validate_wire_shape(document)


def test_loading_no_longer_owns_a_second_generated_at_rule() -> None:
    import minotaur.graph_model.loading as loading

    assert not hasattr(loading, "_FORMAT_CHECKER")
    assert not hasattr(loading, "_is_utc_rfc3339")


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

    The plain ``Draft202012Validator(schema())`` is the same validator the
    loader boundary uses, so the contract test and the load boundary cannot
    disagree about ``generated_at``: its rule is the schema ``pattern``
    (``format: date-time`` is an annotation), not a separate format checker.
    """
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(_MINIMAL_PYTHON_SOURCE, encoding="utf-8")

    graph_path = tmp_path / "minotaur-graph.json"
    _analyze_workspace(tmp_path, graph_path, tmp_path / "src")

    raw = json.loads(graph_path.read_text(encoding="utf-8"))

    # Schema validation with the same plain validator the loader uses
    validator = jsonschema.Draft202012Validator(schema())
    validator.validate(raw)

    # Sanity: the graph is non-trivial — at least one node was emitted
    assert len(raw["nodes"]) > 0, "analyze produced an empty graph"


def _wire_with_extensions(extensions: dict[str, dict[str, object]]) -> dict[str, object]:
    document = _small_workflow()
    nodes = _nodes(document)
    nodes[0]["extensions"] = extensions
    return document


@pytest.mark.parametrize(
    ("extensions", "json_path"),
    [
        ({"x": {"f": 1.5}}, "$.nodes[0].extensions.x.f"),
        ({"\U0001f600": {"f": 1}}, "$.nodes[0].extensions"),
        ({"x": {"\U0001d11e": 1}}, "$.nodes[0].extensions.x"),
    ],
)
def test_schema_rejects_non_integer_or_non_bmp_extension_wire_values(
    extensions: dict[str, dict[str, object]], json_path: str
) -> None:
    """``D-01``: the load error names the JSON path, not the instance dump."""
    with pytest.raises(GraphLoadError) as error:
        load_graph_bytes(json.dumps(_wire_with_extensions(extensions)).encode())
    message = str(error.value)
    assert message.startswith(f"{json_path}: ")
    # ``str(ValidationError)`` appended the schema and the whole instance; the
    # bounded message must not.
    assert "On instance" not in message


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
    assert len(fixtures) == 10
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


def test_format_document_pins_javascript_digest_and_shared_selection_metadata() -> None:
    """The graph-format reference documents both registered producer namespaces."""
    format_document = (ROOT / "docs/formats/minotaur-graph-v1.md").read_text(encoding="utf-8")
    javascript_start = format_document.index(
        "The JavaScript analyzer emits a separate producer extension"
    )
    javascript_end = format_document.index(
        "\nExtension values use a recursive grammar:", javascript_start
    )
    javascript = format_document[javascript_start:javascript_end]
    assert 'extensions["minotaur-javascript"]["content_sha256"]' in javascript
    assert 'extensions["minotaur"]["selection"]' in javascript
    assert "exact source bytes" in javascript
    assert "imports_resolved" not in javascript


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


# ---------------------------------------------------------------------------
# R-06 / AC-05: an explicit JSON null is rejected on the trusted path too
# ---------------------------------------------------------------------------
#
# The published schema forbids ``null`` in every model field, but the trusted
# (sidecar-authorized) load skips the schema pass.  The model layer's shared
# ``reject_unknown_fields`` owns the rule for both paths, so this enumeration
# holds every schema property name in every non-extension object of every
# example graph to the same verdict on both natural load paths.  ``null``
# inside an extension value stays legal, because the schema's
# ``extensionValue`` admits ``null`` and the trusted path must agree.

_GRAPH_EXAMPLES = sorted((ROOT / "examples/synthetic-graphs").glob("*.json"))

# A valid source-control envelope: at least one of commit/branch is required,
# and the commit must be a full lowercase hex identifier.
_SOURCE_CONTROL_VARIANT: dict[str, object] = {
    "system": "git",
    "commit": "0" * 40,
    "branch": "main",
}


def _schema_property_names() -> tuple[str, ...]:
    """Every property name declared anywhere in the published schema.

    Collected from every ``properties`` map (including the ``if``/``then``
    branches), never from a hand-maintained list, so a field added to the
    schema is probed here automatically.
    """
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema())
    return tuple(sorted(names))


_SCHEMA_PROPERTY_NAMES = _schema_property_names()


def _non_extension_objects(
    value: object, path: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], dict[str, object]]]:
    """Yield ``(json_path, object)`` for every object outside an extension value.

    An ``extensions`` subtree is skipped whole: the schema admits ``null``
    inside an extension value, so those objects are deliberately not probed.
    """
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            if key == "extensions":
                continue
            yield from _non_extension_objects(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _non_extension_objects(item, (*path, index))


def _graph_variants() -> list[tuple[str, str]]:
    """Each example graph as ``(label, json_text)``, original and +source_control."""
    variants: list[tuple[str, str]] = []
    for example in _GRAPH_EXAMPLES:
        base_text = example.read_text(encoding="utf-8")
        variants.append((example.name, base_text))
        with_source_control = json.loads(base_text)
        with_source_control["source_control"] = dict(_SOURCE_CONTROL_VARIANT)
        variants.append((example.name + "+source_control", json.dumps(with_source_control)))
    return variants


def _null_cells() -> list[tuple[str, str, tuple[str | int, ...], str]]:
    """``(label, graph_json, object_path, property_name)`` for every probe."""
    cells: list[tuple[str, str, tuple[str | int, ...], str]] = []
    for label, text in _graph_variants():
        for object_path, _ in _non_extension_objects(json.loads(text)):
            for name in _SCHEMA_PROPERTY_NAMES:
                cells.append((label, text, object_path, name))
    return cells


_NULL_CELLS = _null_cells()


def _object_at(document: object, path: tuple[str | int, ...]) -> dict[str, object]:
    target = document
    for segment in path:
        assert isinstance(target, (dict, list))
        target = target[segment]  # type: ignore[index]
    assert isinstance(target, dict)
    return target


def test_null_enumeration_covers_the_trusted_only_fields() -> None:
    """The probe must include the fields the baseline trusted path accepted.

    ``F-14`` found these four accepted only on the trusted path when set to
    ``null``; covering them is what makes the enumeration fail without the
    model-layer check.
    """
    cells = {(label, object_path, name) for label, _, object_path, name in _NULL_CELLS}
    assert ("provenance-demo.json", ("nodes", 0), "language") in cells
    assert ("small-workflow.json", ("nodes", 0), "language") in cells
    assert ("small-workflow.json", ("relationships", 0, "evidence", 0), "producer") in cells
    assert (
        "small-workflow.json",
        ("relationships", 0, "evidence", 0),
        "extensions",
    ) in cells
    assert (
        "provenance-demo.json",
        ("relationships", 0, "evidence", 0, "producer"),
        "version",
    ) in cells


def test_null_in_every_schema_property_fails_both_load_paths() -> None:
    """Every non-extension object rejects a ``null`` model field on both paths.

    The matching sidecar is the natural trusted trigger, so the assertion
    fails without the model-layer check; ``validate=True`` exercises the
    schema-first path.
    """
    for label, text, object_path, name in _NULL_CELLS:
        document = json.loads(text)
        cell = (label, object_path, name)
        _object_at(document, object_path)[name] = None

        assert list(_validator().iter_errors(document)), cell

        content = json.dumps(document).encode()
        sidecar = graph_digest(content).encode()

        with pytest.raises(GraphLoadError) as trusted:
            load_graph_blob(content, sidecar)
        assert f"must not set {name!r} to null" in str(trusted.value), cell

        with pytest.raises(GraphLoadError):
            load_graph_blob(content, validate=True)


@pytest.mark.parametrize("label", [label for label, _ in _graph_variants()])
def test_unmodified_examples_load_on_both_paths(label: str) -> None:
    text = dict(_graph_variants())[label]
    content = text.encode()
    sidecar = graph_digest(content).encode()
    load_graph_blob(content, sidecar)
    load_graph_blob(content, validate=True)


def test_null_language_fails_the_stamped_load_graph_file_path(tmp_path: Path) -> None:
    """At least one stamped ``load_graph_file`` cell carries the null rule."""
    document = _small_workflow()
    _nodes(document)[0]["language"] = None
    graph_path = tmp_path / "minotaur-graph.json"
    graph_path.write_text(json.dumps(document), encoding="utf-8")
    stamp_path(graph_path).write_text(graph_digest(graph_path.read_bytes()), encoding="utf-8")

    with pytest.raises(GraphLoadError, match="must not set 'language' to null"):
        load_graph_file(graph_path)
    with pytest.raises(GraphLoadError):
        load_graph_file(graph_path, validate=True)


def test_null_inside_extension_values_stays_legal_on_both_paths() -> None:
    """An extension value ``{"ns": {"v": null}}`` must not be over-rejected."""
    document = _wire_with_extensions({"ns": {"v": None}})
    _validator().validate(document)

    content = json.dumps(document).encode()
    sidecar = graph_digest(content).encode()
    load_graph_blob(content, sidecar)
    load_graph_blob(content, validate=True)
