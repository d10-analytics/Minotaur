"""Pure semantic correspondence preparation for graph snapshots.

This module deliberately sits below reporting and rendering.  Preparation
first runs the graph model's complete semantic validator, then builds an
immutable index of complete semantic keys.  A caller may subsequently validate
only the relationship keys it needs; absent relationship keys do not activate
local candidate ambiguity.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import IdentityBasis, NodeClass, RelationshipKind
from minotaur.graph_model.relationship import Relationship
from minotaur.graph_model.validation import ValidationReport, validate_document

NodeKey: TypeAlias = tuple[object, ...]
RelationshipKey: TypeAlias = tuple[NodeKey, NodeKey, str]

_SUPPORTED_RELATIONSHIPS = frozenset(
    {
        RelationshipKind.CALLS.value,
        RelationshipKind.REFERENCES.value,
        RelationshipKind.IMPORTS.value,
    }
)


def _sort_token(value: object) -> tuple[object, ...]:
    """Return a total, type-tagged ordering token for nested key values."""
    if value is None:
        return (0, "")
    if isinstance(value, tuple):
        return (1, tuple(_sort_token(item) for item in value))
    if isinstance(value, str):
        return (2, value)
    return (3, repr(value))


def _key_sort(key: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(_sort_token(item) for item in key)


def _node_sort(node: Node) -> tuple[object, ...]:
    location = node.location
    if location is None:
        site: tuple[object, ...] = (0, node.path or "", -1, -1)
    else:
        site = (1, location.path, location.range.start.line, location.range.start.character)
    return (*site, node.id, node.label)


def _derived_file(node: Node) -> str | None:
    if node.location is not None:
        return node.location.path
    return node.path


def node_key(node: Node, *, origin: NodeKey | None = None) -> NodeKey:
    """Build the complete semantic key for one node.

    The returned nested tuple contains every field in the approved identity
    domain.  Resource ``symbol_kind`` is intentionally retained only on the
    original node and never enters its correspondence key.
    """
    identity = node.identity
    basis = identity.basis
    if basis == IdentityBasis.SOURCE_LOCATION:
        fields: tuple[object, ...] = (
            basis.value,
            node.node_class.value,
            identity.namespace,
            _derived_file(node),
            node.label,
        )
        if node.node_class == NodeClass.SYMBOL:
            fields += (node.symbol_kind,)
        return fields
    if basis == IdentityBasis.FILE_PATH:
        return (basis.value, node.node_class.value, identity.namespace, node.path)
    if basis == IdentityBasis.UPSTREAM_IDENTIFIER:
        fields = (
            basis.value,
            node.node_class.value,
            identity.namespace,
            identity.upstream_identifier,
        )
        if node.node_class == NodeClass.SYMBOL:
            fields += (node.symbol_kind,)
        return fields
    if basis == IdentityBasis.RESOURCE_KEY:
        return (basis.value, node.node_class.value, identity.namespace, identity.resource_key)
    if basis == IdentityBasis.UNRESOLVED_REFERENCE:
        if origin is None:
            raise ValueError("unresolved-reference correspondence key requires its origin key")
        return (
            basis.value,
            node.node_class.value,
            identity.namespace,
            origin,
            node.reference_text,
            _derived_file(node),
        )
    raise ValueError(f"unsupported identity basis {basis!r}")


@dataclass(frozen=True, slots=True)
class RelationshipOccurrence:
    """One original paired relationship and its endpoint nodes."""

    relationship: Relationship
    source: Node
    target: Node
    source_key: NodeKey
    target_key: NodeKey

    @property
    def key(self) -> RelationshipKey:
        return (self.source_key, self.target_key, self.relationship.kind)


class CorrespondenceError(ValueError):
    """Base class for admission, eligibility, and selective ambiguity errors."""


class CorrespondenceAdmissionError(CorrespondenceError):
    """The canonical graph validator found one or more admission issues."""

    def __init__(
        self,
        report: ValidationReport,
        document: GraphDocument,
        *,
        side: str = "local",
    ) -> None:
        self.report = report
        self.document = document
        self.side = side
        self.issues = report.issues
        details = "; ".join(f"{issue.code.value} at {issue.json_pointer}" for issue in report)
        super().__init__(f"graph correspondence admission failed: {details}")


class CorrespondenceEligibilityError(CorrespondenceError):
    """A supported relationship has an unresolved endpoint without an ordinary origin."""

    def __init__(
        self,
        relationship: Relationship,
        endpoint: str,
        node: Node,
        origin: Node,
        *,
        side: str = "local",
    ) -> None:
        self.relationship = relationship
        self.endpoint = endpoint
        self.node = node
        self.origin = origin
        self.side = side
        super().__init__(
            f"{relationship.kind} {endpoint} {node.id!r} has unresolved direct origin "
            f"{origin.id!r}; comparison requires an ordinary origin"
        )


class CorrespondenceAmbiguityError(CorrespondenceError):
    """A requested local relationship needs a non-unique semantic candidate."""

    def __init__(
        self,
        relationship_key: RelationshipKey,
        *,
        side: str,
        endpoint: str,
        candidates: Iterable[Node],
        origin: bool = False,
    ) -> None:
        self.relationship_key = relationship_key
        self.side = side
        self.endpoint = endpoint
        self.origin = origin
        self.candidates = tuple(sorted(candidates, key=_node_sort))
        self.candidate_ids = tuple(node.id for node in self.candidates)
        kind = "origin" if origin else endpoint
        rendered = ", ".join(_candidate_site(node) for node in self.candidates)
        super().__init__(
            f"ambiguous {side} {kind} for requested relationship {relationship_key!r}: {rendered}"
        )


def _candidate_site(node: Node) -> str:
    if node.location is not None:
        return f"{node.id} ({node.location.path}:{node.location.range.start.line})"
    return f"{node.id} ({node.path or node.label})"


@dataclass(frozen=True, slots=True)
class CorrespondenceIndex:
    """Immutable per-document correspondence state.

    ``nodes_by_key`` includes every ordinary node and unresolved endpoint
    occurrence.  ``relationships_by_key`` retains each original relationship
    as a paired occurrence.  ``validate_required_keys`` applies the selective
    ambiguity policy and returns this same immutable result for chaining.
    """

    document: GraphDocument
    nodes_by_id: Mapping[str, Node]
    nodes_by_key: Mapping[NodeKey, tuple[Node, ...]]
    relationships_by_key: Mapping[RelationshipKey, tuple[RelationshipOccurrence, ...]]
    origin_dependencies: Mapping[NodeKey, NodeKey]

    @property
    def candidate_groups(self) -> Mapping[NodeKey, tuple[Node, ...]]:
        return self.nodes_by_key

    @property
    def relationship_groups(self) -> Mapping[RelationshipKey, tuple[RelationshipOccurrence, ...]]:
        return self.relationships_by_key

    def validate_required_keys(
        self,
        requested_keys: Iterable[RelationshipKey],
        *,
        side: str = "local",
    ) -> CorrespondenceIndex:
        """Validate candidates for requested relationship keys present locally.

        A relationship absent from this index does not activate endpoint or
        origin ambiguity.  Every candidate node for a present key is checked,
        including edgeless duplicate candidates.
        """
        if side not in {"local", "old", "new", "left", "right"}:
            raise ValueError("side must be local, old, new, left, or right")
        for relationship_key in sorted(set(requested_keys), key=_key_sort):
            occurrences = self.relationships_by_key.get(relationship_key)
            if not occurrences:
                continue
            for endpoint_name, key in (
                ("source", relationship_key[0]),
                ("target", relationship_key[1]),
            ):
                candidates = self.nodes_by_key.get(key, ())
                # Multiple unresolved occurrences with one complete ordinary
                # origin are legitimate observations of one semantic endpoint.
                # Endpoint uniqueness applies to ordinary candidates; for an
                # unresolved endpoint, the origin candidate group below is the
                # ambiguity boundary.
                if (
                    candidates
                    and candidates[0].node_class != NodeClass.UNRESOLVED_REFERENCE
                    and len(candidates) > 1
                ):
                    raise CorrespondenceAmbiguityError(
                        relationship_key,
                        side=side,
                        endpoint=endpoint_name,
                        candidates=candidates,
                    )
                for occurrence in occurrences:
                    endpoint_node = (
                        occurrence.source if endpoint_name == "source" else occurrence.target
                    )
                    if endpoint_node.node_class != NodeClass.UNRESOLVED_REFERENCE:
                        continue
                    origin_key = self.origin_dependencies.get(key)
                    if origin_key is None:
                        continue
                    origin_candidates = self.nodes_by_key.get(origin_key, ())
                    if len(origin_candidates) > 1:
                        raise CorrespondenceAmbiguityError(
                            relationship_key,
                            side=side,
                            endpoint=endpoint_name,
                            candidates=origin_candidates,
                            origin=True,
                        )
        return self


def _check_side(side: str) -> None:
    if side not in {"local", "old", "new", "left", "right"}:
        raise ValueError("side must be local, old, new, left, or right")


def _relationship_sort_key(occurrence: RelationshipOccurrence) -> tuple[object, ...]:
    relationship = occurrence.relationship
    evidence = tuple(
        (
            repr(item.attribution_key),
            tuple(
                (
                    location.path,
                    location.range.start.line,
                    location.range.start.character,
                    location.range.end.line,
                    location.range.end.character,
                )
                for location in item.locations
            ),
        )
        for item in relationship.evidence
    )
    return (
        relationship.source,
        relationship.target,
        relationship.kind,
        repr(evidence),
        repr(relationship.extensions),
    )


def prepare_correspondence(
    document: GraphDocument,
    *,
    side: str = "local",
) -> CorrespondenceIndex:
    """Validate and prepare one graph document for semantic correspondence.

    Admission always calls ``validate_document(document,
    verify_node_ids=True)`` without source text.  Thus the returned index is
    usable only after the complete ordered validator report is valid.
    """
    _check_side(side)
    report = validate_document(document, verify_node_ids=True)
    if not report.is_valid:
        raise CorrespondenceAdmissionError(report, document, side=side)

    by_id = {node.id: node for node in document.nodes}
    ordinary_keys: dict[str, NodeKey] = {}
    groups: dict[NodeKey, list[Node]] = defaultdict(list)
    for node in document.nodes:
        if node.node_class != NodeClass.UNRESOLVED_REFERENCE:
            key = node_key(node)
            ordinary_keys[node.id] = key
            groups[key].append(node)

    origin_dependencies: dict[NodeKey, NodeKey] = {}
    unresolved_keys: dict[str, NodeKey] = {}
    eligibility_errors: list[tuple[Relationship, str, Node, Node]] = []
    candidate_ids: dict[NodeKey, set[str]] = defaultdict(set)

    def add_candidate(key: NodeKey, node: Node) -> None:
        if node.id not in candidate_ids[key]:
            candidate_ids[key].add(node.id)
            groups[key].append(node)

    for key, nodes in tuple(groups.items()):
        for node in nodes:
            candidate_ids[key].add(node.id)

    for relationship in document.relationships:
        if relationship.kind not in _SUPPORTED_RELATIONSHIPS:
            continue
        for endpoint_name, endpoint_id in (
            ("source", relationship.source),
            ("target", relationship.target),
        ):
            endpoint = by_id[endpoint_id]
            if endpoint.node_class != NodeClass.UNRESOLVED_REFERENCE:
                continue
            origin_id = endpoint.identity.originating_node
            assert origin_id is not None  # structural model + admission guarantee this
            origin = by_id[origin_id]
            if origin.node_class == NodeClass.UNRESOLVED_REFERENCE:
                eligibility_errors.append((relationship, endpoint_name, endpoint, origin))
                continue
            origin_key = ordinary_keys[origin.id]
            key = node_key(endpoint, origin=origin_key)
            unresolved_keys[endpoint.id] = key
            origin_dependencies[key] = origin_key
            add_candidate(key, endpoint)

    if eligibility_errors:
        eligibility_errors.sort(
            key=lambda item: (
                item[0].source,
                item[0].target,
                item[0].kind,
                0 if item[1] == "source" else 1,
                item[2].id,
                item[3].id,
            )
        )
        relationship, endpoint_name, endpoint, origin = eligibility_errors[0]
        raise CorrespondenceEligibilityError(
            relationship,
            endpoint_name,
            endpoint,
            origin,
            side=side,
        )

    immutable_groups = MappingProxyType(
        {
            key: tuple(sorted(nodes, key=_node_sort))
            for key, nodes in sorted(groups.items(), key=lambda item: _key_sort(item[0]))
        }
    )
    immutable_ids = MappingProxyType({node_id: by_id[node_id] for node_id in sorted(by_id)})

    relationship_groups: dict[RelationshipKey, list[RelationshipOccurrence]] = defaultdict(list)
    for relationship in document.relationships:
        if relationship.kind not in _SUPPORTED_RELATIONSHIPS:
            continue
        source = by_id[relationship.source]
        target = by_id[relationship.target]
        source_key = ordinary_keys.get(source.id) or unresolved_keys.get(source.id)
        target_key = ordinary_keys.get(target.id) or unresolved_keys.get(target.id)
        if source_key is None or target_key is None:
            # Unsupported relationships involving an unrelated orphan
            # unresolved node are retained in the document but do not enter
            # semantic comparison groups.
            continue
        occurrence = RelationshipOccurrence(relationship, source, target, source_key, target_key)
        relationship_groups[occurrence.key].append(occurrence)
    immutable_relationships = MappingProxyType(
        {
            key: tuple(sorted(items, key=_relationship_sort_key))
            for key, items in sorted(
                relationship_groups.items(), key=lambda item: _key_sort(item[0])
            )
        }
    )
    return CorrespondenceIndex(
        document=document,
        nodes_by_id=immutable_ids,
        nodes_by_key=immutable_groups,
        relationships_by_key=immutable_relationships,
        origin_dependencies=MappingProxyType(
            {key: origin_dependencies[key] for key in sorted(origin_dependencies, key=_key_sort)}
        ),
    )


def validate_required_keys(
    index: CorrespondenceIndex,
    requested_keys: Iterable[RelationshipKey],
    *,
    side: str = "local",
) -> CorrespondenceIndex:
    """Validate requested local relationship keys on a prepared index."""
    return index.validate_required_keys(requested_keys, side=side)


__all__ = [
    "CorrespondenceAdmissionError",
    "CorrespondenceAmbiguityError",
    "CorrespondenceEligibilityError",
    "CorrespondenceError",
    "CorrespondenceIndex",
    "NodeKey",
    "RelationshipKey",
    "RelationshipOccurrence",
    "node_key",
    "prepare_correspondence",
    "validate_required_keys",
]
