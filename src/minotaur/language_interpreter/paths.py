"""Shared path-resolution helpers for native language interpreters."""

from __future__ import annotations

from minotaur.graph_model.location import is_safe_path


def resolve_relative(base_parts: tuple[str, ...], levels_up: int) -> tuple[str, ...] | None:
    """Return the base prefix after ascending, or ``None`` on root escape."""
    if levels_up > len(base_parts):
        return None
    return base_parts[: len(base_parts) - levels_up]


def is_graph_path(relative: str) -> bool:
    """Return whether a root-relative path can be recorded as a graph path.

    The path must satisfy the graph safe-path contract and be strictly
    encodable as UTF-8, so a file name holding undecodable bytes (surfaced as
    lone surrogates) is rejected.
    """
    if not is_safe_path(relative):
        return False
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
