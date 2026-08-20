"""Safe source-file readers shared by query and visualization consumers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def read_source_path(root: Path, wire_path: str, spans: list[tuple[int, int]]) -> dict[str, object]:
    """Read bounded line spans while keeping a graph path inside ``root``.

    ``wire_path`` is a repository-relative, slash-separated graph path and
    span endpoints are zero-based inclusive line numbers. The return shape is
    intentionally the visualizer's existing presentation payload so the
    source-reading policy has one owner for every consumer.
    """
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
    merged = merge_spans(spans, len(lines))
    return {
        "status": "available",
        "spans": [{"start": start, "lines": lines[start:end]} for start, end in merged],
    }


def merge_spans(spans: Iterable[tuple[int, int]], line_count: int) -> list[tuple[int, int]]:
    """Clamp, sort, and merge inclusive source line spans."""
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
