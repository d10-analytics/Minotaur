"""Relationship model for Minotaur graph documents.

Relationships are the structural claims in a Minotaur graph: "node A calls
node B", "module X imports module Y", "class C inherits from class D." Each
relationship is uniquely identified by its (source, target, kind) tuple —
there is exactly one structural relationship for a given combination.

This one-relationship-per-tuple rule exists because graph rendering and
edge counts must be unambiguous. If a function calls another function three
times from different locations, that's ONE "calls" relationship with three
source locations on its evidence, not three separate edges. The visualizer
draws one connecting line and lets the user inspect the individual call
sites through the relationship detail popup.

Relationships have no serialized ID. Their identity IS the (source, target,
kind) tuple. A consumer that needs an implementation-specific edge identifier
(e.g. an HTML element ID for the graph renderer) derives it deterministically
from that tuple. This avoids a second canonical identity that could drift
from the structural one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from minotaur.graph_model._parsing import (
    freeze_extensions,
    reject_unknown_fields,
    serialize_extensions,
    type_error,
)
from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.identity import is_valid_node_id_format
from minotaur.graph_model.location import Location
from minotaur.graph_model.provenance import resolve_relationship_kind

# Module-level constant for reject_unknown_fields (F-13).
_RELATIONSHIP_FIELDS = frozenset({"source", "target", "kind", "evidence", "extensions"})


@dataclass(frozen=True, slots=True)
class Relationship:
    """A single structural relationship between two nodes.

    source and target are node ID strings ("node:sha256:..."), not Node
    objects. This avoids circular references between nodes and relationships
    and matches the wire format, where relationships reference node IDs.
    Endpoint integrity (do these IDs actually exist in the document?) is
    checked by the semantic validator, not by the relationship model.

    The kind field stores the raw string (core or namespaced extension)
    rather than the RelationshipKind enum, for the same reason as
    Node.symbol_kind — extension values pass through without needing
    an enum member.
    """

    source: str
    target: str
    kind: str
    # Evidence is stored as a tuple for frozen-dataclass compatibility.
    # The schema requires at least one evidence record (minItems: 1),
    # enforced at construction.
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)
    extensions: Mapping[str, Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        # Field types first (R-02): in-process construction and
        # dataclasses.replace() bypass from_dict's wire type checks.
        if not isinstance(self.source, str):
            raise type_error("relationship 'source'", self.source, "a string")
        if not isinstance(self.target, str):
            raise type_error("relationship 'target'", self.target, "a string")
        if not isinstance(self.kind, str):
            raise type_error("relationship 'kind'", self.kind, "a string")
        if not isinstance(self.evidence, tuple):
            raise type_error("relationship 'evidence'", self.evidence, "a tuple")
        for index, record in enumerate(self.evidence):
            if not isinstance(record, Evidence):
                raise type_error(f"relationship 'evidence'[{index}]", record, "an Evidence")

        if not self.source:
            raise ValueError("relationship 'source' must be non-empty")
        if not self.target:
            raise ValueError("relationship 'target' must be non-empty")
        if not is_valid_node_id_format(self.source):
            raise ValueError("relationship 'source' must be a valid node ID")
        if not is_valid_node_id_format(self.target):
            raise ValueError("relationship 'target' must be a valid node ID")

        # Validate the kind is either a core relationship kind or a valid
        # namespaced extension. Invalid kinds (arbitrary unqualified strings)
        # are caught here rather than at rendering time.
        resolve_relationship_kind(self.kind)

        # At least one evidence record is required. A relationship without
        # evidence is an unsupported assertion — Minotaur's contract is
        # that every structural claim is traceable.
        if not self.evidence:
            raise ValueError("relationship requires at least one evidence record")
        object.__setattr__(self, "extensions", freeze_extensions(self.extensions))

    @property
    def tuple_key(self) -> tuple[str, str, str]:
        """The canonical identity tuple for this relationship.

        Used for duplicate detection, canonical ordering, and as the
        basis for derived edge identifiers in renderers. The tuple
        is (source, target, kind) — the same order the schema uses
        for structural uniqueness.
        """
        return (self.source, self.target, self.kind)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "evidence": [ev.to_dict() for ev in self.evidence],
        }
        if self.extensions is not None:
            result["extensions"] = serialize_extensions(self.extensions)
        return result

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        *,
        memo: dict[tuple[str, int, int, int, int], Location] | None = None,
    ) -> Relationship:
        reject_unknown_fields(data, _RELATIONSHIP_FIELDS, "relationship")
        source = data.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError("relationship requires a non-empty 'source' string")

        target = data.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError("relationship requires a non-empty 'target' string")

        kind = data.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("relationship requires a non-empty 'kind' string")

        evidence_data = data.get("evidence")
        if not isinstance(evidence_data, list):
            raise ValueError("relationship requires an 'evidence' array")
        if not evidence_data:
            raise ValueError("relationship 'evidence' array must be non-empty")
        evidence = tuple(Evidence.from_dict(ev, memo=memo) for ev in evidence_data)

        extensions = data.get("extensions")
        if extensions is not None and not isinstance(extensions, dict):
            raise ValueError("'extensions' must be an object when present")

        return cls(
            source=source,
            target=target,
            kind=kind,
            evidence=evidence,
            extensions=extensions,
        )
