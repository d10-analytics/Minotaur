"""Inbound dependency impact query and its stable output records."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from minotaur.graph_model.provenance import NodeClass, RelationshipKind
from minotaur.graph_model.slicing import bfs
from minotaur.query.index import GraphIndex


@dataclass(frozen=True, slots=True)
class ImpactRecord:
    """One symbol reached at a BFS depth, or just beyond a depth cut."""

    depth: int
    symbol: str
    kind: str
    boundary: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "boundary": self.boundary,
            "depth": self.depth,
            "kind": self.kind,
            "symbol": self.symbol,
        }


def impact(
    index: GraphIndex,
    qualified_name: str,
    max_depth: int | None = None,
) -> tuple[ImpactRecord, ...]:
    """Return inbound ``calls``/``imports`` impact records by shortest depth."""

    if max_depth is not None and max_depth < 0:
        raise ValueError(f"depth must be non-negative, got {max_depth}")
    target = index.symbols_by_label.get(qualified_name, ())
    if len(target) != 1:
        return ()

    target_id = target[0].id
    adjacency: dict[str, set[str]] = defaultdict(set)
    for kind in (RelationshipKind.CALLS.value, RelationshipKind.IMPORTS.value):
        for relationship in index.relationships(kind):
            # Inverting source -> target makes the BFS from the queried symbol
            # walk inbound dependencies while retaining the selected kinds.
            adjacency[relationship.target].add(relationship.source)

    depths = bfs(frozenset({target_id}), adjacency, max_depth)
    boundary_depths: dict[str, int] = {}
    if max_depth is not None:
        for node_id, depth in depths.items():
            if depth != max_depth:
                continue
            for neighbor in adjacency.get(node_id, ()):
                if neighbor not in depths:
                    boundary_depths.setdefault(neighbor, max_depth + 1)

    records: list[ImpactRecord] = []
    for node_id, depth in depths.items():
        node = index.nodes.get(node_id)
        if node is None or node.node_class != NodeClass.SYMBOL:
            continue
        records.append(
            ImpactRecord(
                depth=depth,
                symbol=node.label,
                kind=node.symbol_kind or "unknown",
            )
        )
    for node_id, depth in boundary_depths.items():
        node = index.nodes.get(node_id)
        if node is None or node.node_class != NodeClass.SYMBOL:
            continue
        records.append(
            ImpactRecord(
                depth=depth,
                symbol=node.label,
                kind=node.symbol_kind or "unknown",
                boundary=True,
            )
        )
    return tuple(sorted(records, key=lambda record: (record.boundary, record.depth, record.symbol)))


def render_text(records: Sequence[ImpactRecord]) -> str:
    """Render one compact line per symbol, grouped by depth."""

    if not records:
        return "no impact\n"
    return "".join(
        f"{'[boundary] ' if record.boundary else ''}depth {record.depth}: {record.symbol}\n"
        for record in records
    )


def render_json(records: Sequence[ImpactRecord]) -> str:
    """Serialize the same records used by text output."""

    return (
        json.dumps(
            {"query": "impact", "results": [record.to_dict() for record in records]},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
