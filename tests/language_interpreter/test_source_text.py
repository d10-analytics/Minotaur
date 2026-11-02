from __future__ import annotations

import time
import tracemalloc

import pytest

from minotaur.graph_model.location import Position
from minotaur.language_interpreter.source_text import LineIndex

MIXED_WIDTHS = "aé€😀b"

CORPUS = [
    ("", (0,)),
    ("a", (0,)),
    ("\r", (0, 1)),
    ("trail\r", (0, 6)),
    ("a\nb\n", (0, 2, 4)),
    ("a\r\nb\rc\nd", (0, 3, 5, 7)),
    ("x\fy\x85z w v", (0,)),
    ("é\n😀a\r\n€", (0, 2, 6)),
    ("a\ud800b\nc", (0, 4)),
    ("abc" + MIXED_WIDTHS + "def", (0,)),
]


def _expected_position(source: str, starts: tuple[int, ...], offset: int) -> Position:
    line = max(index for index, start in enumerate(starts) if start <= offset)
    prefix = source[starts[line] : offset]
    return Position(line, len(prefix.encode("utf-8", "surrogatepass")))


@pytest.mark.parametrize(("source", "starts"), CORPUS)
def test_line_starts_and_every_offset_match_independent_utf8_columns(
    source: str, starts: tuple[int, ...]
) -> None:
    index = LineIndex(source)

    assert index.line_starts == starts
    for offset in range(len(source) + 1):
        assert index.position(offset) == _expected_position(source, starts, offset), offset


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a€", Position(0, 4)),
        ("a€\n", Position(0, 4)),
        ("a€\r", Position(0, 4)),
        ("a€\r\n", Position(0, 4)),
        ("a\n€😀\r\n", Position(1, 7)),
        ("", Position(0, 0)),
    ],
)
def test_end_position_excludes_the_final_terminator(source: str, expected: Position) -> None:
    assert LineIndex(source).end_position() == expected


@pytest.mark.parametrize("offset", [-1, len("a€😀") + 1])
def test_offsets_outside_the_source_raise(offset: int) -> None:
    with pytest.raises(ValueError, match="offset outside source"):
        LineIndex("a€😀").position(offset)


def test_indexes_of_the_same_source_are_equal_and_hashable() -> None:
    first = LineIndex("aé\n€😀")
    second = LineIndex("aé\n€😀")

    assert first == second
    assert hash(first) == hash(second)
    assert first != LineIndex("aé\n€😀x")
    assert "wide_offsets" not in repr(first)
    assert "wide_extra" not in repr(first)


def test_construction_and_positions_never_slice_non_ascii_source() -> None:
    class SliceCountingSource(str):
        slices = 0

        def __getitem__(self, key):  # type: ignore[no-untyped-def]
            if isinstance(key, slice):
                type(self).slices += 1
            return super().__getitem__(key)

    plain = "aé€😀b\ncé😀\r\nd€x"
    index = LineIndex(SliceCountingSource(plain))
    starts = index.line_starts
    for offset in range(len(plain) + 1):
        assert index.position(offset) == _expected_position(plain, starts, offset), offset
    assert index.end_position() == Position(2, 5)

    assert SliceCountingSource.slices == 0


def _retained_growth(source: str) -> int:
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        index = LineIndex(source)
        retained = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert index.position(len(source)).line > 0
    return retained


def test_retained_memory_stays_within_four_bytes_per_character() -> None:
    ascii_line = "x" * 37 + "\n"
    mixed_line = "é€😀" + "x" * 34 + "\n"
    ascii_source = ascii_line * (100_000 // len(ascii_line))
    mixed_source = mixed_line * (100_000 // len(mixed_line))

    for source in (ascii_source, mixed_source):
        assert len(source) >= 99_000
        assert _retained_growth(source) <= 4 * len(source)


@pytest.mark.slow
def test_position_cost_does_not_grow_with_non_ascii_line_length() -> None:
    """A column at the end of a long non-ASCII line costs what one near its start does.

    The slice-count test rejects re-encoding a slice of the line; this ratio also
    rejects scanning the line without slicing it on every call.
    """
    source = "a€" * 25_000
    index = LineIndex(source)
    near = range(1, 501)
    far = range(len(source) - 500, len(source))

    def fastest(offsets: range) -> float:
        best = float("inf")
        for _ in range(5):
            started = time.perf_counter()
            for offset in offsets:
                index.position(offset)
            best = min(best, time.perf_counter() - started)
        return best

    near_cost = fastest(near)
    far_cost = fastest(far)

    assert index.position(len(source)) == Position(0, 4 * 25_000)
    assert far_cost <= 20 * near_cost, (near_cost, far_cost)
