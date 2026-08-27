"""Efficient source-text position conversion shared by language interpreters."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from minotaur.graph_model.location import Position, encoded_length, split_lines
from minotaur.graph_model.provenance import CoordinateEncoding


@dataclass(frozen=True, slots=True)
class LineIndex:
    """Map parser character offsets to UTF-8 document positions in linear time."""

    source: str
    line_starts: tuple[int, ...]
    byte_prefix: tuple[int, ...]

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
        prefix = [0]
        for character in source:
            prefix.append(prefix[-1] + encoded_length(character, CoordinateEncoding.UTF_8))
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "line_starts", tuple(starts))
        object.__setattr__(self, "byte_prefix", tuple(prefix))

    def position(self, offset: int) -> Position:
        """Return the UTF-8 position corresponding to a source character offset."""
        if offset < 0 or offset > len(self.source):
            raise ValueError(f"offset outside source: {offset}")
        line = bisect_right(self.line_starts, offset) - 1
        return Position(line, self.byte_prefix[offset] - self.byte_prefix[self.line_starts[line]])

    def end_position(self) -> Position:
        """Return the end of the final content line, excluding its terminator."""
        offset = len(self.source)
        if self.source.endswith("\r\n"):
            offset -= 2
        elif self.source.endswith(("\r", "\n")):
            offset -= 1
        return self.position(offset)
