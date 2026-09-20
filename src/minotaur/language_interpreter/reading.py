"""Read and parse selected source files for language interpreters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minotaur.graph_model.location import Location
from minotaur.language_interpreter.contract import Diagnostic, DiagnosticCode
from minotaur.language_interpreter.workspace import Workspace


@dataclass(frozen=True, slots=True)
class RawSource:
    """A successfully read source file, retaining bytes and decoded text."""

    relative: str
    content: bytes
    source: str


@dataclass(frozen=True, slots=True)
class ParsedSource:
    """A successfully read and parsed source file, retaining its raw bytes."""

    relative: str
    content: bytes
    source: str
    tree: Any


class ParseFailure(Exception):
    """A source parse failure with an optional source location."""

    message: str
    location: Location | None

    def __init__(self, message: str, location: Location | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.location = location


def read_sources(
    workspace: Workspace,
    files: tuple[Path, ...] | list[Path],
) -> tuple[list[RawSource], list[Diagnostic]]:
    """Read selected source files in deterministic root-relative order.

    Each file is read once and decoded with ``utf-8-sig``. Read and decoding
    failures are isolated to their source so eligible siblings remain usable.
    """
    sources: list[RawSource] = []
    diagnostics: list[Diagnostic] = []
    for path in sorted(files, key=lambda item: item.relative_to(workspace.root).as_posix()):
        relative = path.relative_to(workspace.root).as_posix()
        try:
            content = path.read_bytes()
            source = content.decode("utf-8-sig")
        except (OSError, UnicodeError) as error:
            diagnostics.append(Diagnostic(DiagnosticCode.SOURCE_READ_ERROR, relative, str(error)))
            continue
        sources.append(RawSource(relative, content, source))
    return sources, diagnostics


def read_and_parse(
    workspace: Workspace,
    files: tuple[Path, ...] | list[Path],
    parse: Callable[[str, str], Any],
) -> tuple[list[ParsedSource], list[Diagnostic]]:
    """Read and parse files in deterministic root-relative POSIX order.

    Files are sorted by their path relative to ``workspace.root`` so callers'
    selection order cannot affect emitted facts or diagnostics. A read,
    decoding, or parsing failure is reported for that file and does not stop
    processing its valid siblings.
    """
    sources: list[ParsedSource] = []
    raw_sources, diagnostics = read_sources(workspace, files)
    for raw in raw_sources:
        try:
            tree = parse(raw.source, raw.relative)
        except ParseFailure as failure:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.PARSE_ERROR,
                    raw.relative,
                    failure.message,
                    failure.location,
                )
            )
            continue
        sources.append(ParsedSource(raw.relative, raw.content, raw.source, tree))
    return sources, diagnostics
