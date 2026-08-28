"""Shared path-resolution helpers for native language interpreters."""

from __future__ import annotations


def resolve_relative(
    base_parts: tuple[str, ...], levels_up: int
) -> tuple[str, ...] | None:
    """Return the base prefix after ascending, or ``None`` on root escape."""
    if levels_up > len(base_parts):
        return None
    return base_parts[: len(base_parts) - levels_up]
