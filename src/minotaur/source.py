"""Safe source-file readers shared by query and visualization consumers."""

from __future__ import annotations

import errno
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from minotaur.graph_model.location import is_safe_path
from minotaur.source_encoding import decode_python_source, is_python_source_path


def resolve_source_path(root: Path, wire_path: str) -> Path | None:
    """Resolve *wire_path* strictly under an already resolved *root*.

    Returns the resolved path, or ``None`` when it is missing, unresolvable
    (including a symlink loop, which Python before 3.13 reports as
    ``RuntimeError`` and 3.13+ as ``OSError`` with ``ELOOP``), or resolves
    outside *root*. Any other ``OSError`` (permission denied, I/O failure)
    propagates so each caller keeps its own handling and an unreadable
    directory is never mistaken for a missing file. File type is the caller's
    concern, and callers read the returned path rather than the joined one so
    a symlink cannot be followed past this check.
    """
    candidate = root.joinpath(*wire_path.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        # Resolve before checking containment so a symlink cannot make an
        # apparently relative graph path disclose a file outside the root.
        resolved.relative_to(root)
    except (FileNotFoundError, NotADirectoryError, ValueError, RuntimeError):
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            return None
        raise
    return resolved


def read_source_path(root: Path, wire_path: str, spans: list[tuple[int, int]]) -> dict[str, object]:
    """Read bounded line spans while keeping a graph path inside ``root``.

    ``wire_path`` is a repository-relative, slash-separated graph path and
    span endpoints are zero-based inclusive line numbers. The return shape is
    intentionally the visualizer's existing presentation payload so the
    source-reading policy has one owner for every consumer.
    """
    try:
        resolved = resolve_source_path(root, wire_path)
    except OSError:
        resolved = None
    if resolved is None:
        return {"status": "unavailable", "reason": "path is missing or escapes the source root"}
    try:
        content = resolved.read_bytes()
    except OSError as error:
        return {
            "status": "unavailable",
            "reason": f"source file is unreadable: {error.strerror or error}",
        }
    return read_source_bytes(wire_path, content, spans)


def read_source_bytes(
    wire_path: str, content: bytes, spans: Sequence[tuple[int, int]]
) -> dict[str, object]:
    """Render bounded line spans from already captured source *content*.

    A Python path is decoded with its declared encoding, exactly as analysis
    decodes it, and every other path is decoded as UTF-8; the excerpt lines
    are the decoded text's ``str.splitlines()`` lines. Comparison
    presentation must use this boundary after
    its source captures have been released.  Keeping byte decoding here makes
    it impossible for a later renderer call to silently fall back to a live
    checkout path.
    """
    if not isinstance(content, bytes):
        raise TypeError("captured source content must be bytes")
    if is_python_source_path(wire_path):
        try:
            text = decode_python_source(content)
        except UnicodeError as error:
            return {"status": "unavailable", "reason": f"source file cannot be decoded: {error}"}
    else:
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
        try:
            resolved = resolve_source_path(source_root, wire_path)
            if resolved is None:
                continue
            if not resolved.is_file():
                continue
            captured[wire_path] = bytes(resolved.read_bytes())
        except OSError:
            continue
    return MappingProxyType(captured)


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
