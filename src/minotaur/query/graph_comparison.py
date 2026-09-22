"""Immutable whole-graph classifications for system comparison.

The ordinary query correspondence is intentionally boundary-oriented.  This
module consumes the explicit whole-graph correspondence mode and compares
every admitted node and relationship, including internal and edgeless graph
content.  Evidence locations and opaque source IDs are retained in the
side-local payload but are not structural identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.node import Node
from minotaur.query.correspondence import (
    CorrespondenceIndex,
    NodeKey,
    RelationshipKey,
    RelationshipOccurrence,
    prepare_whole_graph,
)
from minotaur.system import EndpointKind, classify_endpoint

if TYPE_CHECKING:
    from minotaur.query.system import ReportingSnapshot


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw(item) for item in sorted(value, key=repr)]
    return value


def _key_json(key: tuple[object, ...]) -> str:
    return json.dumps(_thaw(key), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def node_display_id(key: NodeKey) -> str:
    """Return a deterministic public ID for a semantic node key."""
    digest = hashlib.sha256(f"node\0{_key_json(key)}".encode()).hexdigest()
    return f"node:comparison:{digest}"


def relationship_display_id(key: RelationshipKey) -> str:
    """Return a deterministic public ID for a semantic relationship key."""
    digest = hashlib.sha256(f"relationship\0{_key_json(key)}".encode()).hexdigest()
    return f"relationship:comparison:{digest}"


def _node_shape(node: Node) -> Mapping[str, object]:
    """Keep semantic observations while excluding IDs, ranges, and metadata."""
    payload = node.to_dict()
    return {
        "node_class": payload.get("node_class"),
        "label": payload.get("label"),
        "symbol_kind": payload.get("symbol_kind"),
        "reference_text": payload.get("reference_text"),
    }


def _node_payload(node: Node) -> Mapping[str, object]:
    return node.to_dict()


def _group_payload(values: Iterable[object]) -> object:
    rendered = tuple(sorted((_freeze(value) for value in values), key=repr))
    return rendered[0] if len(rendered) == 1 else rendered


def _category(snapshot: ReportingSnapshot | None, node: Node) -> str | None:
    if snapshot is None:
        return None
    membership = classify_endpoint(snapshot.systems, node)
    if membership.kind is EndpointKind.SYSTEM and membership.system is not None:
        return membership.system.name
    return None


def _node_side(nodes: Sequence[Node], snapshot: ReportingSnapshot | None) -> object:
    """Return the stored side value for one matched node group.

    C-05 stores the original canonical node together with the declared system
    name on that revision, so the viewer never has to compare memberships
    across revisions to decide same-side eligibility.
    """
    if not nodes:
        return None
    records = tuple(
        _freeze({"node": _node_payload(item), "system": _category(snapshot, item)})
        for item in nodes
    )
    return records[0] if len(records) == 1 else records


def endpoint_eligibility(
    before_pairs: Sequence[tuple[str | None, str | None]],
    after_pairs: Sequence[tuple[str | None, str | None]],
) -> Mapping[str, object]:
    """Precompute per-side endpoint membership and internal/boundary systems.

    Each pair is ``(source_system, target_system)`` for one relationship
    occurrence on that revision. ``internal_systems`` names every system whose
    membership covered both endpoints on the *same* side; combining memberships
    across revisions would invent an internal relationship that never existed.
    """
    before_sources = sorted({source for source, _ in before_pairs if source})
    before_targets = sorted({target for _, target in before_pairs if target})
    after_sources = sorted({source for source, _ in after_pairs if source})
    after_targets = sorted({target for _, target in after_pairs if target})
    internal = sorted(
        (set(before_sources) & set(before_targets)) | (set(after_sources) & set(after_targets))
    )
    touching = set(before_sources) | set(before_targets) | set(after_sources) | set(after_targets)
    payload = {
        "before": {
            "source_systems": before_sources,
            "target_systems": before_targets,
        },
        "after": {
            "source_systems": after_sources,
            "target_systems": after_targets,
        },
        "internal_systems": internal,
        "boundary_systems": sorted(touching - set(internal)),
    }
    return cast(Mapping[str, object], _freeze(payload))


def _eligibility(
    old: Sequence[RelationshipOccurrence],
    new: Sequence[RelationshipOccurrence],
    old_snapshot: ReportingSnapshot | None,
    new_snapshot: ReportingSnapshot | None,
) -> Mapping[str, object]:
    """Precompute eligibility from matched occurrence endpoints and snapshots."""
    return endpoint_eligibility(
        tuple(
            (_category(old_snapshot, item.source), _category(old_snapshot, item.target))
            for item in old
        ),
        tuple(
            (_category(new_snapshot, item.source), _category(new_snapshot, item.target))
            for item in new
        ),
    )


def _involved(
    old_snapshot: ReportingSnapshot | None,
    new_snapshot: ReportingSnapshot | None,
    old_nodes: Sequence[Node],
    new_nodes: Sequence[Node],
) -> tuple[str, ...]:
    names = {
        name
        for snapshot, nodes in (
            (old_snapshot, old_nodes),
            (new_snapshot, new_nodes),
        )
        if snapshot is not None
        for node in nodes
        if (name := _category(snapshot, node)) is not None
    }
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class GraphNodeChange:
    """One semantic node classification, including unchanged nodes."""

    id: str
    status: str
    reasons: tuple[str, ...] = ()
    involved_systems: tuple[str, ...] = ()
    before: object = None
    after: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(sorted(set(self.reasons))))
        object.__setattr__(self, "involved_systems", tuple(sorted(set(self.involved_systems))))
        object.__setattr__(self, "before", _freeze(self.before))
        object.__setattr__(self, "after", _freeze(self.after))

    @property
    def key(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "reasons": list(self.reasons),
            "involved_systems": list(self.involved_systems),
            "before": _thaw(self.before),
            "after": _thaw(self.after),
        }


@dataclass(frozen=True, slots=True)
class GraphRelationshipChange:
    """One semantic relationship classification, including internal edges."""

    id: str
    source: str
    target: str
    kind: str
    status: str
    reasons: tuple[str, ...] = ()
    involved_systems: tuple[str, ...] = ()
    before: object = None
    after: object = None
    eligibility: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(sorted(set(self.reasons))))
        object.__setattr__(self, "involved_systems", tuple(sorted(set(self.involved_systems))))
        object.__setattr__(self, "before", _freeze(self.before))
        object.__setattr__(self, "after", _freeze(self.after))
        object.__setattr__(self, "eligibility", _freeze(self.eligibility))

    @property
    def key(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "status": self.status,
            "reasons": list(self.reasons),
            "involved_systems": list(self.involved_systems),
            "before": _thaw(self.before),
            "after": _thaw(self.after),
            "eligibility": _thaw(self.eligibility),
        }


@dataclass(frozen=True, slots=True)
class GraphComparison:
    """Complete immutable node and relationship classifications."""

    nodes: tuple[GraphNodeChange, ...] = ()
    relationships: tuple[GraphRelationshipChange, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "relationships", tuple(self.relationships))

    @property
    def changed(self) -> bool:
        return any(item.status != "unchanged" for item in self.nodes) or any(
            item.status != "unchanged" for item in self.relationships
        )

    @property
    def has_changes(self) -> bool:
        return self.changed

    def to_dict(self) -> dict[str, object]:
        return {
            "nodes": [item.to_dict() for item in self.nodes],
            "relationships": [item.to_dict() for item in self.relationships],
        }


def _coerce_index(value: object, *, side: str) -> CorrespondenceIndex:
    if isinstance(value, CorrespondenceIndex):
        if not value.whole_graph:
            return prepare_whole_graph(value.document, side=side)
        value.validate_whole_graph(side=side)
        return value
    document = getattr(value, "document", value)
    if not isinstance(document, GraphDocument):
        raise TypeError("whole-graph comparison requires GraphDocument or ReportingSnapshot")
    index = prepare_whole_graph(document, side=side)
    index.validate_whole_graph(side=side)
    return index


def _comparison_inputs(
    value: object, *, side: str
) -> tuple[CorrespondenceIndex, ReportingSnapshot | None]:
    index = _coerce_index(value, side=side)
    snapshot = value if hasattr(value, "systems") and hasattr(value, "document") else None
    return index, snapshot  # type: ignore[return-value]


def _node_changes(
    old: CorrespondenceIndex,
    new: CorrespondenceIndex,
    old_snapshot: ReportingSnapshot | None,
    new_snapshot: ReportingSnapshot | None,
) -> tuple[GraphNodeChange, ...]:
    result: list[GraphNodeChange] = []
    for key in sorted(set(old.nodes_by_key) | set(new.nodes_by_key), key=repr):
        left = old.nodes_by_key.get(key, ())
        right = new.nodes_by_key.get(key, ())
        before = _node_side(left, old_snapshot)
        after = _node_side(right, new_snapshot)
        reasons: list[str] = []
        if not left:
            status = "added"
            reasons.append("added")
        elif not right:
            status = "removed"
            reasons.append("removed")
        else:
            status = "unchanged"
            if len(left) != len(right):
                status = "changed"
                reasons.append("multiplicity_changed")
            if sorted((_node_shape(item) for item in left), key=repr) != sorted(
                (_node_shape(item) for item in right), key=repr
            ):
                status = "changed"
                reasons.append("changed")
            old_membership = {_category(old_snapshot, item) for item in left}
            new_membership = {_category(new_snapshot, item) for item in right}
            if old_membership != new_membership:
                status = "changed"
                reasons.append("membership_changed")
        result.append(
            GraphNodeChange(
                id=node_display_id(key),
                status=status,
                reasons=tuple(reasons),
                involved_systems=_involved(old_snapshot, new_snapshot, left, right),
                before=before,
                after=after,
            )
        )
    return tuple(sorted(result, key=lambda item: item.id))


def _relationship_payload(item: RelationshipOccurrence) -> Mapping[str, object]:
    payload = item.relationship.to_dict()
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        payload = dict(payload)
        payload["evidence"] = sorted(evidence, key=repr)
    return payload


def _relationship_changes(
    old: CorrespondenceIndex,
    new: CorrespondenceIndex,
    old_snapshot: ReportingSnapshot | None,
    new_snapshot: ReportingSnapshot | None,
) -> tuple[GraphRelationshipChange, ...]:
    result: list[GraphRelationshipChange] = []
    for key in sorted(set(old.relationships_by_key) | set(new.relationships_by_key), key=repr):
        left = old.relationships_by_key.get(key, ())
        right = new.relationships_by_key.get(key, ())
        before = _group_payload(_relationship_payload(item) for item in left) if left else None
        after = _group_payload(_relationship_payload(item) for item in right) if right else None
        reasons: list[str] = []
        if not left:
            status = "added"
            reasons.append("added")
        elif not right:
            status = "removed"
            reasons.append("removed")
        else:
            status = "unchanged"
            if len(left) != len(right):
                status = "changed"
                reasons.append("multiplicity_changed")
            old_membership = {
                (_category(old_snapshot, item.source), _category(old_snapshot, item.target))
                for item in left
            }
            new_membership = {
                (_category(new_snapshot, item.source), _category(new_snapshot, item.target))
                for item in right
            }
            if old_membership != new_membership:
                status = "changed"
                reasons.append("membership_changed")
        source_key, target_key, kind = key
        result.append(
            GraphRelationshipChange(
                id=relationship_display_id(key),
                source=node_display_id(source_key),
                target=node_display_id(target_key),
                kind=kind,
                status=status,
                reasons=tuple(reasons),
                involved_systems=_involved(
                    old_snapshot,
                    new_snapshot,
                    tuple(item.source for item in left),
                    tuple(item.source for item in right),
                )
                + tuple(
                    sorted(
                        set(
                            name
                            for snapshot, items in (
                                (old_snapshot, left),
                                (new_snapshot, right),
                            )
                            if snapshot is not None
                            for item in items
                            for endpoint in (item.target,)
                            if (name := _category(snapshot, endpoint)) is not None
                        )
                        - set(
                            _involved(
                                old_snapshot,
                                new_snapshot,
                                tuple(item.source for item in left),
                                tuple(item.source for item in right),
                            )
                        )
                    )
                ),
                before=before,
                after=after,
                eligibility=_eligibility(left, right, old_snapshot, new_snapshot),
            )
        )
    return tuple(sorted(result, key=lambda item: item.id))


def compare_graphs(old: object, new: object) -> GraphComparison:
    """Compare every node and relationship using explicit whole-graph mode."""
    old_index, old_snapshot = _comparison_inputs(old, side="old")
    new_index, new_snapshot = _comparison_inputs(new, side="new")
    return GraphComparison(
        nodes=_node_changes(old_index, new_index, old_snapshot, new_snapshot),
        relationships=_relationship_changes(old_index, new_index, old_snapshot, new_snapshot),
    )


compare_whole_graph = compare_graphs
compare_graph = compare_graphs
GraphNodeClassification = GraphNodeChange
GraphRelationshipClassification = GraphRelationshipChange

__all__ = [
    "GraphComparison",
    "GraphNodeChange",
    "GraphNodeClassification",
    "GraphRelationshipChange",
    "GraphRelationshipClassification",
    "compare_graph",
    "compare_graphs",
    "compare_whole_graph",
    "endpoint_eligibility",
    "node_display_id",
    "relationship_display_id",
]
