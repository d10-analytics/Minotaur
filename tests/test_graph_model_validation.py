"""Behavioral coverage for the semantic validator (graph_model.validation)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from minotaur.graph_model import validation as validation_module
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence, Producer, Rule
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    Provenance,
    RelationshipKind,
    SymbolKind,
)
from minotaur.graph_model.relationship import Relationship
from minotaur.graph_model.validation import (
    IssueCode,
    ValidationIssue,
    ValidationReport,
    validate_document,
)

NS = "example"
CALLER_PATH = "src/app.py"
CALLEE_PATH = "src/lib.py"


def _loc(path: str, sl: int, sc: int, el: int, ec: int) -> Location:
    return Location(path, Range(Position(sl, sc), Position(el, ec)))


def _symbol(label: str, location: Location) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NS)
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=SymbolKind.FUNCTION.value,
        location=location,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=SymbolKind.FUNCTION.value,
        location=location,
    )


def _unresolved(origin: Node, text: str) -> Node:
    identity = NodeIdentity(IdentityBasis.UNRESOLVED_REFERENCE, NS, originating_node=origin.id)
    node_id = compute_node_id(
        identity, node_class=NodeClass.UNRESOLVED_REFERENCE.value, reference_text=text
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        label=text,
        reference_text=text,
    )


def _static(*locations: Location) -> Evidence:
    return Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        producer=Producer(name="minotaur-python", version="0.1"),
        locations=locations,
    )


def _calls(source: Node, target: Node, *evidence: Evidence) -> Relationship:
    return Relationship(source.id, target.id, RelationshipKind.CALLS.value, evidence)


CALLER = _symbol("main", _loc(CALLER_PATH, 0, 4, 0, 8))
CALLEE = _symbol("helper", _loc(CALLEE_PATH, 2, 4, 2, 10))
CALL_SITE = _loc(CALLER_PATH, 1, 4, 1, 12)


def _valid_document(
    encoding: CoordinateEncoding = CoordinateEncoding.UTF_8,
) -> GraphDocument:
    return GraphDocument(
        coordinate_encoding=encoding,
        nodes=(CALLER, CALLEE),
        relationships=(_calls(CALLER, CALLEE, _static(CALL_SITE)),),
    )


def _codes(report: ValidationReport) -> list[tuple[str, tuple[str | int, ...]]]:
    return [(issue.code.value, issue.path) for issue in report]


# --------------------------------------------------------------------------
# Baseline and API contract
# --------------------------------------------------------------------------


def test_valid_document_has_no_issues_and_report_is_iterable() -> None:
    report = validate_document(_valid_document())

    assert report.is_valid
    assert report.issues == ()
    assert list(report) == []
    # A report has no container truthiness: an empty (valid) report must not
    # be falsy, or `if report:` would read as the inverse of is_valid.
    assert bool(report) is True


def test_validation_is_deterministic_and_leaves_document_unchanged() -> None:
    document = _valid_document()
    before = document.to_dict()

    first = validate_document(document)
    second = validate_document(document)

    assert first == second
    assert document.to_dict() == before


def test_node_id_verification_is_optional_but_defaults_to_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trusted-load switch skips only digest verification.

    Keep the default strict so direct validator callers and the byte-loading
    path still detect mismatched IDs. The explicit opt-out is the narrow
    trusted-sidecar contract exercised by the loading tests.
    """

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("node-ID verification was unexpectedly called")

    monkeypatch.setattr(validation_module, "verify_node_id", _boom)

    with pytest.raises(AssertionError, match="unexpectedly called"):
        validate_document(_valid_document())
    assert validate_document(_valid_document(), verify_node_ids=False).is_valid


def test_json_pointer_escapes_per_rfc_6901() -> None:
    issue = ValidationIssue(IssueCode.NODE_ID_MISMATCH, ("nodes", 3, "a/b", "c~d"), "m")

    assert issue.json_pointer == "/nodes/3/a~1b/c~0d"


def test_non_string_source_text_is_a_caller_error() -> None:
    with pytest.raises(TypeError, match="must be str"):
        validate_document(_valid_document(), source_text_by_path={CALLER_PATH: b"bytes"})  # type: ignore[dict-item]


# --------------------------------------------------------------------------
# One mutation per finding code
# --------------------------------------------------------------------------


def test_tampered_node_id_reports_digest_mismatch() -> None:
    tampered = replace(CALLER, id="node:sha256:" + "0" * 64)
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(tampered, CALLEE),
        relationships=(_calls(tampered, CALLEE, _static(CALL_SITE)),),
    )

    assert _codes(validate_document(document)) == [("node-id-mismatch", ("nodes", 0, "id"))]


def test_identity_reconstruction_failure_degrades_to_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No structurally valid v1 node can make reconstruction raise; simulate an
    # identity-module regression to prove the report degrades instead of aborting.
    def _boom(*args: object, **kwargs: object) -> str:
        raise ValueError("simulated identity regression")

    monkeypatch.setattr(validation_module, "verify_node_id", _boom)

    report = validate_document(_valid_document())

    assert _codes(report) == [
        ("node-id-unverifiable", ("nodes", 0, "id")),
        ("node-id-unverifiable", ("nodes", 1, "id")),
    ]
    assert "simulated identity regression" in report.issues[0].message


def test_duplicate_node_id_reports_later_occurrence_only() -> None:
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, CALLEE, CALLER),
        relationships=(_calls(CALLER, CALLEE, _static(CALL_SITE)),),
    )

    assert _codes(validate_document(document)) == [("node-id-duplicate", ("nodes", 2, "id"))]


def test_missing_source_and_target_are_reported_independently() -> None:
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(),
        relationships=(_calls(CALLER, CALLEE, _static(CALL_SITE)),),
    )

    assert _codes(validate_document(document)) == [
        ("relationship-endpoint-missing", ("relationships", 0, "source")),
        ("relationship-endpoint-missing", ("relationships", 0, "target")),
    ]


def test_self_relationship_is_valid() -> None:
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER,),
        relationships=(_calls(CALLER, CALLER, _static(CALL_SITE)),),
    )

    assert validate_document(document).is_valid


def test_duplicate_relationship_tuple_reports_later_occurrence() -> None:
    first = _calls(CALLER, CALLEE, _static(CALL_SITE))
    second = _calls(CALLER, CALLEE, _static(_loc(CALLER_PATH, 3, 0, 3, 5)))
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, CALLEE),
        relationships=(first, second),
    )

    assert _codes(validate_document(document)) == [("relationship-duplicate", ("relationships", 1))]


def test_reversed_range_on_node_and_evidence_is_reported_at_range_path() -> None:
    bad_node = _symbol("bad", _loc(CALLEE_PATH, 5, 3, 5, 1))
    bad_site = _loc(CALLER_PATH, 2, 0, 1, 0)
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, bad_node),
        relationships=(_calls(CALLER, bad_node, _static(bad_site)),),
    )

    assert _codes(validate_document(document)) == [
        ("range-end-before-start", ("nodes", 1, "location", "range")),
        ("range-end-before-start", ("relationships", 0, "evidence", 0, "locations", 0, "range")),
    ]


def test_zero_width_range_is_valid() -> None:
    cursor = _symbol("cursor", _loc(CALLEE_PATH, 4, 2, 4, 2))
    document = GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8, nodes=(cursor,))

    assert validate_document(document).is_valid


def test_unresolved_origin_must_exist_in_document() -> None:
    orphan_origin = _symbol("gone", _loc(CALLEE_PATH, 9, 0, 9, 4))
    unresolved = _unresolved(orphan_origin, "missing.thing")
    document = GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8, nodes=(unresolved,))

    assert _codes(validate_document(document)) == [
        ("identity-origin-missing", ("nodes", 0, "identity", "originating_node"))
    ]


def test_relationship_targeting_unresolved_node_must_use_references() -> None:
    unresolved = _unresolved(CALLER, "missing.thing")
    calls = _calls(CALLER, unresolved, _static(CALL_SITE))
    references = Relationship(
        CALLER.id, unresolved.id, RelationshipKind.REFERENCES.value, (_static(CALL_SITE),)
    )
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, unresolved),
        relationships=(references, calls),
    )

    assert _codes(validate_document(document)) == [
        ("relationship-unresolved-target-kind", ("relationships", 1, "kind"))
    ]


def test_duplicate_evidence_attribution_is_reported_regardless_of_locations() -> None:
    other_site = _loc(CALLER_PATH, 3, 0, 3, 5)
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, CALLEE),
        relationships=(_calls(CALLER, CALLEE, _static(CALL_SITE), _static(other_site)),),
    )

    assert _codes(validate_document(document)) == [
        ("evidence-duplicate", ("relationships", 0, "evidence", 1))
    ]


def test_distinct_attribution_with_same_locations_is_not_a_duplicate() -> None:
    curated = Evidence(
        provenance=Provenance.CURATED_RULE,
        rule=Rule(id="rule-1"),
        locations=(CALL_SITE,),
    )
    with_extension = Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        producer=Producer(name="minotaur-python", version="0.1"),
        locations=(CALL_SITE,),
        extensions={"example.com": {"score": 1}},
    )
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, CALLEE),
        relationships=(_calls(CALLER, CALLEE, _static(CALL_SITE), curated, with_extension),),
    )

    assert validate_document(document).is_valid


def test_duplicate_location_within_one_evidence_record_is_reported() -> None:
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, CALLEE),
        relationships=(_calls(CALLER, CALLEE, _static(CALL_SITE, CALL_SITE)),),
    )

    assert _codes(validate_document(document)) == [
        ("evidence-location-duplicate", ("relationships", 0, "evidence", 0, "locations", 1))
    ]


def test_unsorted_but_unique_evidence_locations_are_accepted() -> None:
    later = _loc(CALLER_PATH, 7, 0, 7, 3)
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, CALLEE),
        relationships=(_calls(CALLER, CALLEE, _static(later, CALL_SITE)),),
    )

    assert validate_document(document).is_valid


# --------------------------------------------------------------------------
# Source-text bounds
# --------------------------------------------------------------------------

# Line 0: "def main():" (11 chars); line 1: "    helper()" (12 chars). The
# CALLER node spans (0,4)-(0,8) and CALL_SITE spans (1,4)-(1,12), so both
# fit exactly; (1,12) exercises "position at encoded length is valid".
CALLER_TEXT = "def main():\n    helper()\n"


def test_bounds_are_skipped_only_for_paths_without_supplied_text() -> None:
    # CALLEE_PATH is not supplied, so its (wildly out-of-bounds) location is not
    # checked; CALLER_PATH is supplied, so its location IS checked in the same call.
    far_away = _symbol("far", _loc(CALLEE_PATH, 900, 0, 900, 5))
    overflow = _symbol("overflow", _loc(CALLER_PATH, 0, 0, 0, 99))
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8, nodes=(far_away, overflow)
    )

    report = validate_document(document, source_text_by_path={CALLER_PATH: CALLER_TEXT})

    assert _codes(report) == [
        ("position-character-out-of-bounds", ("nodes", 1, "location", "range", "end", "character"))
    ]


def test_bounds_are_skipped_entirely_when_no_source_map_is_supplied() -> None:
    far_away = _symbol("far", _loc(CALLEE_PATH, 900, 0, 900, 5))
    document = GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8, nodes=(far_away,))

    assert validate_document(document).is_valid
    assert validate_document(document, source_text_by_path={}).is_valid


def test_positions_within_supplied_source_are_valid() -> None:
    report = validate_document(_valid_document(), source_text_by_path={CALLER_PATH: CALLER_TEXT})

    assert report.is_valid


def test_line_and_character_out_of_bounds_are_reported_at_position_paths() -> None:
    # end line 5 does not exist; start character 13 exceeds line 1's length (12).
    beyond = _loc(CALLER_PATH, 1, 13, 5, 0)
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, CALLEE),
        relationships=(_calls(CALLER, CALLEE, _static(beyond)),),
    )

    report = validate_document(document, source_text_by_path={CALLER_PATH: CALLER_TEXT})

    base: tuple[str | int, ...] = ("relationships", 0, "evidence", 0, "locations", 0, "range")
    assert _codes(report) == [
        ("position-character-out-of-bounds", (*base, "start", "character")),
        ("position-line-out-of-bounds", (*base, "end", "line")),
    ]


def test_trailing_newline_yields_final_empty_line() -> None:
    # "a\n" has lines ["a", ""]: (1, 0) is valid, (2, 0) is not, (1, 1) is not.
    ok = _symbol("ok", _loc(CALLEE_PATH, 0, 0, 1, 0))
    line_beyond = _symbol("line", _loc(CALLEE_PATH, 0, 0, 2, 0))
    char_beyond = _symbol("char", _loc(CALLEE_PATH, 0, 0, 1, 1))
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8, nodes=(ok, line_beyond, char_beyond)
    )

    report = validate_document(document, source_text_by_path={CALLEE_PATH: "a\n"})

    assert _codes(report) == [
        ("position-line-out-of-bounds", ("nodes", 1, "location", "range", "end", "line")),
        ("position-character-out-of-bounds", ("nodes", 2, "location", "range", "end", "character")),
    ]


@pytest.mark.parametrize("text", ["ab\ncd", "ab\r\ncd", "ab\rcd"])
def test_lf_crlf_and_cr_all_split_lines_and_exclude_terminator(text: str) -> None:
    # (0, 2) is the end of "ab" under every terminator style; (0, 3) is not,
    # because the terminator is not part of the line's content.
    at_end = _symbol("end", _loc(CALLEE_PATH, 0, 0, 0, 2))
    past_end = _symbol("past", _loc(CALLEE_PATH, 0, 0, 0, 3))
    document = GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8, nodes=(at_end, past_end))

    report = validate_document(document, source_text_by_path={CALLEE_PATH: text})

    assert _codes(report) == [
        ("position-character-out-of-bounds", ("nodes", 1, "location", "range", "end", "character"))
    ]


def test_form_feed_and_unicode_separators_do_not_split_lines() -> None:
    # str.splitlines() would split on \f and  ; the validator must not.
    text = "a\fb c"
    inside = _symbol("inside", _loc(CALLEE_PATH, 0, 0, 0, 5))  # utf-32 length of the one line
    document = GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_32, nodes=(inside,))

    assert validate_document(document, source_text_by_path={CALLEE_PATH: text}).is_valid


@pytest.mark.parametrize(
    ("encoding", "valid_end", "invalid_end"),
    [
        # "é😀" is 2+4 = 6 UTF-8 bytes, 1+2 = 3 UTF-16 code units, 2 UTF-32 code points.
        (CoordinateEncoding.UTF_8, 6, 7),
        (CoordinateEncoding.UTF_16, 3, 4),
        (CoordinateEncoding.UTF_32, 2, 3),
    ],
)
def test_character_bounds_count_in_the_declared_encoding(
    encoding: CoordinateEncoding, valid_end: int, invalid_end: int
) -> None:
    fits = _symbol("fits", _loc(CALLEE_PATH, 0, 0, 0, valid_end))
    overflows = _symbol("overflows", _loc(CALLEE_PATH, 0, 0, 0, invalid_end))
    document = GraphDocument(coordinate_encoding=encoding, nodes=(fits, overflows))

    report = validate_document(document, source_text_by_path={CALLEE_PATH: "é😀"})

    assert _codes(report) == [
        ("position-character-out-of-bounds", ("nodes", 1, "location", "range", "end", "character"))
    ]


# --------------------------------------------------------------------------
# Multi-fault ordering
# --------------------------------------------------------------------------


def test_multi_fault_document_reports_every_finding_in_documented_order() -> None:
    tampered_callee = replace(CALLEE, id="node:sha256:" + "1" * 64)
    reversed_node = _symbol("rev", _loc(CALLEE_PATH, 5, 3, 5, 1))
    unresolved = _unresolved(CALLER, "missing.thing")
    ghost = _symbol("ghost", _loc(CALLEE_PATH, 8, 0, 8, 5))  # never declared

    rel_calls_unresolved = _calls(CALLER, unresolved, _static(CALL_SITE))
    rel_ghost = _calls(CALLER, ghost, _static(CALL_SITE))
    rel_dup_evidence = _calls(
        CALLER, tampered_callee, _static(CALL_SITE), _static(CALL_SITE, CALL_SITE)
    )
    rel_dup_tuple = _calls(CALLER, tampered_callee, _static(_loc(CALLER_PATH, 3, 0, 3, 1)))

    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(CALLER, tampered_callee, reversed_node, CALLER, unresolved),
        relationships=(rel_calls_unresolved, rel_ghost, rel_dup_evidence, rel_dup_tuple),
    )

    report = validate_document(document, source_text_by_path={CALLER_PATH: CALLER_TEXT})

    assert not report.is_valid
    ev0: tuple[str | int, ...] = ("evidence", 0, "locations", 0, "range")
    assert _codes(report) == [
        # nodes, in document order (CALLER at 0 and 3 fit CALLER_TEXT; CALLEE_PATH has no text)
        ("node-id-mismatch", ("nodes", 1, "id")),
        ("range-end-before-start", ("nodes", 2, "location", "range")),
        ("node-id-duplicate", ("nodes", 3, "id")),
        # relationships, in document order
        ("relationship-unresolved-target-kind", ("relationships", 0, "kind")),
        ("relationship-endpoint-missing", ("relationships", 1, "target")),
        ("evidence-duplicate", ("relationships", 2, "evidence", 1)),
        ("evidence-location-duplicate", ("relationships", 2, "evidence", 1, "locations", 1)),
        ("relationship-duplicate", ("relationships", 3)),
        ("position-line-out-of-bounds", ("relationships", 3, *ev0, "start", "line")),
        ("position-line-out-of-bounds", ("relationships", 3, *ev0, "end", "line")),
    ]
