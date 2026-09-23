"""Shared vocabulary for resolved SQL dependency relationships."""

from __future__ import annotations

# Keep this list explicit: namespaced SQL relationships are not all
# dependencies, so consumers must opt into each meaning deliberately.
CURRENT_SQL_DEPENDENCY_KINDS: tuple[str, str] = (
    "sql:reads-from",
    "sql:foreign-key-to",
)

__all__ = ["CURRENT_SQL_DEPENDENCY_KINDS"]
