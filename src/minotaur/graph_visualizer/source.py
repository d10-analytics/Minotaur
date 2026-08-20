"""Contained, best-effort source excerpts for a portable visualizer."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from minotaur.source import merge_spans, read_source_path

_MAX_CONTEXT = 50


def prepare_excerpts(
    canonical: Mapping[str, object], source_root: Path | None
) -> dict[str, object]:
    """Read only required source spans, never allowing a graph path to escape root.

    The artifact contains numbered source lines in merged intervals.  It does
    not include whole files merely because a location occurs in one. This keeps
    a portable artifact useful for inspection without turning graph export into
    an implicit source-tree export.
    """
    needed: dict[str, list[tuple[int, int]]] = defaultdict(list)
    # Keep these associations beside, rather than inside, canonical graph data:
    # source text is an optional visualization-time capability and must not
    # change graph identity or make ordinary JSON exports disclose source.
    call_sites: dict[str, list[dict[str, object]]] = defaultdict(list)
    nodes = cast(list[dict[str, Any]], canonical["nodes"])
    node_by_id = {node["id"]: node for node in nodes}
    relationships = cast(list[dict[str, Any]], canonical["relationships"])
    for relationship_index, relationship in enumerate(relationships):
        source = node_by_id.get(relationship["source"])
        caller_start = _node_start(source)
        for evidence in cast(list[dict[str, Any]], relationship["evidence"]):
            for location in cast(Iterable[dict[str, Any]], evidence.get("locations", [])):
                path, start, end = _location_lines(location)
                # Evidence is often a one-line reference. A bounded surrounding
                # window preserves enough context to understand it while making
                # artifact size and source disclosure proportional to the graph.
                needed[path].append((max(0, start - _MAX_CONTEXT), end + _MAX_CONTEXT))
                if (
                    relationship["kind"] == "calls"
                    and caller_start is not None
                    and _node_path(source) == path
                ):
                    needed[path].append((caller_start, end))
                if relationship["kind"] == "calls":
                    # This is visualization-only metadata.  Its relationship
                    # index refers to the canonical relationship order in the
                    # accompanying presentation payload, never the graph JSON.
                    site: dict[str, object] = {
                        "location": dict(location),
                        "provenance": str(evidence["provenance"]),
                    }
                    # A same-file function/method start makes the prefix mode
                    # meaningful. Cross-file callers and unknown symbols would
                    # create a misleading excerpt, so deliberately omit it.
                    if caller_start is not None and _node_path(source) == path:
                        site["caller_start"] = caller_start
                    call_sites[str(relationship_index)].append(site)
    if source_root is None:
        # Preserve the site list even when no bytes can be read. The viewer can
        # still identify the graph fact and explain why context is unavailable.
        return {
            "paths": _all_unavailable(needed, "no source root was provided"),
            "call_sites": dict(call_sites),
        }
    try:
        root = source_root.resolve(strict=True)
    except OSError as error:
        return {
            "paths": _all_unavailable(
                needed, f"source root unavailable: {error.strerror or error}"
            ),
            "call_sites": dict(call_sites),
        }
    if not root.is_dir():
        return {
            "paths": _all_unavailable(needed, "source root is not a directory"),
            "call_sites": dict(call_sites),
        }
    result: dict[str, object] = {}
    for path, spans in needed.items():
        result[path] = read_source_path(root, path, spans)
    # Relationship indexes are stable because both this function and the
    # presentation renderer consume the already-canonical relationship order.
    return {"paths": result, "call_sites": dict(call_sites)}


def _node_start(node: dict[str, Any] | None) -> int | None:
    if (
        node is None
        or node.get("node_class") != "symbol"
        or node.get("symbol_kind") not in {"function", "method"}
    ):
        return None
    location = node.get("location")
    if not isinstance(location, dict):
        return None
    return _location_lines(location)[1]


def _node_path(node: dict[str, Any] | None) -> str | None:
    if node is None or not isinstance(node.get("location"), dict):
        return None
    return _location_lines(cast(dict[str, Any], node["location"]))[0]


def _location_lines(location: Mapping[str, Any]) -> tuple[str, int, int]:
    range_data = cast(Mapping[str, Mapping[str, int]], location["range"])
    return str(location["path"]), range_data["start"]["line"], range_data["end"]["line"]


def _all_unavailable(paths: Iterable[str], reason: str) -> dict[str, object]:
    return {path: {"status": "unavailable", "reason": reason} for path in paths}
