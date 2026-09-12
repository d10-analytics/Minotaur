"""Small, tolerant Git probes shared by configuration and CLI owners."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def run_git(
    root: Path,
    arguments: Sequence[str],
    *,
    text: bool = True,
) -> subprocess.CompletedProcess[Any] | None:
    """Run a Git probe, treating unavailable or failed execution as unknown."""
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            check=False,
            text=text,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def work_tree_root(start: Path) -> Path | None:
    """Return the enclosing work-tree root, or ``None`` for an unknown probe."""
    completed = run_git(start, ("rev-parse", "--show-toplevel"))
    if completed is None or completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value).resolve() if value else None


def read_head_blob(root: Path, relative_path: str) -> bytes | None:
    """Read exact bytes from ``HEAD``; ``None`` means no such committed path.

    Git command/probe failures are also represented as ``None``. Callers that
    already established a work tree treat that as a strict artifact error;
    callers deciding whether Git is available use :func:`work_tree_root`.
    """
    try:
        completed = subprocess.run(
            ["git", "show", f"HEAD:{relative_path}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


class GitInputError(ValueError):
    """A strict historical Git observation could not be completed."""

    def __init__(
        self,
        *,
        side: str,
        commit: str | None,
        path: str,
        detail: str,
        cause: str | None = None,
    ) -> None:
        self.side = side
        self.historical_side = side
        self.commit = commit
        self.pinned_sha = commit
        self.path = path
        self.cause = cause
        identity = f"commit {commit}" if commit else cause or "unavailable probe"
        super().__init__(f"{side} Git input ({identity}) at {path!r}: {detail}")


def _error_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip() or "Git operation failed"
    return value.strip() if value else "Git operation failed"


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One immutable entry observed in a pinned Git tree."""

    path: str
    mode: str
    kind: str

    @property
    def is_regular_file(self) -> bool:
        return self.kind == "blob" and self.mode in {"100644", "100755"}

    @property
    def is_link(self) -> bool:
        return self.mode == "120000"

    @property
    def is_gitlink(self) -> bool:
        return self.mode == "160000"


@dataclass(frozen=True, slots=True)
class PinnedCommit:
    """A work tree and one immutable commit used for historical reads."""

    root: Path
    commit: str

    @classmethod
    def pin(cls, root: Path) -> PinnedCommit:
        """Resolve ``HEAD`` once, rejecting unavailable or unborn repositories."""
        completed = run_git(root, ("rev-parse", "--verify", "HEAD^{commit}"))
        if completed is None:
            raise GitInputError(
                side="historical",
                commit=None,
                path=str(root),
                detail="Git probe was unavailable",
                cause="unavailable Git probe",
            )
        stdout = completed.stdout
        if isinstance(stdout, bytes):
            commit = stdout.decode("utf-8", errors="replace").strip()
        else:
            commit = str(stdout).strip()
        if completed.returncode != 0 or not commit:
            raise GitInputError(
                side="historical",
                commit=None,
                path=str(root),
                detail="repository has no committed HEAD",
                cause="no committed HEAD",
            )
        return cls(root=root, commit=commit)

    def _error(self, path: str, detail: str) -> GitInputError:
        return GitInputError(side="historical", commit=self.commit, path=path, detail=detail)

    def _validate_relative(self, relative: str) -> tuple[str, ...]:
        if not relative or relative.startswith("/"):
            raise self._error(relative, "invalid committed path")
        parts = tuple(relative.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise self._error(relative, "invalid committed path")
        return parts

    def entries(self, relative: str = "") -> tuple[TreeEntry, ...]:
        """List direct entries in one tree from this pinned commit."""
        if relative:
            parts = self._validate_relative(relative)
            treeish = f"{self.commit}:{'/'.join(parts)}"
            display_path = relative
        else:
            treeish = self.commit
            display_path = "."
        completed = run_git(self.root, ("ls-tree", "-z", treeish), text=False)
        if completed is None:
            raise self._error(display_path, "Git tree listing probe was unavailable")
        if completed.returncode != 0:
            raise self._error(display_path, _error_text(completed.stderr))
        output = completed.stdout
        if not isinstance(output, bytes):
            raise self._error(display_path, "Git tree listing did not return bytes")
        result: list[TreeEntry] = []
        for raw in output.split(b"\0"):
            if not raw:
                continue
            header, separator, name_bytes = raw.partition(b"\t")
            if not separator:
                raise self._error(display_path, "invalid Git tree entry")
            fields = header.split()
            if len(fields) != 3:
                raise self._error(display_path, "invalid Git tree entry")
            try:
                mode = fields[0].decode("ascii")
                kind = fields[1].decode("ascii")
            except UnicodeDecodeError:
                raise self._error(display_path, "invalid Git tree entry") from None
            name = name_bytes.decode("utf-8", errors="surrogateescape")
            path = f"{relative}/{name}" if relative else name
            result.append(TreeEntry(path=path, mode=mode, kind=kind))
        return tuple(sorted(result, key=lambda entry: entry.path))

    def entry(self, relative: str) -> TreeEntry | None:
        """Find one pinned entry, distinguishing confirmed absence from failure."""
        parts = self._validate_relative(relative)
        parent = ""
        current: TreeEntry | None = None
        for index, part in enumerate(parts):
            candidates = {item.path.rsplit("/", 1)[-1]: item for item in self.entries(parent)}
            current = candidates.get(part)
            if current is None:
                return None
            if index < len(parts) - 1:
                if current.is_link or current.is_gitlink or current.kind != "tree":
                    raise self._error(
                        relative,
                        "path traverses a non-tree entry",
                    )
                parent = current.path
        return current

    def read_blob(self, relative: str) -> bytes:
        """Read exact bytes from a pinned regular-file blob."""
        item = self.entry(relative)
        if item is None:
            raise self._error(relative, "path is absent")
        if item.is_link:
            raise self._error(relative, "path is a symbolic link")
        if item.is_gitlink:
            raise self._error(relative, "path is a gitlink")
        if not item.is_regular_file:
            raise self._error(relative, "path is not a regular file")
        completed = run_git(self.root, ("show", f"{self.commit}:{relative}"), text=False)
        if completed is None:
            raise self._error(relative, "Git blob read probe was unavailable")
        if completed.returncode != 0:
            raise self._error(relative, _error_text(completed.stderr))
        output = completed.stdout
        if not isinstance(output, bytes):
            raise self._error(relative, "Git blob read did not return bytes")
        return output
