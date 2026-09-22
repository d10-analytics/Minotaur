"""Safe source-file readers shared by query and visualization consumers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from minotaur.graph_model.location import is_safe_path


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
        content = resolved.read_bytes()
    except UnicodeDecodeError:
        return {"status": "unavailable", "reason": "source file is not UTF-8"}
    except OSError as error:
        return {
            "status": "unavailable",
            "reason": f"source file is unreadable: {error.strerror or error}",
        }
    return read_source_bytes(wire_path, content, spans)


def read_source_bytes(
    wire_path: str, content: bytes, spans: Sequence[tuple[int, int]]
) -> dict[str, object]:
    """Render bounded line spans from already captured UTF-8 *content*.

    Comparison presentation must use this boundary after its source captures
    have been released.  Keeping byte decoding here makes it impossible for a
    later renderer call to silently fall back to a live checkout path.
    """
    if not isinstance(content, bytes):
        raise TypeError("captured source content must be bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "unavailable", "reason": "source file is not UTF-8"}
    lines = text.splitlines()
    merged = merge_spans(spans, len(lines))
    return {
        "status": "available",
        "spans": [{"start": start, "lines": lines[start:end]} for start, end in merged],
    }


def capture_source_bytes(root: Path, paths: Iterable[str]) -> Mapping[str, bytes]:
    """Capture selected repository-relative files as an immutable byte map.

    The returned mapping is detached from *root*: callers may mutate or remove
    the source tree after capture without changing the captured evidence.
    Unsafe, missing, or unreadable paths are omitted and are represented as
    unavailable by comparison excerpt preparation.
    """
    try:
        source_root = root.resolve(strict=True)
    except OSError:
        return MappingProxyType({})
    if not source_root.is_dir():
        return MappingProxyType({})
    captured: dict[str, bytes] = {}
    for wire_path in sorted(set(paths)):
        if not isinstance(wire_path, str) or not is_safe_path(wire_path):
            continue
        candidate = source_root.joinpath(*wire_path.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(source_root)
            if not resolved.is_file():
                continue
            captured[wire_path] = bytes(resolved.read_bytes())
        except (OSError, ValueError):
            continue
    return MappingProxyType(captured)


# Descriptive aliases keep the capture boundary discoverable to comparison
# owners without introducing another source-reading implementation.
capture_source = capture_source_bytes
read_captured_source = read_source_bytes


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
