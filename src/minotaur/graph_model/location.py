"""Source-location model for Minotaur graph documents.

Locations anchor nodes and evidence to specific positions in source files.
The v1 contract requires locations to be complete when present — partial
locations (path without range, or range without both endpoints) are never
valid. This "all-or-nothing" rule exists because a partial location would
be ambiguous: a consumer cannot distinguish "the location is lines 1–5"
from "we only know the start line" without an explicit contract, and
Minotaur chose to avoid that ambiguity rather than model it.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from minotaur.graph_model._parsing import reject_unknown_fields

# The v1 schema requires paths that are:
#   - non-empty
#   - not absolute (no leading /)
#   - no empty segments (no //)
#   - no . or .. components
#   - no backslashes (paths are slash-separated on every host)
#   - no NUL bytes
#
# This protects consumers from directory traversal and ensures the path
# is meaningful as a repository-relative identifier. The regex matches
# the JSON Schema `safePath` pattern exactly so that Python validation
# rejects the same inputs the schema does.
# The JSON Schema encodes this as "^(?!/)(?!.*//)(?!.*(?:^|/)\\.{1,2}(?:/|$))[^\\u0000]+$"
# where \\. is JSON-escaped \.  (literal dot in regex). In a Python raw
# string the equivalent is \. without the extra backslash.
_SAFE_PATH_RE = re.compile(
    r"^(?!/)(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*\\)[^\x00]+$"
)


def is_safe_path(path: str) -> bool:
    """Check whether a path satisfies the v1 safe-path contract."""
    return bool(path) and _SAFE_PATH_RE.match(path) is not None


@functools.total_ordering
@dataclass(frozen=True, slots=True)
class Position:
    """A zero-based line and character offset within a source file.

    Zero-based indexing follows the LSP (Language Server Protocol) convention,
    which is the dominant standard for code-intelligence tooling. The v1 schema
    stores these as non-negative integers; the character offset's meaning depends
    on the document-level `coordinate_encoding` (utf-8, utf-16, or utf-32).
    User-facing displays convert to one-based values; the model stores the wire
    values unchanged.
    """

    line: int
    character: int

    def __post_init__(self) -> None:
        # Enforced at construction, not just at validation time, because a
        # negative position is never structurally meaningful — it cannot be
        # an "unchecked input we'll validate later."
        if self.line < 0:
            raise ValueError(f"line must be non-negative, got {self.line}")
        if self.character < 0:
            raise ValueError(f"character must be non-negative, got {self.character}")

    def to_dict(self) -> dict[str, int]:
        return {"line": self.line, "character": self.character}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Position:
        reject_unknown_fields(data, frozenset({"line", "character"}), "position")
        return cls(line=_require_int(data, "line"), character=_require_int(data, "character"))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Position):
            return NotImplemented
        return (self.line, self.character) == (other.line, other.character)

    def __lt__(self, other: object) -> bool:
        """Positions compare by (line, character) for range-ordering validation.

        The semantic validator needs to confirm that a range's end is not
        before its start. A simple tuple comparison on (line, character)
        correctly models document order. @total_ordering derives __le__,
        __gt__, and __ge__ from __eq__ and __lt__ so all six comparisons
        are consistent.
        """
        if not isinstance(other, Position):
            return NotImplemented
        return (self.line, self.character) < (other.line, other.character)


@dataclass(frozen=True, slots=True)
class Range:
    """A half-open source range from start (inclusive) to end (exclusive).

    Half-open ranges are standard in LSP and most editor protocols. They
    compose cleanly: two adjacent ranges share an endpoint without overlap,
    and a zero-width range (start == end) meaningfully represents a cursor
    position or insertion point.

    The semantic validator enforces that end >= start (not strictly greater,
    because a zero-width range at a cursor position is valid). This class
    stores the values; validation happens in the validation module so that
    all errors are reported together rather than failing at construction.
    """

    start: Position
    end: Position

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Range:
        reject_unknown_fields(data, frozenset({"start", "end"}), "range")
        start_data = data.get("start")
        end_data = data.get("end")
        if not isinstance(start_data, dict):
            raise ValueError("range requires a 'start' object")
        if not isinstance(end_data, dict):
            raise ValueError("range requires an 'end' object")
        return cls(start=Position.from_dict(start_data), end=Position.from_dict(end_data))


@dataclass(frozen=True, slots=True)
class Location:
    """A source file path with a specific range within that file.

    Locations are the primary evidence anchor in Minotaur. They connect
    abstract graph relationships to inspectable source positions so that
    a consumer can verify a claimed relationship by reading the code at
    that location.

    The path is repository-relative and slash-separated regardless of the
    host OS. This ensures graph documents are portable across platforms
    — a graph generated on Windows is readable on Linux without path
    translation.
    """

    path: str
    range: Range

    def __post_init__(self) -> None:
        # Path safety is enforced at construction because an unsafe path
        # (absolute, with traversal components, or empty) is never a valid
        # repository-relative reference. Letting one through would require
        # every downstream consumer to re-check.
        if not is_safe_path(self.path):
            raise ValueError(
                f"path must be a non-empty, repository-relative, slash-separated "
                f"path with no absolute, empty, or dot components, got {self.path!r}"
            )

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "range": self.range.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Location:
        reject_unknown_fields(data, frozenset({"path", "range"}), "location")
        path = data.get("path")
        range_data = data.get("range")
        if not isinstance(path, str):
            raise ValueError("location requires a 'path' string")
        if not isinstance(range_data, dict):
            raise ValueError("location requires a 'range' object")
        return cls(path=path, range=Range.from_dict(range_data))

    @property
    def sort_key(self) -> tuple[str, int, int, int, int]:
        """Canonical sort key for deterministic location ordering.

        Locations sort by (path, start.line, start.character, end.line,
        end.character). This order is used both for canonical serialization
        and for the interactive visualizer's call-site selector, which
        presents call sites as numbered 1, 2, 3, ... in this order.
        """
        return (
            self.path,
            self.range.start.line,
            self.range.start.character,
            self.range.end.line,
            self.range.end.character,
        )


def _require_int(data: dict[str, object], key: str) -> int:
    """Extract a required integer field, rejecting strings, floats, and nulls.

    The v1 schema explicitly requires position values to be JSON integers.
    Python's json.loads decodes JSON integers as int and JSON floats as float,
    so a strict isinstance check catches the case where a producer sends
    "line": 4.0 instead of "line": 4. This matters because position 4.0
    would pass a loose numeric check but violates the schema and could cause
    subtle mismatches in node-ID computation.
    """
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"'{key}' must be an integer, got {type(value).__name__}: {value!r}")
    return value
