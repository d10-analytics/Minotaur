"""Prepare a renderer-neutral presentation payload from validated graph data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast


def build_presentation(
    canonical: Mapping[str, object], excerpts: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Return JSON-safe renderer input without reading files or producing HTML.

    The canonical graph already guarantees one relationship per structural
    tuple. The renderer deliberately receives every evidence record on that
    relationship rather than a display-selected representative: collapsing
    evidence for a compact graph view must not silently discard provenance or
    locations that an inspector needs to audit the displayed connection.
    """
    nodes = cast(list[dict[str, Any]], canonical["nodes"])
    relationships = cast(list[dict[str, Any]], canonical["relationships"])
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
        "excerpts": dict(excerpts or {}),
    }
