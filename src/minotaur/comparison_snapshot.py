"""Read-only, immutable source captures for revision comparisons.

The comparison command must analyze source belonging to a resolved revision
without borrowing the caller's checkout.  A :class:`RevisionSnapshot` therefore
pins a local revision once, captures its tree with ``git archive`` into a unique
temporary directory, and records a manifest of that captured directory.  The
manifest is checked before the snapshot is consumed and again on cleanup; a
mutation is an input error rather than a mixed or partial comparison.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from minotaur.git import GitInputError, PinnedCommit, run_git


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


def _error_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip() or "Git operation failed"
    return value.strip() if value else "Git operation failed"


def _safe_member_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"archive contains unsafe path {name!r}")
    return path


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


def _extract_archive(data: bytes, destination: Path, *, side: str, revision: str) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")  # noqa: SIM115
    except (tarfile.TarError, OSError) as error:
        raise SnapshotError(
            side=side,
            revision=revision,
            detail=f"Git archive was not a valid local tree: {error}",
        ) from error
    with archive:
        seen: set[str] = set()
        members = sorted(archive.getmembers(), key=lambda member: member.name)
        for member in members:
            try:
                relative = _safe_member_path(member.name)
            except ValueError as error:
                raise SnapshotError(side=side, revision=revision, detail=str(error)) from error
            key = relative.as_posix()
            if key in seen:
                raise SnapshotError(
                    side=side,
                    revision=revision,
                    detail=f"archive contains duplicate path {key!r}",
                )
            seen.add(key)
            if not (member.isdir() or member.isreg()):
                raise SnapshotError(
                    side=side,
                    revision=revision,
                    detail=f"archive contains unsafe entry {key!r}",
                )
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=False)
                os.chmod(target, member.mode & 0o7777)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise SnapshotError(
                    side=side,
                    revision=revision,
                    detail=f"archive file has no content: {key!r}",
                )
            with source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            os.chmod(target, member.mode & 0o7777)


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
    completed = run_git(root, ("archive", "--format=tar", pin.commit), text=False)
    if completed is None:
        raise SnapshotError(
            side=side,
            revision=revision,
            commit=pin.commit,
            detail="Git archive probe was unavailable",
        )
    if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
        raise SnapshotError(
            side=side,
            revision=revision,
            commit=pin.commit,
            detail=f"revision tree is unavailable locally: {_error_text(completed.stderr)}",
        )
    temporary_directory = tempfile.TemporaryDirectory(prefix="minotaur-revision-")
    destination = Path(temporary_directory.name)
    try:
        _extract_archive(completed.stdout, destination, side=side, revision=revision)
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
]
