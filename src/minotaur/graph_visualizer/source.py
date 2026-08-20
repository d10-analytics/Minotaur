"""Contained, best-effort source excerpts for a portable visualizer."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

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
    nodes = cast(list[dict[str, Any]], canonical["nodes"])
    node_by_id = {node["id"]: node for node in nodes}
    relationships = cast(list[dict[str, Any]], canonical["relationships"])
    for relationship in relationships:
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
    if source_root is None:
        return _all_unavailable(needed, "no source root was provided")
    try:
        root = source_root.resolve(strict=True)
    except OSError as error:
        return _all_unavailable(needed, f"source root unavailable: {error.strerror or error}")
    if not root.is_dir():
        return _all_unavailable(needed, "source root is not a directory")
    result: dict[str, object] = {}
    for path, spans in needed.items():
        result[path] = _read_path(root, path, spans)
    return result


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


def _read_path(root: Path, wire_path: str, spans: list[tuple[int, int]]) -> dict[str, object]:
    candidate = root.joinpath(*wire_path.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        # Resolve before checking containment so a symlink cannot make an
        # apparently relative graph path disclose a file outside source_root.
        resolved.relative_to(root)
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "path is missing or escapes the source root"}
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"status": "unavailable", "reason": "source file is not UTF-8"}
    except OSError as error:
        return {
            "status": "unavailable",
            "reason": f"source file is unreadable: {error.strerror or error}",
        }
    lines = text.splitlines()
    merged = _merge_spans(spans, len(lines))
    return {
        "status": "available",
        "spans": [{"start": start, "lines": lines[start:end]} for start, end in merged],
    }


def _merge_spans(spans: Iterable[tuple[int, int]], line_count: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted((max(0, a), min(line_count, b + 1)) for a, b in spans):
        if start >= end:
            continue
        # Adjacent excerpts are merged too: readers get continuous context and
        # the payload never repeats the same numbered source line.
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _all_unavailable(paths: Iterable[str], reason: str) -> dict[str, object]:
    return {path: {"status": "unavailable", "reason": reason} for path in paths}
