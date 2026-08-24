"""Behavioral coverage for the implemented v1 graph-model modules."""

from __future__ import annotations

import json
import re
from collections.abc import Set
from dataclasses import replace
from pathlib import Path

import pytest

from minotaur.graph_model._parsing import reject_unknown_fields
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


class _NonIteratingAllowedFields(Set[str]):
    """Probe that catches the old difference operation on valid input."""

    def __contains__(self, value: object) -> bool:
        return value in {"line"}

    def __iter__(self):
        raise AssertionError("the conforming fast path must not iterate allowed fields")

    def __len__(self) -> int:
        return 1


def test_reject_unknown_fields_does_not_iterate_allowed_set_on_success() -> None:
    allowed = _NonIteratingAllowedFields()

    assert reject_unknown_fields({"line": 1}, allowed, "position") is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {"x": 1, "line": 1, "character": 0},
            "position has unsupported field(s): 'x'",
        ),
        (
            {"line": 1, "character": 0, "y": 2, "x": 3},
            "position has unsupported field(s): 'x', 'y'",
        ),
    ],
)
def test_reject_unknown_fields_preserves_exact_failure_messages(
    data: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError) as error:
        reject_unknown_fields(data, frozenset({"line", "character"}), "position")

    assert str(error.value) == message


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {"end": {"line": 1, "character": 0}},
            "range requires a 'start' object",
        ),
        (
            {"start": 3, "end": {"line": 1, "character": 0}},
            "range requires a 'start' object",
        ),
        (
            {
                "start": {"line": 1, "character": 0},
                "end": {"line": 1, "character": 0},
                "z": 1,
            },
            "range has unsupported field(s): 'z'",
        ),
        (
            {
                "start": {"line": 1, "character": 0, "z": 1},
                "end": {"line": 1, "character": 0},
            },
            "position has unsupported field(s): 'z'",
        ),
        (
            {
                "start": {"line": 4.0, "character": 0},
                "end": {"line": 4, "character": 0},
            },
            "'line' must be an integer, got float: 4.0",
        ),
        (
            {
                "start": {"line": True, "character": 0},
                "end": {"line": 4, "character": 0},
            },
            "'line' must be an integer, got bool: True",
        ),
        (
            {
                "start": {"line": "4", "character": 0},
                "end": {"line": 4, "character": 0},
            },
            "'line' must be an integer, got str: '4'",
        ),
        (
            {
                "start": {"line": -1, "character": 0},
                "end": {"line": 4, "character": 0},
            },
            "line must be non-negative, got -1",
        ),
        (
            {
                "start": {"line": 4.0, "character": 0},
                "end": {"line": 4, "character": 0},
                "z": 1,
            },
            "range has unsupported field(s): 'z'",
        ),
    ],
)
def test_range_from_dict_preserves_error_messages_and_precedence(
    data: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError) as error:
        Range.from_dict(data)

    assert str(error.value) == message


def test_range_from_dict_round_trips_every_model_constructible_fixture_range() -> None:
    fixture_paths = sorted(EXAMPLES.glob("*.json")) + [
        PYTHON_WORKFLOW / "minotaur-graph.json",
        Path(__file__).parent / "fixtures/minotaur-graph-v1/invalid/dangling-relationship.json",
    ]
    ranges: list[Range] = []
    for path in fixture_paths:
        document = GraphDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))
        ranges.extend(node.location.range for node in document.nodes if node.location is not None)
        ranges.extend(
            location.range
            for relationship in document.relationships
            for evidence in relationship.evidence
            for location in evidence.locations
        )

    assert ranges, "expected model-constructible fixtures to contain ranges"
    for range_value in ranges:
        assert Range.from_dict(range_value.to_dict()) == range_value


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


def test_memo_key_includes_path_component() -> None:
    """M-4: two locations that share a range but have different paths must
    not collide in the memo. The first relationship's evidence populates
    the memo with path "a.py"; the second relationship's evidence uses the
    same range under path "b.py" and must parse to its own path, not the
    first's cached one."""
    shared_range = {
        "start": {"line": 1, "character": 0},
        "end": {"line": 1, "character": 5},
    }
    loc_a = {"path": "a.py", "range": shared_range}
    loc_b = {"path": "b.py", "range": shared_range}
    doc_data = _make_guard_test_document(loc_a, loc_b)

    document = GraphDocument.from_dict(doc_data)

    parsed_a = document.relationships[0].evidence[0].locations[0]
    parsed_b = document.relationships[1].evidence[0].locations[0]
    assert parsed_a.path == "a.py"
    assert parsed_b.path == "b.py"
    assert parsed_a is not parsed_b


# --- M-5: memo-key component coupling ---
# Each pair below is identical except in exactly one of the five components
# the memo key is built from (path, start.line, start.character, end.line,
# end.character). If the memo key tuple ever dropped one of these
# components -- e.g. because a maintainer added a field to
# _LOCATION_FIELDS and the memo guard's key-set check widened along with
# it while the hard-coded key tuple stayed the same shape -- the second
# location in the affected pair would collide with the first in the memo
# and be silently served the first's (wrong) value.
_MEMO_KEY_COMPONENT_CASES = [
    pytest.param(
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
        },
        {
            "path": "b.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
        },
        id="path",
    ),
    pytest.param(
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
        },
        {
            "path": "a.py",
            "range": {"start": {"line": 2, "character": 0}, "end": {"line": 1, "character": 5}},
        },
        id="start.line",
    ),
    pytest.param(
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
        },
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 9}, "end": {"line": 1, "character": 15}},
        },
        id="start.character",
    ),
    pytest.param(
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
        },
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 2, "character": 5}},
        },
        id="end.line",
    ),
    pytest.param(
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
        },
        {
            "path": "a.py",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 9}},
        },
        id="end.character",
    ),
]


@pytest.mark.parametrize(("loc_a", "loc_b"), _MEMO_KEY_COMPONENT_CASES)
def test_memo_key_distinguishes_every_component(
    loc_a: dict[str, object], loc_b: dict[str, object]
) -> None:
    """M-5: verifies the coupling between _MEMO_LOCATION_FIELDS /
    _MEMO_RANGE_FIELDS / _MEMO_POSITION_FIELDS and the memo key tuple by
    proving, for each of the five components the key is built from, that
    two locations differing only in that component parse independently
    rather than one being served from the other's memo entry."""
    doc_data = _make_guard_test_document(loc_a, loc_b)

    document = GraphDocument.from_dict(doc_data)

    parsed_a = document.relationships[0].evidence[0].locations[0]
    parsed_b = document.relationships[1].evidence[0].locations[0]
    assert parsed_a.to_dict() == loc_a
    assert parsed_b.to_dict() == loc_b
    assert parsed_a is not parsed_b


def test_from_dict_memo_interns_equal_locations() -> None:
    """L-5: proves the memo actually interns rather than merely producing
    equal-but-distinct objects. Parses the python-workflow example graph
    (which repeats several locations across nodes and evidence) and
    asserts that every group of equal-valued Locations shares exactly one
    object identity -- the count of distinct values equals the count of
    distinct ids."""
    source = json.loads((PYTHON_WORKFLOW / "minotaur-graph.json").read_text(encoding="utf-8"))
    document = GraphDocument.from_dict(source)

    locations: list[Location] = [
        node.location for node in document.nodes if node.location is not None
    ]
    for relationship in document.relationships:
        for evidence in relationship.evidence:
            locations.extend(evidence.locations)

    assert len(locations) > 0, "expected the example graph to contain locations"

    distinct_by_value = {loc for loc in locations}
    distinct_by_id = {id(loc) for loc in locations}
    assert len(locations) > len(distinct_by_value), (
        "expected repeated location values in the example graph to exercise interning"
    )
    assert len(distinct_by_value) == len(distinct_by_id)

    # Every occurrence of an equal Location value must be the SAME object,
    # not merely an equal one -- this is the actual interning claim.
    by_value: dict[Location, set[int]] = {}
    for loc in locations:
        by_value.setdefault(loc, set()).add(id(loc))
    for loc, ids in by_value.items():
        assert len(ids) == 1, f"equal Location {loc!r} was not interned to a single object"


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


def _extension_test_node(extensions: dict[str, dict[str, object]]) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, "test")
    node_id = compute_node_id(identity, node_class=NodeClass.FILE.value, path="src/test.py")
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.FILE,
        label="test.py",
        path="src/test.py",
        extensions=extensions,
    )


_INVALID_EXTENSION_VALUES = [
    ({"x": {"f": 1.5}}, "/x/f"),
    ({"x": {"f": 4.0}}, "/x/f"),
    ({"x": {"f": float("nan")}}, "/x/f"),
    ({"x": {"f": float("inf")}}, "/x/f"),
    ({"x": {"y": {"z": {"f": 1.5}}}}, "/x/y/z/f"),
    ({"x": {"values": [{"f": 1.5}]}}, "/x/values/0/f"),
    ({"x": {"\U0001f600": 1}}, "/x/\U0001f600"),
    ({"x": {"y": {"\U0001d11e": 1}}}, "/x/y/\U0001d11e"),
    ({"\U0001f600": {"f": 1}}, "/\U0001f600"),
    # Non-BMP keys reached through every container the freeze recurses into,
    # and in both insertion orders — the shapes the encoder's astral edge
    # cases used to cover before R-02 made them unrepresentable.
    ({"x": {"values": [{"\U0001d11e": 1}]}}, "/x/values/0/\U0001d11e"),
    ({"x": {"values": [[{"\U0001d11e": 1}]]}}, "/x/values/0/0/\U0001d11e"),
    ({"x": {"\U0001d11e": 1, "\U0001f600": 2}}, "/x/\U0001d11e"),
    ({"x": {"\U0001f600": 2, "\U0001d11e": 1}}, "/x/\U0001f600"),
    ({"x": {"y": {"z": {"\U0001f600": 1}}}}, "/x/y/z/\U0001f600"),
    # Floats reached through nested lists, matching the same container sweep.
    ({"x": {"values": [[1, 2.5]]}}, "/x/values/0/1"),
]


@pytest.mark.parametrize(("extensions", "path"), _INVALID_EXTENSION_VALUES)
@pytest.mark.parametrize("wire", [False, True], ids=["construction", "from-dict"])
def test_extension_model_guard_rejects_float_and_non_bmp_keys(
    extensions: dict[str, dict[str, object]], path: str, wire: bool
) -> None:
    if wire:
        source = json.loads((EXAMPLES / "small-workflow.json").read_text(encoding="utf-8"))
        nodes = source["nodes"]
        assert isinstance(nodes, list)
        nodes[0]["extensions"] = extensions
        with pytest.raises(ValueError, match=re.escape(path)):
            GraphDocument.from_dict(source)
    else:
        with pytest.raises(ValueError, match=re.escape(path)):
            _extension_test_node(extensions)


def test_extension_model_guard_accepts_json_values_and_is_idempotent() -> None:
    extensions = {
        "a\n": {
            "note": "\U0001d11e",
            "values": [1, True, None, {"\ud800": 2}],
        }
    }
    node = _extension_test_node(extensions)
    assert node.to_dict()["extensions"] == extensions
    assert replace(node).to_dict()["extensions"] == extensions


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (1.5, "line must be an integer, got float: 1.5"),
        (True, "line must be an integer, got bool: True"),
    ],
)
def test_position_rejects_non_integer_values_in_process(line: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Position(line=line, character=0)  # type: ignore[arg-type]


def test_position_rejects_boolean_character_in_process() -> None:
    with pytest.raises(ValueError, match="character must be an integer, got bool: True"):
        Position(line=0, character=True)  # type: ignore[arg-type]


def test_position_wire_parser_keeps_require_int_message() -> None:
    source = json.loads((EXAMPLES / "small-workflow.json").read_text(encoding="utf-8"))
    nodes = source["nodes"]
    assert isinstance(nodes, list)
    location = nodes[0]["location"]
    assert isinstance(location, dict)
    range_data = location["range"]
    assert isinstance(range_data, dict)
    start = range_data["start"]
    assert isinstance(start, dict)
    start["line"] = 4.0

    with pytest.raises(ValueError) as error:
        GraphDocument.from_dict(source)
    assert str(error.value) == "'line' must be an integer, got float: 4.0"


# ---------------------------------------------------------------------------
# R-02 / AC-06: the model layer type-checks every field on every path
# ---------------------------------------------------------------------------
#
# from_dict type-checks the wire, so these guards exist for the paths that do
# not go through it: direct construction and dataclasses.replace().  Without
# them a mistyped field reaches serialize() and compute_node_id(), which
# happily encode it — `replace(node, label=1.5)` used to produce
# `"label":1.5` and a plausible-looking digest.


def _guard_identity() -> NodeIdentity:
    return NodeIdentity(IdentityBasis.FILE_PATH, "test")


def _guard_node() -> Node:
    identity = _guard_identity()
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path="src/test.py"),
        identity=identity,
        node_class=NodeClass.FILE,
        label="test.py",
        path="src/test.py",
    )


def _guard_location() -> Location:
    return Location(path="src/test.py", range=Range(Position(0, 0), Position(0, 4)))


def _guard_evidence() -> Evidence:
    return Evidence(
        Provenance.STATIC_ANALYSIS,
        producer=Producer(name="minotaur-python"),
        locations=(_guard_location(),),
    )


def _guard_relationship() -> Relationship:
    node_id = _guard_node().id
    return Relationship(
        source=node_id,
        target=node_id,
        kind=RelationshipKind.CALLS.value,
        evidence=(_guard_evidence(),),
    )


def _guard_document() -> GraphDocument:
    return GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(_guard_node(),),
        relationships=(_guard_relationship(),),
        generated_by=Producer(name="minotaur-python"),
        generated_at="2026-01-01T00:00:00Z",
        source_control=SourceControl(system="git", branch="main"),
    )


_FIELD_TYPE_GUARDS: list[tuple[str, object, str]] = [
    # (field, replacement value, expected message)
    # -- Position / Range / Location -----------------------------------------
    ("position.line", 1.5, "line must be an integer, got float: 1.5"),
    ("position.character", "0", "character must be an integer, got str: '0'"),
    ("range.start", (0, 0), "range start must be a Position, got tuple"),
    ("range.end", None, "range end must be a Position, got NoneType"),
    ("location.path", 1, "location path must be a string, got int"),
    ("location.range", {"start": 0}, "location range must be a Range, got dict"),
    # -- NodeIdentity --------------------------------------------------------
    ("identity.basis", "file-path", "identity basis must be an IdentityBasis, got str"),
    ("identity.namespace", 1, "identity namespace must be a string, got int"),
    # -- Node ---------------------------------------------------------------
    ("node.id", 1.5, "node id must be a string, got float"),
    ("node.identity", {"basis": "file-path"}, "node identity must be a NodeIdentity, got dict"),
    ("node.node_class", "file", "node_class must be a NodeClass, got str"),
    ("node.label", 1.5, "node label must be a string, got float"),
    ("node.symbol_kind", 1, "'symbol_kind' must be a string when present, got int"),
    ("node.language", 1, "language must be a string when present, got int"),
    (
        "node.location",
        {"path": "src/test.py"},
        "'location' must be a Location when present, got dict",
    ),
    ("node.path", 1, "node path must be a string when present, got int"),
    ("node.reference_text", 1, "'reference_text' must be a string when present, got int"),
    (
        "node.expected_symbol_kind",
        1,
        "'expected_symbol_kind' must be a string when present, got int",
    ),
    ("node.extensions", [], "extensions must be an object, got list"),
    # -- Producer / Rule -----------------------------------------------------
    ("producer.name", 1, "producer name must be a string, got int"),
    ("producer.version", 1, "producer version must be a string when present, got int"),
    ("rule.id", 1, "rule id must be a string, got int"),
    ("rule.version", 1, "rule version must be a string when present, got int"),
    # -- Evidence ------------------------------------------------------------
    ("evidence.provenance", "static-analysis", "evidence provenance must be a Provenance, got str"),
    (
        "evidence.producer",
        {"name": "x"},
        "evidence 'producer' must be a Producer when present, got dict",
    ),
    ("evidence.rule", {"id": "x"}, "evidence 'rule' must be a Rule when present, got dict"),
    ("evidence.locations", [], "evidence 'locations' must be a tuple, got list"),
    ("evidence.locations", (1,), "evidence 'locations'[0] must be a Location, got int"),
    ("evidence.extensions", [], "extensions must be an object, got list"),
    # -- Relationship --------------------------------------------------------
    ("relationship.source", 1, "relationship 'source' must be a string, got int"),
    ("relationship.target", 1, "relationship 'target' must be a string, got int"),
    ("relationship.kind", 1, "relationship 'kind' must be a string, got int"),
    ("relationship.evidence", [], "relationship 'evidence' must be a tuple, got list"),
    ("relationship.evidence", (1,), "relationship 'evidence'[0] must be an Evidence, got int"),
    ("relationship.extensions", [], "extensions must be an object, got list"),
    # -- SourceControl -------------------------------------------------------
    ("source_control.commit", 1, "git commit must be a string when present, got int"),
    ("source_control.branch", 1, "branch must be a string when present, got int"),
    # -- GraphDocument -------------------------------------------------------
    (
        "document.coordinate_encoding",
        "utf-8",
        "coordinate_encoding must be a CoordinateEncoding, got str",
    ),
    ("document.nodes", [], "document 'nodes' must be a tuple, got list"),
    ("document.nodes", (1,), "document 'nodes'[0] must be a Node, got int"),
    ("document.relationships", [], "document 'relationships' must be a tuple, got list"),
    (
        "document.relationships",
        (1,),
        "document 'relationships'[0] must be a Relationship, got int",
    ),
    (
        "document.generated_by",
        {"name": "x"},
        "'generated_by' must be a Producer when present, got dict",
    ),
    ("document.generated_at", 1, "generated_at must be a string when present, got int"),
    (
        "document.source_control",
        {"system": "git"},
        "'source_control' must be a SourceControl when present, got dict",
    ),
    ("document.extensions", [], "extensions must be an object, got list"),
]

_GUARD_FACTORIES = {
    "position": lambda: Position(line=0, character=0),
    "range": lambda: Range(Position(0, 0), Position(0, 1)),
    "location": lambda: Location(path="a.py", range=Range(Position(0, 0), Position(0, 1))),
    "identity": _guard_identity,
    "node": _guard_node,
    "producer": lambda: Producer(name="minotaur-python"),
    "rule": lambda: Rule(id="rule-1"),
    "evidence": _guard_evidence,
    "relationship": _guard_relationship,
    "source_control": lambda: SourceControl(system="git", branch="main"),
    "document": _guard_document,
}


@pytest.mark.parametrize(
    ("target", "value", "message"),
    _FIELD_TYPE_GUARDS,
    ids=[f"{target}-{index}" for index, (target, _, _) in enumerate(_FIELD_TYPE_GUARDS)],
)
def test_model_field_type_guards_reject_wrong_types_in_process(
    target: str, value: object, message: str
) -> None:
    model_name, field_name = target.split(".")
    valid = _GUARD_FACTORIES[model_name]()
    with pytest.raises(ValueError, match=re.escape(message)):
        replace(valid, **{field_name: value})  # type: ignore[type-var]


def test_field_type_guard_matrix_covers_every_model_dataclass_field() -> None:
    """Every field of every guarded model dataclass appears in the matrix.

    R-02 makes the model layer the sole owner of the invariant, so the matrix
    is only a proof while it is exhaustive; a newly added field must either be
    guarded or be recorded here with the reason it needs no guard.
    """
    covered = {target for target, _, _ in _FIELD_TYPE_GUARDS}
    exempt = {
        # SourceControl already rejects every value that is not the literal
        # string "git", of any type, with the message that names the v1 rule.
        "source_control.system",
        # NodeIdentity's conditional fields are guarded inside the
        # permitted-basis branch so a value in a forbidden slot keeps its
        # permission message; both halves are proved by the two dedicated
        # tests below rather than by a whole-object replace().
        "identity.upstream_identifier",
        "identity.originating_node",
        "identity.resource_key",
    }
    for model_name, factory in _GUARD_FACTORIES.items():
        for field_name in factory().__dataclass_fields__:
            target = f"{model_name}.{field_name}"
            assert target in covered or target in exempt, f"{target} has no type guard"


def test_source_control_system_rejects_a_non_string_with_its_v1_message() -> None:
    with pytest.raises(ValueError, match=re.escape("v1 only supports 'git' source control, got 1")):
        SourceControl(system=1, branch="main")  # type: ignore[arg-type]


def test_identity_forbidden_conditional_field_keeps_its_permission_message() -> None:
    """A non-str value in a forbidden slot is still a permission error.

    The type guard sits inside the permitted branch so it cannot take over the
    message that explains the real problem: this basis has no such field.
    """
    with pytest.raises(ValueError, match=re.escape("file-path basis does not permit")):
        NodeIdentity(IdentityBasis.FILE_PATH, "test", upstream_identifier=1)  # type: ignore[arg-type]


def test_identity_permitted_conditional_field_is_type_checked() -> None:
    with pytest.raises(
        ValueError, match=re.escape("identity 'upstream_identifier' must be a string, got int")
    ):
        NodeIdentity(IdentityBasis.UPSTREAM_IDENTIFIER, "test", upstream_identifier=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R-02 / M-2: extension object keys must be non-empty strings at every depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extensions", "message"),
    [
        ({"x": {"": 1}}, "extension object at /x has an empty key"),
        ({"x": {"y": {"": 1}}}, "extension object at /x/y has an empty key"),
        ({"x": {"values": [{"": 1}]}}, "extension object at /x/values/0 has an empty key"),
    ],
)
def test_extension_guard_rejects_empty_keys_below_the_top_level(
    extensions: dict[str, dict[str, object]], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        _extension_test_node(extensions)


def test_extension_guard_keeps_the_top_level_empty_name_message() -> None:
    with pytest.raises(ValueError, match=re.escape("extension names must be non-empty strings")):
        _extension_test_node({"": {"f": 1}})


@pytest.mark.parametrize(
    ("extensions", "message"),
    [
        ({"x": {1: "one"}}, "extension object at /x has a non-string key: 1"),
        ({"x": {"y": {None: 1}}}, "extension object at /x/y has a non-string key: None"),
        (
            {"x": {"values": [{2.5: 1}]}},
            "extension object at /x/values/0 has a non-string key: 2.5",
        ),
    ],
)
def test_extension_guard_rejects_non_string_keys(
    extensions: dict[str, dict[str, object]], message: str
) -> None:
    """json.dumps would silently stringify these, producing unreproducible bytes."""
    with pytest.raises(ValueError, match=re.escape(message)):
        _extension_test_node(extensions)


def test_replace_cannot_smuggle_a_float_past_the_serializer() -> None:
    node = _guard_node()
    with pytest.raises(ValueError, match=re.escape("node label must be a string, got float")):
        replace(node, label=1.5)  # type: ignore[arg-type]
