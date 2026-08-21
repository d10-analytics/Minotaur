"""Semantic comparison of two Minotaur graph snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import NodeClass
from minotaur.query.render import dump_json

# The first item makes keys from different node classes unambiguous.  Symbol
# keys intentionally contain exactly the two fields promised by R-09; the
# class marker is an implementation detail used to avoid collisions with
# files and unresolved references.
NodeKey = tuple[str, ...]
RelationshipKey = tuple[NodeKey, NodeKey, str]


@dataclass(frozen=True, slots=True)
class SymbolChange:
    """A symbol added to or removed from a snapshot."""

    kind: str
    symbol: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "symbol": self.symbol}


@dataclass(frozen=True, slots=True)
class Relocation:
    """A symbol that retained its semantic key but moved in source."""

    kind: str
    symbol: str
    old_location: Location | None
    new_location: Location | None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "from": self.old_location.to_dict() if self.old_location is not None else None,
            "to": self.new_location.to_dict() if self.new_location is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RelationshipChange:
    """A relationship addition or removal using semantic endpoint labels."""

    kind: str
    source: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "source": self.source, "target": self.target}


@dataclass(frozen=True, slots=True)
class DiffResult:
    """All changes between two graph snapshots in stable output groups."""

    added: tuple[SymbolChange, ...] = ()
    removed: tuple[SymbolChange, ...] = ()
    relocated: tuple[Relocation, ...] = ()
    relationships_added: tuple[RelationshipChange, ...] = ()
    relationships_removed: tuple[RelationshipChange, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "query": "diff",
            "added": [item.to_dict() for item in self.added],
            "removed": [item.to_dict() for item in self.removed],
            "relocated": [item.to_dict() for item in self.relocated],
            "relationships_added": [item.to_dict() for item in self.relationships_added],
            "relationships_removed": [item.to_dict() for item in self.relationships_removed],
        }


def diff(old: GraphDocument, new: GraphDocument) -> DiffResult:
    """Compare symbols and relationships without relying on opaque node IDs."""

    old_symbols = _keyed_symbols(old)
    new_symbols = _keyed_symbols(new)
    old_keys = set(old_symbols)
    new_keys = set(new_symbols)

    added = tuple(_symbol_change(new_symbols[key]) for key in sorted(new_keys - old_keys))
    removed = tuple(_symbol_change(old_symbols[key]) for key in sorted(old_keys - new_keys))
    relocated = tuple(
        _relocation(old_symbols[key], new_symbols[key])
        for key in sorted(old_keys & new_keys)
        if old_symbols[key].location != new_symbols[key].location
    )

    old_relationships = _keyed_relationships(old)
    new_relationships = _keyed_relationships(new)
    added_relationships = tuple(
        new_relationships[key] for key in sorted(set(new_relationships) - set(old_relationships))
    )
    removed_relationships = tuple(
        old_relationships[key] for key in sorted(set(old_relationships) - set(new_relationships))
    )
    return DiffResult(
        added=added,
        removed=removed,
        relocated=relocated,
        relationships_added=added_relationships,
        relationships_removed=removed_relationships,
    )


def render_text(result: DiffResult) -> str:
    """Render changes grouped as additions, removals, relocations, and edges."""

    lines: list[str] = []
    lines.extend(f"+ {item.symbol}\n" for item in result.added)
    lines.extend(f"- {item.symbol}\n" for item in result.removed)
    lines.extend(f"~ {item.symbol} (relocated)\n" for item in result.relocated)
    lines.extend(
        f"+ {item.kind} {item.source} → {item.target}\n" for item in result.relationships_added
    )
    lines.extend(
        f"- {item.kind} {item.source} → {item.target}\n" for item in result.relationships_removed
    )
    return "".join(lines) if lines else "no changes\n"


def render_json(result: DiffResult) -> str:
    """Render the same semantic records used by text output."""

    return dump_json(result.to_dict())


def _keyed_symbols(document: GraphDocument) -> dict[NodeKey, Node]:
    result: dict[NodeKey, Node] = {}
    for node in document.nodes:
        # Module nodes and contains edges describe graph scaffolding rather
        # than declarations an agent can act on.  Their source ranges shift
        # when a line is inserted, which would otherwise drown out the
        # function/class changes in a useful diff.
        if node.node_class == NodeClass.SYMBOL and node.symbol_kind != "module":
            key = ("symbol", node.symbol_kind or "unknown", node.label)
        else:
            continue
        # Duplicate symbols are legal and definitions() reports them.  A diff
        # still needs one deterministic representative for the shared key.
        current = result.get(key)
        if current is None or _node_sort_key(node) < _node_sort_key(current):
            result[key] = node
    return result


def _node_keys(document: GraphDocument) -> dict[str, NodeKey]:
    result: dict[str, NodeKey] = {}
    for node in document.nodes:
        key: NodeKey
        if node.node_class == NodeClass.SYMBOL:
            key = ("symbol", node.symbol_kind or "unknown", node.label)
        elif node.node_class == NodeClass.UNRESOLVED_REFERENCE:
            origin = node.identity.originating_node
            origin_label = _node_label(document, origin) if origin is not None else ""
            key = ("unresolved", origin_label, node.reference_text or node.label)
        elif node.node_class == NodeClass.FILE:
            key = ("file", node.path or node.label)
        else:
            key = (node.node_class.value, node.label)
        result[node.id] = key
    return result


def _keyed_relationships(document: GraphDocument) -> dict[RelationshipKey, RelationshipChange]:
    endpoint_keys = _node_keys(document)
    result: dict[RelationshipKey, RelationshipChange] = {}
    for relationship in document.relationships:
        if relationship.kind == "contains":
            continue
        source_key = endpoint_keys.get(relationship.source, ("node", relationship.source))
        target_key = endpoint_keys.get(relationship.target, ("node", relationship.target))
        key = (source_key, target_key, relationship.kind)
        result.setdefault(
            key,
            RelationshipChange(
                kind=relationship.kind,
                source=_display_key(source_key),
                target=_display_key(target_key),
            ),
        )
    return result


def _node_label(document: GraphDocument, node_id: str | None) -> str:
    if node_id is None:
        return ""
    node = document.node_by_id(node_id)
    return node.label if node is not None else node_id


def _display_key(key: NodeKey) -> str:
    if key[0] == "symbol":
        return key[2]
    if key[0] == "unresolved":
        return key[2]
    return key[-1]


def _node_sort_key(node: Node) -> tuple[object, ...]:
    location = node.location
    return (
        location.sort_key if location is not None else ("", -1, -1, -1, -1),
        node.id,
    )


def _symbol_change(node: Node) -> SymbolChange:
    return SymbolChange(kind=node.symbol_kind or "unknown", symbol=node.label)


def _relocation(old: Node, new: Node) -> Relocation:
    return Relocation(
        kind=new.symbol_kind or old.symbol_kind or "unknown",
        symbol=new.label,
        old_location=old.location,
        new_location=new.location,
    )
