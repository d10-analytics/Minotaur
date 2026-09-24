"""Deterministic structural warnings derived from a completed SQL graph."""

from __future__ import annotations

from collections.abc import Mapping

from minotaur.config import SqlSettings
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.node import Node
from minotaur.language_interpreter.contract import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
)

NAMESPACE = "minotaur-sql"
_READS = "sql:reads-from"
_REFERENCES = "references"


def analyze_view_warnings(
    document: GraphDocument, settings: SqlSettings | None = None
) -> tuple[Diagnostic, ...]:
    """Return cycle and depth warnings from one fully resolved SQL graph."""
    threshold = (settings or SqlSettings()).view_depth_threshold
    nodes = {node.id: node for node in document.nodes}
    views = {node.id: node for node in document.nodes if node.symbol_kind == "sql:view"}
    view_edges: dict[str, tuple[str, ...]] = {}
    terminal_edges: dict[str, tuple[str, ...]] = {}
    unresolved_edges: dict[str, tuple[str, ...]] = {}
    for relationship in document.relationships:
        source = nodes.get(relationship.source)
        target = nodes.get(relationship.target)
        if source is None or target is None or source.id not in views:
            continue
        if relationship.kind == _READS:
            if target.id in views:
                view_edges.setdefault(source.id, tuple())
                view_edges[source.id] = tuple(sorted((*view_edges[source.id], target.id)))
            elif target.symbol_kind == "sql:table":
                terminal_edges.setdefault(source.id, tuple())
                terminal_edges[source.id] = tuple(sorted((*terminal_edges[source.id], target.id)))
        elif relationship.kind == _REFERENCES and target.node_class.value == "unresolved-reference":
            unresolved_edges.setdefault(source.id, tuple())
            unresolved_edges[source.id] = tuple(sorted((*unresolved_edges[source.id], target.id)))

    cycles = _elementary_cycles(view_edges, views)
    cyclic = {node_id for cycle in cycles for node_id in cycle[:-1]}
    warnings: list[Diagnostic] = []
    for cycle in cycles:
        labels = tuple(views[node_id].label for node_id in cycle)
        first = views[cycle[0]]
        warnings.append(
            Diagnostic(
                DiagnosticCode.CIRCULAR_DEPENDENCY,
                first.location.path if first.location is not None else "<graph>",
                "circular view dependency: " + " -> ".join(labels),
                first.location,
                DiagnosticSeverity.WARNING,
                {NAMESPACE: {"path": list(labels)}},
            )
        )

    # A branch entering a cycle is discarded while walking that branch. A
    # view with another complete terminal branch can still report the longest
    # valid path on that other branch.
    excluded = cyclic
    for root_id in sorted(views):
        if root_id in excluded:
            continue
        candidate = _longest_path(
            root_id,
            views,
            view_edges,
            terminal_edges,
            unresolved_edges,
            excluded,
            set(),
        )
        if candidate is None:
            continue
        depth, path = candidate
        if depth <= threshold:
            continue
        first = views[root_id]
        labels = tuple(nodes[node_id].label for node_id in path)
        warnings.append(
            Diagnostic(
                DiagnosticCode.VIEW_DEPTH_WARNING,
                first.location.path if first.location is not None else "<graph>",
                f"view dependency depth {depth} exceeds threshold {threshold}: "
                + " -> ".join(labels),
                first.location,
                DiagnosticSeverity.WARNING,
                {NAMESPACE: {"depth": depth, "path": list(labels)}},
            )
        )
    return tuple(
        sorted(
            warnings,
            key=lambda item: (
                item.path,
                item.location.sort_key if item.location else ("", 0, 0, 0, 0),
                item.code.value,
                item.message,
            ),
        )
    )


def _elementary_cycles(
    edges: Mapping[str, tuple[str, ...]], views: Mapping[str, Node]
) -> tuple[tuple[str, ...], ...]:
    """Enumerate directed simple cycles, canonicalized by node ID rotation."""
    found: set[tuple[str, ...]] = set()
    for start in sorted(views):

        def visit(current: str, path: tuple[str, ...], cycle_start: str = start) -> None:
            for target in edges.get(current, ()):
                if target == cycle_start:
                    cycle = path + (cycle_start,)
                    body = cycle[:-1]
                    minimum = min(
                        range(len(body)),
                        key=lambda index: (views[body[index]].label.casefold(), body[index]),
                    )
                    rotated = body[minimum:] + body[:minimum] + (body[minimum],)
                    found.add(rotated)
                elif target in views and target not in path and target >= cycle_start:
                    visit(target, path + (target,))

        visit(start, (start,))
    return tuple(sorted(found))


def _longest_path(
    current: str,
    views: Mapping[str, Node],
    view_edges: Mapping[str, tuple[str, ...]],
    terminal_edges: Mapping[str, tuple[str, ...]],
    unresolved_edges: Mapping[str, tuple[str, ...]],
    excluded: set[str],
    active: set[str],
) -> tuple[int, tuple[str, ...]] | None:
    if current in active or current in excluded:
        return None
    active = active | {current}
    candidates: list[tuple[int, tuple[str, ...]]] = []
    for target in terminal_edges.get(current, ()):
        candidates.append((1, (current, target)))
    for target in unresolved_edges.get(current, ()):
        candidates.append((1, (current, target)))
    for target in view_edges.get(current, ()):
        nested = _longest_path(
            target,
            views,
            view_edges,
            terminal_edges,
            unresolved_edges,
            excluded,
            active,
        )
        if nested is not None:
            candidates.append((nested[0] + 1, (current, *nested[1])))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item[0],
            tuple(views[node].label if node in views else node for node in item[1]),
        ),
    )
