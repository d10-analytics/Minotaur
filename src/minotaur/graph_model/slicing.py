"""Breadth-first traversal for Minotaur graph querying.

``bfs`` returns the shortest depth for every node reachable from a set of
seed nodes, optionally bounded by a maximum depth. The traversal is
deliberately independent of graph-document concerns so callers can construct
a filtered adjacency map for a specific query. Callers supply the adjacency
map: a mapping from node ID to the IDs it connects to.
"""

from __future__ import annotations

from collections import deque


def bfs(
    seeds: frozenset[str],
    adjacency: dict[str, set[str]],
    max_depth: int | None,
) -> dict[str, int]:
    """Return the shortest depth for every node reached from ``seeds``.

    Every seed starts at depth 0; a neighbor is discovered at ``depth + 1``
    the first time it is seen, which makes the recorded depth the shortest
    path length in hops. ``max_depth`` of ``None`` means unlimited; otherwise
    nodes at that depth are not expanded further. Seeds absent from
    ``adjacency`` are still reported at depth 0.
    """
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
