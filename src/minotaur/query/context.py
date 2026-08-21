"""Source context query for one analyzed file and line."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import is_safe_path
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import NodeClass
from minotaur.query.freshness import content_sha256
from minotaur.query.render import dump_json
from minotaur.source import read_source_path


@dataclass(frozen=True, slots=True)
class ContextLine:
    """One displayed source line, with the requested site marked."""

    line: int
    text: str
    target: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"line": self.line, "target": self.target, "text": self.text}


@dataclass(frozen=True, slots=True)
class ContextRecord:
    """The bounded context around one source site."""

    path: str
    lines: tuple[ContextLine, ...]
    stale: bool = False
    hash_available: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "hash_available": self.hash_available,
            "lines": [line.to_dict() for line in self.lines],
            "path": self.path,
            "stale": self.stale,
        }


def context(
    document: GraphDocument,
    root: Path,
    site: str,
    *,
    before: int = 3,
    after: int = 3,
) -> ContextRecord:
    """Read context around ``site`` without refreshing the graph.

    The graph's recorded file hash is compared with the current bytes, while
    the displayed text always comes from the current source tree. This keeps
    the command useful for an agent inspecting an edit and makes stale data
    explicit instead of silently presenting an excerpt as analyzed evidence.
    """
    if before < 0 or after < 0:
        raise ValueError("before and after must be non-negative")
    path, target_line = parse_site(site)
    if not is_safe_path(path):
        raise ValueError(f"site path must be a repository-relative path: {path}")

    workspace_root = root.resolve(strict=True)
    if not workspace_root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    file_node = _file_node(document, path)
    candidate = workspace_root.joinpath(*path.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace_root)
    except (OSError, ValueError) as error:
        raise ValueError(f"site path is missing or escapes the source root: {path}") from error
    if not resolved.is_file():
        raise ValueError(f"site path is not a file: {path}")

    expected_hash = content_sha256(file_node)
    hash_available = expected_hash is not None
    try:
        actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"site path is unreadable: {error.strerror or error}") from error
    stale = hash_available and expected_hash != actual_hash

    start = target_line - 1 - before
    end = target_line - 1 + after
    payload = read_source_path(workspace_root, path, [(start, end)])
    if payload.get("status") != "available":
        reason = payload.get("reason", "source is unavailable")
        raise ValueError(str(reason))
    spans = payload.get("spans")
    if not isinstance(spans, list) or len(spans) != 1:
        raise ValueError(f"site line is outside source file: {site}")
    span = spans[0]
    if not isinstance(span, dict) or not isinstance(span.get("start"), int):
        raise ValueError(f"invalid source excerpt for site: {site}")
    raw_lines = span.get("lines")
    if not isinstance(raw_lines, list) or not all(isinstance(line, str) for line in raw_lines):
        raise ValueError(f"invalid source excerpt for site: {site}")
    first_line = span["start"] + 1
    last_line = first_line + len(raw_lines) - 1
    if target_line < first_line or target_line > last_line:
        raise ValueError(f"site line is outside source file: {site}")
    lines = tuple(
        ContextLine(line=first_line + offset, text=text, target=first_line + offset == target_line)
        for offset, text in enumerate(raw_lines)
    )
    return ContextRecord(
        path=path,
        lines=lines,
        stale=stale,
        hash_available=hash_available,
    )


def parse_site(value: str) -> tuple[str, int]:
    """Parse a root-relative ``path:line`` site using one-based line numbers."""
    path, separator, line = value.rpartition(":")
    if not separator or not path:
        raise ValueError("site must have the form path:line")
    try:
        target_line = int(line)
    except ValueError as error:
        raise ValueError("site line must be a positive integer") from error
    if target_line < 1:
        raise ValueError("site line must be a positive integer")
    return path, target_line


def render_text(record: ContextRecord) -> str:
    """Render context as compact numbered lines with a ``>`` target marker."""
    output: list[str] = []
    if record.stale:
        output.append("[file changed since analysis]\n")
    elif not record.hash_available:
        output.append("[file hash unavailable]\n")
    output.append(f"{record.path}:{record.lines[0].line}-{record.lines[-1].line}\n")
    output.extend(
        f"{'>' if line.target else ' '} {line.line}: {line.text}\n" for line in record.lines
    )
    return "".join(output)


def render_json(record: ContextRecord) -> str:
    """Render the same context records used by text output."""
    return dump_json({"query": "context", "results": [record.to_dict()]})


def _file_node(document: GraphDocument, path: str) -> Node:
    for node in document.nodes:
        if node.node_class == NodeClass.FILE and node.path == path:
            return node
    raise ValueError(f"site path is not present in graph: {path}")
