"""Read-only, immutable source captures for revision comparisons.

The comparison command must analyze source belonging to a resolved revision
without borrowing the caller's checkout. A :class:`RevisionSnapshot` therefore
pins a local revision once, materializes exact tree/blob bytes into a unique
temporary directory, and records a manifest of that captured directory. The
manifest is checked before the snapshot is consumed and again on cleanup; a
mutation is an input error rather than a mixed or partial comparison.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from minotaur.git import GitInputError, PinnedCommit


class SnapshotError(ValueError):
    """A revision could not be captured as an immutable local input."""

    def __init__(self, *, side: str, revision: str, detail: str, commit: str | None = None):
        self.side = side
        self.revision = revision
        self.commit = commit
        self.pinned_sha = commit
        super().__init__(f"{side} revision {revision!r}: {detail}")


class SnapshotMutationError(SnapshotError):
    """The temporary captured source changed during comparison."""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Stable metadata and content digest for one captured path."""

    path: str
    kind: str
    mode: int
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class CaptureManifest:
    """The complete deterministic inventory of a captured source root."""

    entries: tuple[ManifestEntry, ...]

    def by_path(self) -> dict[str, ManifestEntry]:
        return {entry.path: entry for entry in self.entries}


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _manifest(root: Path) -> CaptureManifest:
    """Inventory ordinary directories and files without following links."""
    entries: list[ManifestEntry] = []
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        retained_directories: list[str] = []
        for name in directory_names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise SnapshotError(
                    side="capture",
                    revision="captured",
                    detail=f"captured path is not an ordinary directory: {relative}",
                )
            retained_directories.append(name)
            entries.append(ManifestEntry(relative, "directory", stat.S_IMODE(info.st_mode), 0, ""))
        directory_names[:] = retained_directories
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SnapshotError(
                    side="capture",
                    revision="captured",
                    detail=f"captured path is not an ordinary file: {relative}",
                )
            size, digest = _digest_file(path)
            entries.append(
                ManifestEntry(relative, "file", stat.S_IMODE(info.st_mode), size, digest)
            )
    return CaptureManifest(tuple(sorted(entries, key=lambda entry: entry.path)))


def _tree_files(pin: PinnedCommit) -> tuple[tuple[str, int], ...]:
    """Preflight a pinned tree before creating any captured files.

    Walking Git trees and reading blobs directly avoids ``git archive``
    attributes such as ``export-ignore`` and ``export-subst``.  The preflight
    also rejects links, gitlinks, and unsupported modes before materialization.
    """
    files: list[tuple[str, int]] = []

    def visit(relative: str = "") -> None:
        for entry in pin.entries(relative):
            if entry.is_gitlink:
                raise SnapshotError(
                    side=pin.side,
                    revision=pin.commit,
                    commit=pin.commit,
                    detail=f"unsafe entry {entry.path!r}: Gitlink is not a source directory",
                )
            if entry.is_link:
                raise SnapshotError(
                    side=pin.side,
                    revision=pin.commit,
                    commit=pin.commit,
                    detail=f"unsafe entry {entry.path!r}: symbolic links are not captured",
                )
            if entry.kind == "tree":
                visit(entry.path)
            elif entry.is_regular_file:
                files.append((entry.path, int(entry.mode, 8)))
            else:
                raise SnapshotError(
                    side=pin.side,
                    revision=pin.commit,
                    commit=pin.commit,
                    detail=f"unsafe entry {entry.path!r}: unsupported Git mode {entry.mode}",
                )

    visit()
    return tuple(files)


def _materialize_tree(pin: PinnedCommit, destination: Path) -> None:
    """Write exact pinned blob bytes into the temporary analysis root."""
    for relative, mode in _tree_files(pin):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pin.read_blob(relative))
        os.chmod(target, mode & 0o7777)


@dataclass(slots=True)
class RevisionSnapshot:
    """One pinned revision captured outside the user's checkout."""

    pin: PinnedCommit
    revision: str
    side: str
    temporary_root: Path
    manifest: CaptureManifest
    _temporary_directory: tempfile.TemporaryDirectory[str]
    _closed: bool = False

    @property
    def root(self) -> Path:
        """Return the temporary source root used by analysis."""
        return self.temporary_root

    @property
    def commit(self) -> str:
        return self.pin.commit

    @property
    def pinned_sha(self) -> str:
        return self.pin.commit

    @property
    def checkout_root(self) -> Path:
        """Compatibility name that emphasizes this is not a Git checkout."""
        return self.temporary_root

    def path(self, relative: str | Path = ".") -> Path:
        """Return a captured path, rejecting lexical escape from the root."""
        candidate = Path(relative)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise SnapshotError(
                side=self.side,
                revision=self.revision,
                commit=self.commit,
                detail=f"captured path escapes the temporary root: {relative}",
            )
        result = (self.temporary_root / candidate).resolve()
        try:
            result.relative_to(self.temporary_root.resolve())
        except ValueError as error:
            raise SnapshotError(
                side=self.side,
                revision=self.revision,
                commit=self.commit,
                detail=f"captured path escapes the temporary root: {relative}",
            ) from error
        return result

    def verify_unchanged(self) -> None:
        """Reject any mutation of the captured tree since acquisition."""
        if self._closed:
            return
        try:
            current = _manifest(self.temporary_root)
        except SnapshotError as error:
            raise SnapshotMutationError(
                side=self.side,
                revision=self.revision,
                commit=self.commit,
                detail=str(error),
            ) from error
        if current != self.manifest:
            before = self.manifest.by_path()
            after = current.by_path()
            changed = sorted(set(before) | set(after))
            path = next((item for item in changed if before.get(item) != after.get(item)), ".")
            raise SnapshotMutationError(
                side=self.side,
                revision=self.revision,
                commit=self.commit,
                detail=f"captured input changed at {path!r}",
            )

    def close(self) -> None:
        """Verify and remove the temporary capture exactly once."""
        if self._closed:
            return
        try:
            self.verify_unchanged()
        finally:
            self._temporary_directory.cleanup()
            self._closed = True

    def __enter__(self) -> RevisionSnapshot:
        self.verify_unchanged()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def capture_revision(
    worktree_root: Path,
    revision: str,
    *,
    side: str = "historical",
) -> RevisionSnapshot:
    """Capture a locally available revision without changing Git state."""
    root = Path(worktree_root).resolve()
    pin = PinnedCommit.resolve(root, revision, side=side)
    temporary_directory = tempfile.TemporaryDirectory(
        prefix="minotaur-revision-", ignore_cleanup_errors=True
    )
    destination = Path(temporary_directory.name)
    try:
        _materialize_tree(pin, destination)
        manifest = _manifest(destination)
    except Exception:
        temporary_directory.cleanup()
        raise
    return RevisionSnapshot(pin, revision, side, destination, manifest, temporary_directory)


def capture_local_revision(
    worktree_root: Path, revision: str, *, side: str = "historical"
) -> RevisionSnapshot:
    """Explicit alias for :func:`capture_revision` used by command owners."""
    return capture_revision(worktree_root, revision, side=side)


def capture_working_tree(worktree_root: Path, *, side: str = "after") -> RevisionSnapshot:
    """Capture the current working tree, including ordinary untracked files.

    The comparison analyzes this copy rather than the live checkout. Git
    administrative entries are excluded, while links and special files are
    omitted so a path cannot escape the captured side. Configured unsafe paths
    are rejected by side-specific route validation before production. The
    pinned ``HEAD`` is retained only as a stable identity for diagnostics; the
    manifest guards the copied working-tree bytes during analysis.
    """
    root = Path(worktree_root).resolve()
    pin = PinnedCommit.resolve(root, "HEAD", side=side)
    temporary_directory = tempfile.TemporaryDirectory(
        prefix="minotaur-working-", ignore_cleanup_errors=True
    )
    destination = Path(temporary_directory.name)
    try:
        for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                if name == ".git":
                    continue
                source = current_path / name
                info = source.lstat()
                if stat.S_ISLNK(info.st_mode):
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                retained_directories.append(name)
            directory_names[:] = retained_directories
            relative_directory = current_path.relative_to(root)
            (destination / relative_directory).mkdir(parents=True, exist_ok=True)
            for name in sorted(file_names):
                if name == ".git":
                    continue
                source = current_path / name
                info = source.lstat()
                relative = source.relative_to(root)
                if not stat.S_ISREG(info.st_mode):
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                os.chmod(target, stat.S_IMODE(info.st_mode))
        manifest = _manifest(destination)
    except Exception:
        temporary_directory.cleanup()
        raise
    return RevisionSnapshot(pin, "WORKTREE", side, destination, manifest, temporary_directory)


def capture_pair(
    worktree_root: Path,
    before: str,
    after: str,
) -> tuple[RevisionSnapshot, RevisionSnapshot]:
    """Capture both sides with independently pinned, side-attributed errors."""
    first = capture_revision(worktree_root, before, side="before")
    try:
        second = capture_revision(worktree_root, after, side="after")
    except Exception:
        first.close()
        raise
    return first, second


__all__ = [
    "CaptureManifest",
    "GitInputError",
    "ManifestEntry",
    "RevisionSnapshot",
    "SnapshotError",
    "SnapshotMutationError",
    "capture_local_revision",
    "capture_pair",
    "capture_revision",
    "capture_working_tree",
]
