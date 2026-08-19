"""Validated workspace roots for language interpreters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Workspace:
    """A local source root whose paths become graph-relative graph paths.

    Resolving once at construction gives every interpreter the same physical
    root for containment and relative-path calculations. Without that shared
    canonical root, a symlink or ``..`` in one caller's path could make two
    analyses assign different graph identities to the same source file.
    """

    root: Path

    def __post_init__(self) -> None:
        # Fail before discovery rather than letting each interpreter report a
        # confusing empty graph for a misspelled or file-valued workspace root.
        resolved = self.root.resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace root must be an existing directory: {self.root}")
        object.__setattr__(self, "root", resolved)
