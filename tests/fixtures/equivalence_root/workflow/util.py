"""Small helpers shared by the fixture workflow."""

from __future__ import annotations

from collections.abc import Sequence


def format_report(values: Sequence[int]) -> str:
    """Render *values* as one stable line."""

    return "report: " + ", ".join(str(value) for value in values)


def summarize(values: Sequence[int]) -> int:
    """Return the total of *values*."""

    return sum(values)


def unused_helper(value: int) -> int:
    """Never called from anywhere: the fixture's unreferenced symbol."""

    return value * 2
