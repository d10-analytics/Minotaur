"""Semantic validation for Minotaur graph documents.

Graph handling proceeds in phases: parse JSON, validate against the JSON
Schema, construct the graph model (``GraphDocument.from_dict``), run this
semantic validator, canonically normalize, then render or slice. The
structural phases reject anything that is wrong about a single object's own
fields. This module handles what they cannot: rules that need cross-object
knowledge (do relationship endpoints exist? are node IDs unique?) or
computation (does the declared node ID match its recomputed digest? does a
range fall inside the referenced source text?).

Design rules that every check here obeys:

- The validator is pure. It never opens files, never mutates the document,
  and never sorts, deduplicates, coerces, infers defaults, or repairs input.
  Canonical ordering is ``serialization.py``'s job; this module reports on
  the document exactly as loaded.
- It reports every independently discoverable finding rather than stopping
  at the first, so a producer can fix a whole document in one pass.
- Findings are emitted in a fixed, documented order (see ``validate_document``)
  so identical input always yields an identical report.
- ``GraphDocument.from_dict`` does not call this module, and there is no
  raising convenience wrapper. The caller decides what an invalid report
  means for its flow.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.identity import verify_node_id
from minotaur.graph_model.location import Location, Position, encoded_length, split_lines
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    RelationshipKind,
)
from minotaur.graph_model.relationship import Relationship

# A wire-format path into the document: field names and array indices in the
# order they would be traversed in the JSON, e.g. ("relationships", 2, "source").
IssuePath = tuple[str | int, ...]


class IssueCode(str, Enum):
    """The closed vocabulary of semantic finding codes.

    Codes are stable identifiers for tests, CLI output, and any future
    machine consumer. Adding a code is a contract change and belongs in the
    documented semantic-validation decision, not in an ad-hoc branch.
    """

    NODE_ID_MISMATCH = "node-id-mismatch"
    # Emitted when the identity input cannot be reconstructed or JCS-encoded.
    # The structural layer closes every known v1 trigger (per-basis fields,
    # basis/class coupling, unpaired surrogates), so this is a defensive
    # degradation path: an identity-module regression becomes a finding
    # instead of aborting the report. It is part of the public code list.
    NODE_ID_UNVERIFIABLE = "node-id-unverifiable"
    NODE_ID_DUPLICATE = "node-id-duplicate"
    IDENTITY_ORIGIN_MISSING = "identity-origin-missing"
    RANGE_END_BEFORE_START = "range-end-before-start"
    POSITION_LINE_OUT_OF_BOUNDS = "position-line-out-of-bounds"
    POSITION_CHARACTER_OUT_OF_BOUNDS = "position-character-out-of-bounds"
    RELATIONSHIP_ENDPOINT_MISSING = "relationship-endpoint-missing"
    RELATIONSHIP_DUPLICATE = "relationship-duplicate"
    RELATIONSHIP_UNRESOLVED_TARGET_KIND = "relationship-unresolved-target-kind"
    EVIDENCE_DUPLICATE = "evidence-duplicate"
    EVIDENCE_LOCATION_DUPLICATE = "evidence-location-duplicate"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One semantic finding: what rule failed, where, and a human message."""

    code: IssueCode
    path: IssuePath
    message: str

    @property
    def json_pointer(self) -> str:
        """Render ``path`` as an RFC 6901 JSON Pointer (``/relationships/2/source``).

        The tuple form is the primary API because it is directly usable by
        Python callers; the pointer form exists because it is the standard
        cross-language way to name a location inside a JSON document and is
        what a CLI or log line should print.
        """
        parts = []
        for segment in self.path:
            text = str(segment)
            parts.append(text.replace("~", "~0").replace("/", "~1"))
        return "".join(f"/{part}" for part in parts)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The complete, ordered result of one ``validate_document`` call."""

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.issues

    # Deliberately no __len__/__bool__: an empty report is the VALID case, so
    # container truthiness would invert `is_valid` and make `if report:` a trap.
    def __iter__(self) -> Iterator[ValidationIssue]:
        return iter(self.issues)


def validate_document(
    document: GraphDocument,
    *,
    source_text_by_path: Mapping[str, str] | None = None,
    verify_node_ids: bool = True,
) -> ValidationReport:
    """Run every semantic check over an already constructed ``GraphDocument``.

    ``source_text_by_path`` optionally supplies decoded Unicode source text
    keyed by the exact repository-relative path used in locations. Source
    availability is an external policy concern (the caller may be sandboxed,
    or the text may be private), so it is never read from disk here. A path
    that is absent from the mapping skips only that location's bounds check;
    every other check still runs. Supplying a non-``str`` value is a caller
    contract error and raises ``TypeError`` rather than producing a finding.
    ``verify_node_ids`` defaults to ``True`` so direct callers retain the
    complete semantic contract. A trusted graph-file load may set it to
    ``False`` when its sidecar matches the exact input bytes: the sidecar
    vouches for the bytes that were previously validated, so recomputing each
    node digest would prove nothing new. This switch affects only the digest
    check; all other semantic checks below remain active.

    Finding order is object-major and fixed:

    1. Nodes, in document order. Per node: ``node-id-mismatch`` /
       ``node-id-unverifiable``, ``node-id-duplicate``,
       ``identity-origin-missing``, then the node location's
       ``range-end-before-start`` and (when text is available) position
       bounds — start before end, line before character.
    2. Relationships, in document order. Per relationship:
       ``relationship-endpoint-missing`` for ``source`` then ``target``,
       ``relationship-duplicate``, ``relationship-unresolved-target-kind``,
       then evidence records in order. Per evidence record:
       ``evidence-duplicate``, then each location in order: range ordering,
       position bounds, ``evidence-location-duplicate``.
    """
    _check_source_text_contract(source_text_by_path)
    lines_by_path = _SourceLines(source_text_by_path, document.coordinate_encoding)
    issues: list[ValidationIssue] = []

    # One index serves endpoint membership and target-class lookup. For a
    # duplicated ID the last node wins, which only affects the class used by
    # the unresolved-target check; the duplicate itself is reported separately.
    node_class_by_id = {node.id: node.node_class for node in document.nodes}

    seen_node_ids: set[str] = set()
    for index, node in enumerate(document.nodes):
        node_path: IssuePath = ("nodes", index)
        if verify_node_ids:
            _check_node_digest(node, node_path, issues)
        if node.id in seen_node_ids:
            issues.append(
                ValidationIssue(
                    IssueCode.NODE_ID_DUPLICATE,
                    (*node_path, "id"),
                    f"node id {node.id!r} is already declared by an earlier node",
                )
            )
        seen_node_ids.add(node.id)
        _check_identity_origin(node, node_path, node_class_by_id, issues)
        if node.location is not None:
            _check_location(node.location, (*node_path, "location"), lines_by_path, issues)

    seen_tuples: set[tuple[str, str, str]] = set()
    for index, relationship in enumerate(document.relationships):
        rel_path: IssuePath = ("relationships", index)
        for endpoint, node_id in (("source", relationship.source), ("target", relationship.target)):
            if node_id not in node_class_by_id:
                issues.append(
                    ValidationIssue(
                        IssueCode.RELATIONSHIP_ENDPOINT_MISSING,
                        (*rel_path, endpoint),
                        f"relationship {endpoint} {node_id!r} does not identify a declared node",
                    )
                )
        if relationship.tuple_key in seen_tuples:
            issues.append(
                ValidationIssue(
                    IssueCode.RELATIONSHIP_DUPLICATE,
                    rel_path,
                    "relationship (source, target, kind) tuple duplicates an earlier relationship",
                )
            )
        seen_tuples.add(relationship.tuple_key)
        _check_unresolved_target_kind(relationship, rel_path, node_class_by_id, issues)
        _check_evidence(relationship, rel_path, lines_by_path, issues)

    return ValidationReport(tuple(issues))


# ---------------------------------------------------------------------------
# Node checks
# ---------------------------------------------------------------------------


def _check_node_digest(node: Node, node_path: IssuePath, issues: list[ValidationIssue]) -> None:
    # A loaded Node satisfies every structural precondition of reconstruction
    # (per-basis fields, basis/class coupling, JCS-encodable strings), so this
    # should not raise for a v1 document. The catch is defensive: a future
    # basis added to the model without a matching branch in
    # identity._build_canonical_input, or a new non-encodable input, must
    # surface as a finding, not abort the whole report.
    try:
        matches = verify_node_id(
            node.id,
            node.identity,
            node_class=node.node_class.value,
            symbol_kind=node.symbol_kind,
            path=node.path,
            location=node.location,
            reference_text=node.reference_text,
        )
    except (ValueError, TypeError) as error:
        issues.append(
            ValidationIssue(
                IssueCode.NODE_ID_UNVERIFIABLE,
                (*node_path, "id"),
                f"node id could not be reconstructed from its identity: {error}",
            )
        )
        return
    if not matches:
        issues.append(
            ValidationIssue(
                IssueCode.NODE_ID_MISMATCH,
                (*node_path, "id"),
                f"node id {node.id!r} does not match the digest recomputed from its identity",
            )
        )


def _check_identity_origin(
    node: Node,
    node_path: IssuePath,
    declared_ids: Mapping[str, NodeClass],
    issues: list[ValidationIssue],
) -> None:
    identity = node.identity
    if identity.basis != IdentityBasis.UNRESOLVED_REFERENCE:
        return
    # Structural validation guarantees originating_node is present for this
    # basis; existence in the document is the semantic question.
    if identity.originating_node not in declared_ids:
        issues.append(
            ValidationIssue(
                IssueCode.IDENTITY_ORIGIN_MISSING,
                (*node_path, "identity", "originating_node"),
                f"originating node {identity.originating_node!r} is not declared in this document",
            )
        )


# ---------------------------------------------------------------------------
# Relationship and evidence checks
# ---------------------------------------------------------------------------


def _check_unresolved_target_kind(
    relationship: Relationship,
    rel_path: IssuePath,
    node_class_by_id: Mapping[str, NodeClass],
    issues: list[ValidationIssue],
) -> None:
    # An unresolved-reference node is a placeholder for text that could not
    # be resolved. Connecting to it with `calls`, `inherits`, etc. would
    # assert that the text identifies a known callable/type; the accepted
    # contract is that only `references` may point at such a placeholder.
    target_class = node_class_by_id.get(relationship.target)
    if target_class is None or target_class != NodeClass.UNRESOLVED_REFERENCE:
        return
    if relationship.kind != RelationshipKind.REFERENCES.value:
        issues.append(
            ValidationIssue(
                IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND,
                (*rel_path, "kind"),
                f"relationship kind {relationship.kind!r} targets an unresolved-reference node; "
                f"only '{RelationshipKind.REFERENCES.value}' may",
            )
        )


def _check_evidence(
    relationship: Relationship,
    rel_path: IssuePath,
    lines_by_path: _SourceLines,
    issues: list[ValidationIssue],
) -> None:
    # Evidence records within one relationship are unique by their complete
    # content EXCLUDING locations; Evidence.attribution_key is that identity
    # (and documents the accepted equality quirks).
    seen_attributions: set[tuple[object, ...]] = set()
    for ev_index, evidence in enumerate(relationship.evidence):
        ev_path: IssuePath = (*rel_path, "evidence", ev_index)
        attribution = evidence.attribution_key
        if attribution in seen_attributions:
            issues.append(
                ValidationIssue(
                    IssueCode.EVIDENCE_DUPLICATE,
                    ev_path,
                    "evidence record duplicates an earlier record's provenance, producer, "
                    "rule, and extensions",
                )
            )
        seen_attributions.add(attribution)

        seen_locations: set[Location] = set()
        for loc_index, location in enumerate(evidence.locations):
            loc_path: IssuePath = (*ev_path, "locations", loc_index)
            _check_location(location, loc_path, lines_by_path, issues)
            if location in seen_locations:
                issues.append(
                    ValidationIssue(
                        IssueCode.EVIDENCE_LOCATION_DUPLICATE,
                        loc_path,
                        "location duplicates an earlier location on the same evidence record",
                    )
                )
            seen_locations.add(location)


# ---------------------------------------------------------------------------
# Location checks
# ---------------------------------------------------------------------------


def _check_location(
    location: Location,
    loc_path: IssuePath,
    lines_by_path: _SourceLines,
    issues: list[ValidationIssue],
) -> None:
    start = location.range.start
    end = location.range.end
    # Half-open ranges: zero-width (start == end) is valid; end before start is not.
    if end < start:
        issues.append(
            ValidationIssue(
                IssueCode.RANGE_END_BEFORE_START,
                (*loc_path, "range"),
                f"range end ({end.line}, {end.character}) precedes "
                f"start ({start.line}, {start.character})",
            )
        )
    lines = lines_by_path.get(location.path)
    if lines is None:
        return  # source unavailable: bounds are not checkable, not a finding
    _check_position_bounds(start, lines, (*loc_path, "range", "start"), issues)
    _check_position_bounds(end, lines, (*loc_path, "range", "end"), issues)


def _check_position_bounds(
    position: Position,
    line_lengths: tuple[int, ...],
    pos_path: IssuePath,
    issues: list[ValidationIssue],
) -> None:
    if position.line >= len(line_lengths):
        issues.append(
            ValidationIssue(
                IssueCode.POSITION_LINE_OUT_OF_BOUNDS,
                (*pos_path, "line"),
                f"line {position.line} is outside the source text ({len(line_lengths)} line(s))",
            )
        )
        return  # character bounds are meaningless on a non-existent line
    # A position AT the encoded line length is valid: it addresses the end
    # of the line (LSP convention). Beyond it is not.
    if position.character > line_lengths[position.line]:
        issues.append(
            ValidationIssue(
                IssueCode.POSITION_CHARACTER_OUT_OF_BOUNDS,
                (*pos_path, "character"),
                f"character {position.character} is outside line {position.line} "
                f"(encoded length {line_lengths[position.line]})",
            )
        )


class _SourceLines:
    """Lazily computed per-path encoded line lengths for bounds checking.

    Line lengths are computed at most once per path and only for paths that
    a location actually references, so supplying a large source map costs
    nothing for unreferenced files.
    """

    def __init__(
        self,
        source_text_by_path: Mapping[str, str] | None,
        encoding: CoordinateEncoding,
    ) -> None:
        self._source = source_text_by_path
        self._encoding = encoding
        self._cache: dict[str, tuple[int, ...]] = {}

    def get(self, path: str) -> tuple[int, ...] | None:
        if self._source is None:
            return None
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        text = self._source.get(path)
        if text is None:
            return None
        lengths = tuple(encoded_length(line, self._encoding) for line in split_lines(text))
        self._cache[path] = lengths
        return lengths


def _check_source_text_contract(source_text_by_path: Mapping[str, str] | None) -> None:
    if source_text_by_path is None:
        return
    for path, text in source_text_by_path.items():
        if not isinstance(text, str):
            raise TypeError(
                f"source_text_by_path[{path!r}] must be str (decoded Unicode text), "
                f"got {type(text).__name__}"
            )
