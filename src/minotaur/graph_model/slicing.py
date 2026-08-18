"""Bounded graph slicing for Minotaur graph documents.

Slicing extracts a subgraph around a set of seed nodes by following
relationships up to a bounded depth. The result is a valid GraphDocument
that preserves all evidence on included relationships and maintains
unresolved-reference identity integrity.

The slicing module sits after validation in the graph-handling pipeline:
parse JSON, validate against the JSON Schema, construct the graph model,
run semantic validation, then slice. A slice of a valid document is itself
valid — the integrity pass ensures that unresolved-reference nodes whose
originating node was not reached by traversal still have that node present
in the output.
"""

from __future__ import annotations

import enum
from collections import defaultdict, deque
from collections.abc import Set
from dataclasses import dataclass

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.provenance import IdentityBasis


class SliceDirection(enum.Enum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class SliceResult:
    document: GraphDocument
    seed_ids: frozenset[str]
    boundary_ids: frozenset[str]
    depth: int | None


def slice_document(
    document: GraphDocument,
    seed_ids: Set[str],
    *,
    max_depth: int | None = None,
    direction: SliceDirection = SliceDirection.BOTH,
) -> SliceResult:
    if max_depth is not None and max_depth < 0:
        raise ValueError(f"max_depth must be non-negative, got {max_depth}")

    all_node_ids = {node.id for node in document.nodes}
    unknown = set(seed_ids) - all_node_ids
    if unknown:
        raise ValueError(f"seed IDs not found in document: {sorted(unknown)}")

    frozen_seeds = frozenset(seed_ids)

    if not seed_ids:
        return SliceResult(
            document=GraphDocument(
                coordinate_encoding=document.coordinate_encoding,
                generated_by=document.generated_by,
                generated_at=document.generated_at,
                source_control=document.source_control,
                extensions=document.extensions,
            ),
            seed_ids=frozen_seeds,
            boundary_ids=frozenset(),
            depth=max_depth,
        )

    adjacency = _build_adjacency(document, direction)
    reached = _bfs(frozen_seeds, adjacency, max_depth)
    integrity_added = _ensure_unresolved_reference_integrity(document, reached)
    boundary = _compute_boundary(reached, adjacency, max_depth, integrity_added)

    sliced_nodes = tuple(n for n in document.nodes if n.id in reached)
    sliced_rels = tuple(
        r for r in document.relationships
        if r.source in reached and r.target in reached
    )

    sliced_doc = GraphDocument(
        coordinate_encoding=document.coordinate_encoding,
        nodes=sliced_nodes,
        relationships=sliced_rels,
        generated_by=document.generated_by,
        generated_at=document.generated_at,
        source_control=document.source_control,
        extensions=document.extensions,
    )

    return SliceResult(
        document=sliced_doc,
        seed_ids=frozen_seeds,
        boundary_ids=boundary,
        depth=max_depth,
    )


def _build_adjacency(
    document: GraphDocument,
    direction: SliceDirection,
) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for rel in document.relationships:
        if direction in (SliceDirection.OUTGOING, SliceDirection.BOTH):
            adj[rel.source].add(rel.target)
        if direction in (SliceDirection.INCOMING, SliceDirection.BOTH):
            adj[rel.target].add(rel.source)
    return adj


def _bfs(
    seeds: frozenset[str],
    adjacency: dict[str, set[str]],
    max_depth: int | None,
) -> dict[str, int]:
    reached: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque()

    for seed in seeds:
        reached[seed] = 0
        queue.append((seed, 0))

    while queue:
        node_id, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for neighbor in adjacency.get(node_id, ()):
            if neighbor not in reached:
                reached[neighbor] = depth + 1
                queue.append((neighbor, depth + 1))

    return reached


def _ensure_unresolved_reference_integrity(
    document: GraphDocument,
    reached: dict[str, int],
) -> frozenset[str]:
    node_by_id = {n.id: n for n in document.nodes}
    integrity_added: set[str] = set()
    added = True
    while added:
        added = False
        for node_id in list(reached):
            node = node_by_id.get(node_id)
            if node is None:
                continue
            if node.identity.basis != IdentityBasis.UNRESOLVED_REFERENCE:
                continue
            origin = node.identity.originating_node
            if origin is not None and origin not in reached:
                reached[origin] = reached[node_id]
                integrity_added.add(origin)
                added = True
    return frozenset(integrity_added)


def _compute_boundary(
    reached: dict[str, int],
    adjacency: dict[str, set[str]],
    max_depth: int | None,
    integrity_added: frozenset[str],
) -> frozenset[str]:
    if max_depth is None:
        return frozenset()
    boundary: set[str] = set()
    for node_id, depth in reached.items():
        if node_id in integrity_added:
            continue
        if depth != max_depth:
            continue
        neighbors = adjacency.get(node_id, set())
        if neighbors - reached.keys():
            boundary.add(node_id)
    return frozenset(boundary)
