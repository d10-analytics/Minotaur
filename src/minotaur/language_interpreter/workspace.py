"""Validated workspace roots for language interpreters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Workspace:
    """A local source root whose paths become graph-relative paths."""

    root: Path

    def __post_init__(self) -> None:
        resolved = self.root.resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace root must be an existing directory: {self.root}")
        object.__setattr__(self, "root", resolved)
