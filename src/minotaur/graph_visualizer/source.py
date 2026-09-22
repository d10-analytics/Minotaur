"""Contained, best-effort source excerpts for a portable visualizer."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from minotaur.source import capture_source_bytes as _capture_source_bytes
from minotaur.source import read_source_bytes, read_source_path

_MAX_CONTEXT = 50

capture_source_bytes = _capture_source_bytes

__all__ = [
    "capture_source_bytes",
    "prepare_comparison_excerpts",
    "prepare_excerpts",
    "read_source_bytes",
]


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


def prepare_comparison_excerpts(
    complete_result: object,
    captured_sources: Mapping[str, Mapping[str, bytes]] | None = None,
) -> dict[str, object]:
    """Build side-correct call excerpts from immutable captured source bytes.

    ``complete_result`` is the already-computed comparison.  This function
    intentionally accepts only byte maps for source evidence; it has no source
    root parameter and therefore cannot reread a checkout after comparison.
    The returned ``before``/``after`` records retain unavailable states instead
    of presenting a live or partial excerpt as comparison evidence.
    """
    source_maps = _comparison_source_maps(captured_sources)
    call_changes = tuple(getattr(complete_result, "call_changes", ()))
    relationships = tuple(getattr(complete_result, "relationships", ()))
    nodes = tuple(getattr(complete_result, "nodes", ()))
    paths_by_side: dict[str, dict[str, list[tuple[int, int]]]] = {
        "before": defaultdict(list),
        "after": defaultdict(list),
    }
    sites_by_side: dict[str, dict[str, list[dict[str, object]]]] = {
        "before": defaultdict(list),
        "after": defaultdict(list),
    }
    for change in call_changes:
        relationship_id = str(getattr(change, "relationship_id", ""))
        for side, field in (("before", "before"), ("after", "after")):
            observations = _observation_records(getattr(change, field, None))
            evidence = _relationship_evidence(relationships, relationship_id, side)
            for observation in observations:
                location = _observation_location(observation)
                if location is None:
                    continue
                path, start, end = _location_lines(location)
                paths_by_side[side][path].append((max(0, start - _MAX_CONTEXT), end + _MAX_CONTEXT))
                caller_start = _caller_start(
                    nodes, relationships, relationship_id, side, observed_path=path
                )
                site: dict[str, object] = {
                    "location": dict(location),
                    "provenance": list(evidence),
                }
                if caller_start is not None:
                    # Merge the caller prefix into the same stored byte span;
                    # rendering after capture release must never need a live
                    # source reread to satisfy the prefix mode.
                    paths_by_side[side][path].append((caller_start, end))
                    site["caller_start"] = caller_start
                sites_by_side[side][relationship_id].append(site)

    rendered: dict[str, object] = {}
    for side in ("before", "after"):
        paths: dict[str, object] = {}
        source_map = source_maps[side]
        for path, spans in paths_by_side[side].items():
            content = source_map.get(path)
            if content is None:
                paths[path] = {
                    "status": "unavailable",
                    "reason": "captured source bytes are unavailable",
                }
            else:
                paths[path] = read_source_bytes(path, content, spans)
        rendered[side] = {
            "paths": paths,
            "call_sites": dict(sites_by_side[side]),
        }
    return rendered


def _comparison_source_maps(
    captured_sources: Mapping[str, Mapping[str, bytes]] | None,
) -> dict[str, Mapping[str, bytes]]:
    """Normalize old/new aliases while retaining a detached read-only view."""
    supplied = captured_sources or {}
    before = supplied.get("before", supplied.get("old", {}))
    after = supplied.get("after", supplied.get("new", {}))
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise TypeError("comparison captured sources must map before/after sides to byte maps")
    return {"before": before, "after": after}


def _observation_records(value: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(value, Mapping):
        if "expression" in value or "callee" in value:
            return (value,)
        return ()
    if isinstance(value, (tuple, list)):
        return tuple(record for item in value for record in _observation_records(item))
    return ()


def _observation_location(observation: Mapping[str, object]) -> Mapping[str, object] | None:
    expression = observation.get("expression")
    callee = observation.get("callee")
    location = expression if isinstance(expression, Mapping) else callee
    if not isinstance(location, Mapping):
        return None
    if not isinstance(location.get("path"), str) or not isinstance(location.get("range"), Mapping):
        return None
    return _plain_mapping(location)


def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Detach nested frozen comparison values for the JSON presentation."""
    return {str(key): _plain_value(item) for key, item in value.items()}


def _plain_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _plain_mapping(value)
    if isinstance(value, (tuple, list)):
        return [_plain_value(item) for item in value]
    return value


def _relationship_evidence(
    relationships: Sequence[object], relationship_id: str, side: str
) -> tuple[str, ...]:
    for relationship in relationships:
        if str(getattr(relationship, "id", "")) != relationship_id:
            continue
        value = getattr(relationship, side, None)
        payload = value if isinstance(value, Mapping) else {}
        evidence = payload.get("evidence", ())
        if not isinstance(evidence, (tuple, list)):
            return ()
        return tuple(
            str(item.get("provenance"))
            for item in evidence
            if isinstance(item, Mapping) and isinstance(item.get("provenance"), str)
        )
    return ()


def _caller_start(
    nodes: Sequence[object],
    relationships: Sequence[object],
    relationship_id: str,
    side: str,
    *,
    observed_path: str,
) -> int | None:
    source_id: str | None = None
    for relationship in relationships:
        if str(getattr(relationship, "id", "")) == relationship_id:
            source_id = str(getattr(relationship, "source", ""))
            break
    if not source_id:
        return None
    for node in nodes:
        if str(getattr(node, "id", "")) != source_id:
            continue
        value = getattr(node, side, None)
        payload = _node_side_payload(value)
        if payload.get("node_class") != "symbol":
            return None
        if payload.get("symbol_kind") not in {"function", "method"}:
            return None
        location = payload.get("location")
        if isinstance(location, Mapping) and isinstance(location.get("range"), Mapping):
            caller_location = cast(Mapping[str, Any], location)
            if caller_location.get("path") != observed_path:
                return None
            return int(caller_location["range"]["start"]["line"])
        return None
    return None


def _node_side_payload(value: object) -> Mapping[str, object]:
    """Return the canonical node stored on one comparison side.

    Comparison node sides are ``{node, system}`` records; a bare canonical
    payload is still accepted so hand-built values keep working. The nested
    canonical payload is what distinguishes the side record wrapper.
    """
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("node")
    if isinstance(nested, Mapping):
        return nested
    return value


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
