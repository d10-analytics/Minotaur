"""Efficient source-text position conversion shared by language interpreters."""

from __future__ import annotations

import re
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from minotaur.graph_model.location import Position, encoded_length, split_lines
from minotaur.graph_model.provenance import CoordinateEncoding

_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


@dataclass(frozen=True, slots=True)
class LineIndex:
    """Map parser character offsets to UTF-8 document positions.

    Storage grows with the number of non-ASCII characters: ``wide_offsets``
    holds the character offset of each one and ``wide_extra`` the cumulative
    count of UTF-8 bytes beyond one per character through it. A position is a
    binary search over those tables, so no call touches the source text.
    """

    source: str
    line_starts: tuple[int, ...]
    wide_offsets: array[int] = field(compare=False, repr=False)
    wide_extra: array[int] = field(compare=False, repr=False)

    def __init__(self, source: str) -> None:
        starts = [0]
        cursor = 0
        lines = split_lines(source)
        for line in lines[:-1]:
            cursor += len(line)
            if source.startswith("\r\n", cursor):
                cursor += 2
            else:
                cursor += 1
            starts.append(cursor)
        offsets: array[int] = array("Q")
        extra: array[int] = array("Q")
        total = 0
        for match in _NON_ASCII_RE.finditer(source):
            total += encoded_length(match.group(), CoordinateEncoding.UTF_8) - 1
            offsets.append(match.start())
            extra.append(total)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "line_starts", tuple(starts))
        object.__setattr__(self, "wide_offsets", offsets)
        object.__setattr__(self, "wide_extra", extra)

    def _byte_offset(self, offset: int) -> int:
        """Return the UTF-8 byte offset of a character offset from the source start."""
        wide_before = bisect_left(self.wide_offsets, offset)
        return offset + (self.wide_extra[wide_before - 1] if wide_before else 0)

    def position(self, offset: int) -> Position:
        """Return the UTF-8 position corresponding to a source character offset."""
        if offset < 0 or offset > len(self.source):
            raise ValueError(f"offset outside source: {offset}")
        line = bisect_right(self.line_starts, offset) - 1
        return Position(
            line,
            self._byte_offset(offset) - self._byte_offset(self.line_starts[line]),
        )

    def end_position(self) -> Position:
        """Return the end of the final content line, excluding its terminator."""
        offset = len(self.source)
        if self.source.endswith("\r\n"):
            offset -= 2
        elif self.source.endswith(("\r", "\n")):
            offset -= 1
        return self.position(offset)
