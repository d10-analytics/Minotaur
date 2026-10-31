"""Semantic comparison of two Minotaur graph snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import NodeClass
from minotaur.query.correspondence import declaration_role
from minotaur.query.render import dump_json

# The first item makes keys from different node classes unambiguous and avoids
# collisions between symbols, files, and unresolved references.
NodeKey = tuple[str, ...]
# Symbols are keyed by (kind, label, role, ordinal).  The role is "" when none
# is recorded so keys with and without a role sort together; the ordinal counts
# same-role definitions of one label in source order.
SymbolKey = tuple[str, str, str, int]
RelationshipKey = tuple[NodeKey, NodeKey, str]


@dataclass(frozen=True, slots=True)
class SymbolChange:
    """A symbol added to or removed from a snapshot."""

    kind: str
    symbol: str
    role: str | None = None

    def to_dict(self) -> dict[str, str]:
        record = {"kind": self.kind, "symbol": self.symbol}
        if self.role is not None:
            record["role"] = self.role
        return record


@dataclass(frozen=True, slots=True)
class Relocation:
    """A symbol that retained its semantic key but moved in source."""

    kind: str
    symbol: str
    old_location: Location | None
    new_location: Location | None
    role: str | None = None

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "from": _start_record(self.old_location),
            "kind": self.kind,
            "symbol": self.symbol,
            "to": _start_record(self.new_location),
        }
        if self.role is not None:
            record["role"] = self.role
        return record


@dataclass(frozen=True, slots=True)
class RelationshipChange:
    """A relationship addition or removal using semantic endpoint labels."""

    kind: str
    source: str
    target: str
    unresolved: bool = False

    def to_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
        }
        if self.unresolved:
            record["unresolved"] = True
        return record


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
        if _start(old_symbols[key].location) != _start(new_symbols[key].location)
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
    lines.extend(f"+ {_symbol_text(item.symbol, item.role)}\n" for item in result.added)
    lines.extend(f"- {_symbol_text(item.symbol, item.role)}\n" for item in result.removed)
    lines.extend(_relocation_text(item) for item in result.relocated)
    lines.extend(f"+ {_relationship_text(item)}\n" for item in result.relationships_added)
    lines.extend(f"- {_relationship_text(item)}\n" for item in result.relationships_removed)
    return "".join(lines) if lines else "no changes\n"


def _symbol_text(symbol: str, role: str | None) -> str:
    return symbol if role is None else f"{symbol} ({role})"


def _relationship_text(item: RelationshipChange) -> str:
    suffix = " [unresolved]" if item.unresolved else ""
    return f"{item.kind} {item.source} → {item.target}{suffix}"


def _start(location: Location | None) -> tuple[str, int] | None:
    if location is None:
        return None
    return (location.path, location.range.start.line)


def _start_record(location: Location | None) -> dict[str, object] | None:
    if location is None:
        return None
    return {
        "path": location.path,
        "line": location.range.start.line + 1,
        "column": location.range.start.character + 1,
    }


def _relocation_text(item: Relocation) -> str:
    # Text output is what agents read by default; the from/to line move is
    # otherwise only visible in JSON. Falls back to the bare "(relocated)"
    # form when either endpoint has no location -- e.g. a symbol whose
    # location became unknown across snapshots -- rather than guessing.
    old_location = item.old_location
    new_location = item.new_location
    if old_location is None or new_location is None:
        return f"~ {_symbol_text(item.symbol, item.role)} (relocated)\n"
    old_line = old_location.range.start.line + 1
    new_line = new_location.range.start.line + 1
    if old_location.path == new_location.path:
        move = f"{old_location.path}:{old_line}→{new_line}"
    else:
        move = f"{old_location.path}:{old_line}→{new_location.path}:{new_line}"
    return f"~ {_symbol_text(item.symbol, item.role)} (relocated {move})\n"


def render_json(result: DiffResult) -> str:
    """Render the same semantic records used by text output."""

    return dump_json(result.to_dict())


def _keyed_symbols(document: GraphDocument) -> dict[SymbolKey, Node]:
    # Module nodes and contains edges describe graph scaffolding rather
    # than declarations an agent can act on.  Their source ranges shift
    # when a line is inserted, which would otherwise drown out the
    # function/class changes in a useful diff.  impact.py deliberately
    # makes the opposite call: it keeps module symbols as real inbound
    # dependants, because a module-scope caller is a genuine impact
    # target even though its location is unstable across edits.
    groups: dict[tuple[str, str, str], list[Node]] = {}
    for node in document.nodes:
        if node.node_class == NodeClass.SYMBOL and node.symbol_kind != "module":
            group = (node.symbol_kind or "unknown", node.label, declaration_role(node) or "")
            groups.setdefault(group, []).append(node)
    # Duplicate symbols are legal and definitions() reports them.  Each
    # same-role duplicate gets its source-order position as an ordinal so a
    # removed duplicate is a removal rather than a collapsed no-op.
    result: dict[SymbolKey, Node] = {}
    for (kind, label, role), nodes in groups.items():
        for ordinal, node in enumerate(sorted(nodes, key=_node_sort_key)):
            result[(kind, label, role, ordinal)] = node
    return result


def _node_keys(document: GraphDocument) -> tuple[dict[str, NodeKey], frozenset[str]]:
    labels: dict[str, str] = {}
    for node in document.nodes:
        labels.setdefault(node.id, node.label)
    result: dict[str, NodeKey] = {}
    unresolved: set[str] = set()
    for node in document.nodes:
        key: NodeKey
        if node.node_class == NodeClass.SYMBOL:
            key = ("symbol", node.symbol_kind or "unknown", node.label)
        elif node.node_class == NodeClass.UNRESOLVED_REFERENCE:
            origin = node.identity.originating_node
            origin_label = _node_label(labels, origin) if origin is not None else ""
            key = ("unresolved", origin_label, node.reference_text or node.label)
            unresolved.add(node.id)
        elif node.node_class == NodeClass.FILE:
            key = ("file", node.path or node.label)
        else:
            key = (node.node_class.value, node.label)
        result[node.id] = key
    return result, frozenset(unresolved)


def _keyed_relationships(document: GraphDocument) -> dict[RelationshipKey, RelationshipChange]:
    endpoint_keys, unresolved_ids = _node_keys(document)
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
                unresolved=relationship.target in unresolved_ids,
            ),
        )
    return result


def _node_label(labels: dict[str, str], node_id: str | None) -> str:
    if node_id is None:
        return ""
    return labels.get(node_id, node_id)


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
    return SymbolChange(
        kind=node.symbol_kind or "unknown", symbol=node.label, role=declaration_role(node)
    )


def _relocation(old: Node, new: Node) -> Relocation:
    return Relocation(
        kind=new.symbol_kind or old.symbol_kind or "unknown",
        symbol=new.label,
        old_location=old.location,
        new_location=new.location,
        role=declaration_role(new),
    )
