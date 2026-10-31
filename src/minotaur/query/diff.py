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


def diff(
    old: GraphDocument, new: GraphDocument, error_paths: frozenset[str] = frozenset()
) -> DiffResult:
    """Compare symbols and relationships without relying on opaque node IDs.

    ``error_paths`` names files whose current analysis reported an error.  A
    missing fact there could fabricate a row, so rows judged from old-side
    locations in those files are withheld (see ``_compare_affected``).
    """

    old_groups = _grouped_symbols(old)
    new_groups = _grouped_symbols(new)
    added: list[tuple[SymbolKey, SymbolChange]] = []
    removed: list[tuple[SymbolKey, SymbolChange]] = []
    relocated: list[tuple[SymbolKey, Relocation]] = []
    for group in old_groups.keys() | new_groups.keys():
        old_nodes = old_groups.get(group, [])
        new_nodes = new_groups.get(group, [])
        if _group_is_affected(old_nodes, new_nodes, error_paths):
            _compare_affected(group, old_nodes, new_nodes, error_paths, added, removed, relocated)
        else:
            _compare_ordinal(group, old_nodes, new_nodes, added, removed, relocated)

    old_relationships, old_error_keys = _keyed_relationships(old, error_paths)
    new_relationships, _ = _keyed_relationships(new)
    added_relationships = tuple(
        new_relationships[key] for key in sorted(set(new_relationships) - set(old_relationships))
    )
    removed_relationships = tuple(
        old_relationships[key]
        for key in sorted(set(old_relationships) - set(new_relationships))
        if key not in old_error_keys
    )
    return DiffResult(
        added=tuple(item for _, item in sorted(added, key=_by_key)),
        removed=tuple(item for _, item in sorted(removed, key=_by_key)),
        relocated=tuple(item for _, item in sorted(relocated, key=_by_key)),
        relationships_added=added_relationships,
        relationships_removed=removed_relationships,
    )


def _by_key(entry: tuple[SymbolKey, object]) -> SymbolKey:
    return entry[0]


def _node_path(node: Node) -> str | None:
    return node.location.path if node.location is not None else node.path


def _file_counts(nodes: list[Node]) -> dict[str | None, int]:
    counts: dict[str | None, int] = {}
    for node in nodes:
        path = _node_path(node)
        counts[path] = counts.get(path, 0) + 1
    return counts


def _group_is_affected(
    old_nodes: list[Node], new_nodes: list[Node], error_paths: frozenset[str]
) -> bool:
    # A missing member shifts cross-file ordinal pairing for its whole group,
    # but only when the per-file definition counts actually differ.
    if not error_paths or not any(_node_path(node) in error_paths for node in old_nodes):
        return False
    return _file_counts(old_nodes) != _file_counts(new_nodes)


def _compare_ordinal(
    group: tuple[str, str, str],
    old_nodes: list[Node],
    new_nodes: list[Node],
    added: list[tuple[SymbolKey, SymbolChange]],
    removed: list[tuple[SymbolKey, SymbolChange]],
    relocated: list[tuple[SymbolKey, Relocation]],
) -> None:
    kind, label, role = group
    shared = min(len(old_nodes), len(new_nodes))
    for ordinal in range(shared):
        if _start(old_nodes[ordinal].location) != _start(new_nodes[ordinal].location):
            relocated.append(
                ((kind, label, role, ordinal), _relocation(old_nodes[ordinal], new_nodes[ordinal]))
            )
    for ordinal in range(shared, len(new_nodes)):
        added.append(((kind, label, role, ordinal), _symbol_change(new_nodes[ordinal])))
    for ordinal in range(shared, len(old_nodes)):
        removed.append(((kind, label, role, ordinal), _symbol_change(old_nodes[ordinal])))


def _compare_affected(
    group: tuple[str, str, str],
    old_nodes: list[Node],
    new_nodes: list[Node],
    error_paths: frozenset[str],
    added: list[tuple[SymbolKey, SymbolChange]],
    removed: list[tuple[SymbolKey, SymbolChange]],
    relocated: list[tuple[SymbolKey, Relocation]],
) -> None:
    """Pair inside each file, never across files, and withhold error-file rows."""

    kind, label, role = group
    old_by_file: dict[str | None, list[tuple[int, Node]]] = {}
    new_by_file: dict[str | None, list[tuple[int, Node]]] = {}
    for ordinal, node in enumerate(old_nodes):
        old_by_file.setdefault(_node_path(node), []).append((ordinal, node))
    for ordinal, node in enumerate(new_nodes):
        new_by_file.setdefault(_node_path(node), []).append((ordinal, node))
    for path in old_by_file.keys() | new_by_file.keys():
        old_members = old_by_file.get(path, [])
        new_members = new_by_file.get(path, [])
        in_error = path in error_paths
        shared = min(len(old_members), len(new_members))
        for (_, old_node), (ordinal, new_node) in zip(old_members, new_members, strict=False):
            if not in_error and _start(old_node.location) != _start(new_node.location):
                relocated.append(((kind, label, role, ordinal), _relocation(old_node, new_node)))
        for ordinal, new_node in new_members[shared:]:
            added.append(((kind, label, role, ordinal), _symbol_change(new_node)))
        if not in_error:
            for ordinal, old_node in old_members[shared:]:
                removed.append(((kind, label, role, ordinal), _symbol_change(old_node)))


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
    old_start = _start_record(item.old_location)
    new_start = _start_record(item.new_location)
    if old_start is None or new_start is None:
        return f"~ {_symbol_text(item.symbol, item.role)} (relocated)\n"
    if old_start["path"] == new_start["path"]:
        move = f"{old_start['path']}:{old_start['line']}→{new_start['line']}"
    else:
        move = f"{old_start['path']}:{old_start['line']}→{new_start['path']}:{new_start['line']}"
    return f"~ {_symbol_text(item.symbol, item.role)} (relocated {move})\n"


def render_json(result: DiffResult) -> str:
    """Render the same semantic records used by text output."""

    return dump_json(result.to_dict())


def _grouped_symbols(document: GraphDocument) -> dict[tuple[str, str, str], list[Node]]:
    # Module nodes and contains edges describe graph scaffolding rather
    # than declarations an agent can act on.  Their source ranges shift
    # when a line is inserted, which would otherwise drown out the
    # function/class changes in a useful diff.  impact.py deliberately
    # makes the opposite call: it keeps module symbols as real inbound
    # dependants, because a module-scope caller is a genuine impact
    # target even though its location is unstable across edits.
    #
    # Duplicate symbols are legal and definitions() reports them.  Each
    # group's members are in source order so a member's position is its
    # ordinal and a removed duplicate is a removal rather than a collapsed
    # no-op.
    groups: dict[tuple[str, str, str], list[Node]] = {}
    for node in document.nodes:
        if node.node_class == NodeClass.SYMBOL and node.symbol_kind != "module":
            group = (node.symbol_kind or "unknown", node.label, declaration_role(node) or "")
            groups.setdefault(group, []).append(node)
    for nodes in groups.values():
        nodes.sort(key=_node_sort_key)
    return groups


def _node_keys(
    document: GraphDocument, error_paths: frozenset[str] = frozenset()
) -> tuple[dict[str, NodeKey], frozenset[str], frozenset[str]]:
    labels: dict[str, str] = {}
    for node in document.nodes:
        labels.setdefault(node.id, node.label)
    result: dict[str, NodeKey] = {}
    unresolved: set[str] = set()
    in_error: set[str] = set()
    for node in document.nodes:
        key: NodeKey
        # An unresolved reference is judged by its reference-site location.
        if error_paths and _node_path(node) in error_paths:
            in_error.add(node.id)
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
    return result, frozenset(unresolved), frozenset(in_error)


def _keyed_relationships(
    document: GraphDocument, error_paths: frozenset[str] = frozenset()
) -> tuple[dict[RelationshipKey, RelationshipChange], frozenset[RelationshipKey]]:
    """Key relationships and report the keys with an endpoint in an error file."""

    endpoint_keys, unresolved_ids, error_ids = _node_keys(document, error_paths)
    result: dict[RelationshipKey, RelationshipChange] = {}
    error_keys: set[RelationshipKey] = set()
    for relationship in document.relationships:
        if relationship.kind == "contains":
            continue
        source_key = endpoint_keys.get(relationship.source, ("node", relationship.source))
        target_key = endpoint_keys.get(relationship.target, ("node", relationship.target))
        key = (source_key, target_key, relationship.kind)
        if (
            relationship.source in error_ids
            or relationship.target in error_ids
            or any(
                location.path in error_paths
                for record in relationship.evidence
                for location in record.locations
            )
        ):
            error_keys.add(key)
        result.setdefault(
            key,
            RelationshipChange(
                kind=relationship.kind,
                source=_display_key(source_key),
                target=_display_key(target_key),
                unresolved=relationship.target in unresolved_ids,
            ),
        )
    return result, frozenset(error_keys)


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
