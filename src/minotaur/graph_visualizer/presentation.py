"""Prepare a renderer-neutral presentation payload from validated graph data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from minotaur.graph_model.node import Node
from minotaur.graph_visualizer.source import prepare_comparison_excerpts
from minotaur.system import EndpointKind, System, classify_endpoint


def build_presentation(
    canonical: Mapping[str, object] | object,
    excerpts: Mapping[str, object] | None = None,
    *,
    systems: Sequence[System] = (),
    document_nodes: Sequence[Node] = (),
    comparison_sources: Mapping[str, Mapping[str, bytes]] | None = None,
) -> dict[str, object]:
    """Return JSON-safe renderer input without reading files or producing HTML.

    The canonical graph already guarantees one relationship per structural
    tuple. The renderer deliberately receives every evidence record on that
    relationship rather than a display-selected representative: collapsing
    evidence for a compact graph view must not silently discard provenance or
    locations that an inspector needs to audit the displayed connection.
    """
    if not isinstance(canonical, Mapping):
        if comparison_sources is not None or hasattr(canonical, "call_changes"):
            return build_comparison_presentation(canonical, comparison_sources, excerpts=excerpts)
        raise TypeError("graph presentation requires a canonical mapping")
    nodes = cast(list[dict[str, Any]], canonical["nodes"])
    relationships = cast(list[dict[str, Any]], canonical["relationships"])
    if systems and {node.id for node in document_nodes} != {node["id"] for node in nodes}:
        raise ValueError("system presentation requires every canonical node")
    node_systems = {
        node.id: membership.system.name
        for node in document_nodes
        if (membership := classify_endpoint(systems, node)).kind is EndpointKind.SYSTEM
        and membership.system is not None
    }
    node_classes = sorted({node["node_class"] for node in nodes})
    provenance = sorted(
        {
            evidence["provenance"]
            for relationship in relationships
            for evidence in cast(list[dict[str, Any]], relationship["evidence"])
        }
    )
    relationship_kinds = sorted({rel["kind"] for rel in relationships})
    return {
        "graph": dict(canonical),
        "node_classes": node_classes,
        "relationship_kinds": relationship_kinds,
        "provenance": provenance,
        "systems": [system.name for system in systems],
        "node_systems": node_systems,
        # Give the self-contained viewer a stable empty shape when callers do
        # not request source loading, avoiding a UI-only special case while
        # keeping the canonical ``graph`` payload source-free.
        "excerpts": dict(excerpts or {"paths": {}, "call_sites": {}}),
    }


def build_comparison_presentation(
    complete_result: object,
    captured_sources: Mapping[str, Mapping[str, bytes]] | None = None,
    *,
    excerpts: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a renderer-neutral payload from one complete typed comparison.

    Comparison records are never rebuilt from a selected system or a live
    source tree.  Stable IDs, statuses, memberships, and full side-local
    payloads come directly from the immutable result; source excerpts come
    only from the optional captured byte maps.
    """
    result_dict = _result_dict(complete_result)
    nodes = tuple(getattr(complete_result, "nodes", ()))
    relationships = tuple(getattr(complete_result, "relationships", ()))
    calls = tuple(getattr(complete_result, "call_changes", ()))
    node_payloads = [_comparison_record(item) for item in nodes]
    relationship_payloads = [_comparison_record(item) for item in relationships]
    call_payloads = [_comparison_record(item) for item in calls]
    graph = {
        "nodes": node_payloads,
        "relationships": relationship_payloads,
    }
    old_names = result_dict.get("old_system_names", ())
    new_names = result_dict.get("new_system_names", ())
    old_name_set = set(old_names) if isinstance(old_names, (tuple, list)) else set()
    new_name_set = set(new_names) if isinstance(new_names, (tuple, list)) else set()
    system_names = sorted(
        {
            name
            for item in (*nodes, *relationships, *calls)
            for name in getattr(item, "involved_systems", ())
        }
        | old_name_set
        | new_name_set
    )
    comparison_excerpts = (
        dict(excerpts)
        if excerpts is not None
        else prepare_comparison_excerpts(complete_result, captured_sources)
    )
    return {
        # ``graph`` retains the renderer's existing top-level entry point while
        # its comparison records carry the complete before/after evidence.
        "graph": graph,
        "comparison": {
            **result_dict,
            "graph": graph,
            "nodes": node_payloads,
            "relationships": relationship_payloads,
            "calls": call_payloads,
        },
        "systems": system_names,
        # Membership is per side: a node that moved between systems belongs to
        # different names on Before and After, so a flat union would let the
        # viewer invent same-revision internal relationships.
        "node_systems": {
            item["id"]: {
                "before": _side_system(item.get("before")),
                "after": _side_system(item.get("after")),
            }
            for item in node_payloads
        },
        "excerpts": comparison_excerpts,
    }


def _side_system(value: object) -> str | None:
    """Return the declared system stored on one side record, if any."""
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    if isinstance(value, Mapping):
        name = value.get("system")
        if isinstance(name, str):
            return name
    return None


def _result_dict(result: object) -> dict[str, object]:
    to_dict = getattr(result, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("comparison presentation requires a complete typed result")
    value = to_dict()
    if not isinstance(value, dict):
        raise TypeError("comparison result serialization must be a mapping")
    return dict(value)


def _comparison_record(item: object) -> dict[str, object]:
    to_dict = getattr(item, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("comparison records must provide to_dict")
    value = to_dict()
    if not isinstance(value, dict):
        raise TypeError("comparison record serialization must be a mapping")
    payload = dict(value)
    if "relationship_id" in payload and "id" not in payload:
        payload["id"] = payload["relationship_id"]
    before = payload.get("before")
    after = payload.get("after")
    payload["presence"] = (
        "both"
        if before is not None and after is not None
        else "before"
        if before is not None
        else "after"
    )
    # A removed relationship is the one case where showing the historical
    # side first is materially safer: it keeps the edge visible on opening.
    if payload.get("status") == "removed":
        payload["default_side"] = "before"
    else:
        payload["default_side"] = "after"
    return payload


# Short aliases make the comparison owner explicit without duplicating the
# payload construction path.
build_comparison = build_comparison_presentation
