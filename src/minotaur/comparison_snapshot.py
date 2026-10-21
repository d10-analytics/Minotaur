"""Read-only, immutable source captures for revision comparisons.

The comparison command must analyze source belonging to a resolved revision
without borrowing the caller's checkout. A :class:`RevisionSnapshot` therefore
pins a local revision once, materializes exact tree/blob bytes into a unique
temporary directory, and records a manifest of that captured directory. The
manifest is checked before the snapshot is consumed and again on cleanup; a
mutation is an input error rather than a mixed or partial comparison.

:func:`capture_pair` captures only the *comparison selection*, in two phases.
Each side is first opened as a :class:`CaptureStage`; a caller-supplied plan
stages the few files (configuration and discovery candidates) from which the
selection is derived and returns a :class:`CaptureSelection`. The union of that
selection and every path staged on either side is then included into both
stages, which are sealed (manifest built) together. A path is selected when any
selection entry admits it: an exact file, a directory captured as a directory
only (plus the route ancestors of every entry), or a subtree. A pruned subtree
omits what the analysis's own exclusion rule omits (``exclusions.is_excluded``
below the subtree), while an unpruned subtree keeps everything. A link, gitlink
or unsupported mode inside the selection is refused on a revision side; the
working-tree side skips links silently and never enters ``.git``. Nothing
outside the selection is listed for writing, read from the object store,
walked or copied.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

from minotaur.git import GitInputError, PinnedCommit, TreeEntry
from minotaur.language_interpreter import exclusions


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


def _raise_walk_error(error: OSError) -> None:
    """Keep filesystem traversal failures visible to the capture owner."""
    raise error


def _logical_mode(mode: int, *, kind: str) -> int:
    """Reduce a filesystem mode to the logical exec/non-exec model.

    Raw permission bits carry the creating process umask, so the same logical
    captured input would otherwise produce different manifests on different
    hosts. Directories always map to one canonical mode, and files keep only
    the executable distinction that Git itself records. A real executable-bit
    change still changes the manifest; ownership bits and umask-derived
    read/write bits do not.
    """
    if kind == "directory":
        return 0o755
    return 0o755 if mode & 0o111 else 0o644


def _manifest(root: Path) -> CaptureManifest:
    """Inventory ordinary directories and files without following links."""
    entries: list[ManifestEntry] = []
    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=_raise_walk_error
    ):
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
            entries.append(
                ManifestEntry(
                    relative,
                    "directory",
                    _logical_mode(stat.S_IMODE(info.st_mode), kind="directory"),
                    0,
                    "",
                )
            )
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
                ManifestEntry(
                    relative,
                    "file",
                    _logical_mode(stat.S_IMODE(info.st_mode), kind="file"),
                    size,
                    digest,
                )
            )
    return CaptureManifest(tuple(sorted(entries, key=lambda entry: entry.path)))


def _capture_os_error(
    *,
    side: str,
    revision: str,
    commit: str | None,
    path: str | Path,
    operation: str,
    error: OSError,
) -> SnapshotError:
    """Translate an unexpected capture I/O failure without hiding its cause."""
    return SnapshotError(
        side=side,
        revision=revision,
        commit=commit,
        detail=f"{operation} at {path!r}: {error}",
    )


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
        except OSError as error:
            raise SnapshotMutationError(
                side=self.side,
                revision=self.revision,
                commit=self.commit,
                detail=f"could not verify captured input: {error}",
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


_ROOT = "."
_T = TypeVar("_T")


def _coordinate(value: str) -> str:
    """Normalize a repository-relative POSIX coordinate; the root is ``"."``."""
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if value.startswith("/") or ".." in parts:
        raise ValueError(f"selection coordinate must be repository-relative: {value!r}")
    return "/".join(parts) or _ROOT


@dataclass(frozen=True, slots=True)
class CaptureSelection:
    """The repository paths a comparison captures.

    ``files`` are captured exactly as listed, ``directories`` as directories
    only, and ``trees`` map a subtree coordinate to its prune flag: a pruned
    subtree omits what ``exclusions.is_excluded`` omits below it, an unpruned
    one (``False``) keeps everything. The route ancestors of every coordinate
    are captured as directories (or as the file or link found at that
    position). The repository root is the coordinate ``"."``.
    """

    files: Collection[str] = ()
    directories: Collection[str] = ()
    trees: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        files = frozenset(_coordinate(item) for item in self.files) - {_ROOT}
        directories = frozenset(_coordinate(item) for item in self.directories)
        trees: dict[str, bool] = {}
        for item, prune in self.trees.items():
            coordinate = _coordinate(item)
            trees[coordinate] = trees.get(coordinate, True) and bool(prune)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "directories", directories)
        object.__setattr__(self, "trees", MappingProxyType(trees))

    def union(self, *others: CaptureSelection) -> CaptureSelection:
        """Combine selections; an unpruned subtree wins over a pruned one."""
        files: set[str] = set(self.files)
        directories: set[str] = set(self.directories)
        trees: dict[str, bool] = dict(self.trees)
        for other in others:
            files.update(other.files)
            directories.update(other.directories)
            for coordinate, prune in other.trees.items():
                trees[coordinate] = trees.get(coordinate, True) and prune
        return CaptureSelection(files, directories, trees)


class _Scope:
    """Answers which paths a selection admits and which directories to enter."""

    def __init__(self, selection: CaptureSelection) -> None:
        self._files = selection.files
        self._directories = selection.directories
        self._trees = dict(selection.trees)
        self._admitted: dict[str, bool] = {}
        # Directory -> names leading toward a selected coordinate.
        self.route: dict[str, set[str]] = {}
        for coordinate in (*self._files, *self._directories, *self._trees):
            parent = _ROOT
            for part in () if coordinate == _ROOT else coordinate.split("/"):
                self.route.setdefault(parent, set()).add(part)
                parent = part if parent == _ROOT else f"{parent}/{part}"

    def in_tree(self, path: str) -> bool:
        """Whether a selected subtree admits *path* (judged below its root)."""
        cached = self._admitted.get(path)
        if cached is not None:
            return cached
        result = False
        for coordinate, prune in self._trees.items():
            if coordinate == _ROOT:
                below = path
            elif path == coordinate:
                below = ""
            elif path.startswith(f"{coordinate}/"):
                below = path[len(coordinate) + 1 :]
            else:
                continue
            if not below or not prune or not exclusions.is_excluded(Path(below)):
                result = True
                break
        self._admitted[path] = result
        return result

    def admits(self, path: str) -> bool:
        return (
            path in self._files
            or path in self._directories
            or path in self.route
            or self.in_tree(path)
        )


def _unsafe(entry: TreeEntry) -> str | None:
    if entry.is_gitlink:
        return f"unsafe entry {entry.path!r}: Gitlink is not a source directory"
    if entry.is_link:
        return f"unsafe entry {entry.path!r}: symbolic links are not captured"
    if entry.kind != "tree" and not entry.is_regular_file:
        return f"unsafe entry {entry.path!r}: unsupported Git mode {entry.mode}"
    return None


class CaptureStage:
    """One side of a pair being captured into a private temporary directory.

    A plan receives the stage to stage files (``include``) and to read them
    from ``root``. ``include`` is write-once per path: bytes staged first are
    the bytes sealed. Sealing and discarding belong to :func:`capture_pair`.
    """

    def __init__(
        self,
        worktree: Path,
        pin: PinnedCommit,
        revision: str,
        *,
        working: bool,
    ) -> None:
        self._worktree = worktree
        self.pin = pin
        self.revision = revision
        self._working = working
        self._listing: tuple[TreeEntry, ...] | None = None
        self._written: set[str] = set()
        self._requested: set[str] = set()
        self._sealed = False
        prefix = "minotaur-working-" if working else "minotaur-revision-"
        try:
            self._directory = tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
        except OSError as error:
            raise _capture_os_error(
                side=pin.side,
                revision=revision,
                commit=pin.commit,
                path=worktree,
                operation=(
                    "could not create temporary working-tree capture"
                    if working
                    else "could not create temporary revision capture"
                ),
                error=error,
            ) from error
        self.root = Path(self._directory.name)

    @property
    def side(self) -> str:
        return self.pin.side

    @property
    def requested(self) -> frozenset[str]:
        """Every path passed to :meth:`include`, whether or not it exists."""
        return frozenset(self._requested)

    def include(self, paths: Iterable[str]) -> None:
        """Stage *paths* as listed; a missing path stages nothing.

        On a revision side the whole call reads its blobs with one batch.
        """
        if isinstance(paths, str):
            raise TypeError("include takes a collection of paths, not one string")
        coordinates = frozenset(_coordinate(item) for item in paths) - {_ROOT}
        self._requested |= coordinates
        self._stage(CaptureSelection(files=coordinates))

    def discard(self) -> None:
        """Remove the temporary directory without verification."""
        self._directory.cleanup()

    def seal(self) -> RevisionSnapshot:
        """Build the manifest and return the captured snapshot."""
        self._sealed = True
        revision = "WORKTREE" if self._working else self.revision
        try:
            manifest = _manifest(self.root)
        except SnapshotError:
            raise
        except OSError as error:
            raise _capture_os_error(
                side=self.pin.side,
                revision=revision,
                commit=self.pin.commit,
                path=self.root,
                operation=(
                    "could not finalize working-tree capture"
                    if self._working
                    else "could not finalize revision capture"
                ),
                error=error,
            ) from error
        return RevisionSnapshot(
            self.pin, revision, self.pin.side, self.root, manifest, self._directory
        )

    def _stage(self, selection: CaptureSelection) -> None:
        if self._sealed:
            raise RuntimeError("a sealed capture stage cannot include more paths")
        scope = _Scope(selection)
        if self._working:
            self._stage_working(scope)
        else:
            self._stage_revision(scope)

    def _stage_revision(self, scope: _Scope) -> None:
        if self._listing is None:
            self._listing = self.pin.full_listing()
        directories: list[TreeEntry] = []
        pending: list[TreeEntry] = []
        for entry in self._listing:
            if not scope.admits(entry.path):
                continue
            problem = _unsafe(entry)
            if problem is not None:
                raise SnapshotError(
                    side=self.pin.side,
                    revision=self.pin.commit,
                    commit=self.pin.commit,
                    detail=problem,
                )
            if entry.kind == "tree":
                directories.append(entry)
            elif entry.path not in self._written:
                pending.append(entry)
        blobs = self.pin.read_blobs(pending)
        for entry in directories:
            self._make_directory(entry.path)
        for entry, data in zip(pending, blobs, strict=True):
            target = self.root / entry.path
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                os.chmod(target, int(entry.mode, 8) & 0o7777)
            except OSError as error:
                raise _capture_os_error(
                    side=self.pin.side,
                    revision=self.revision,
                    commit=self.pin.commit,
                    path=entry.path,
                    operation="could not materialize captured file",
                    error=error,
                ) from error
            self._written.add(entry.path)

    def _make_directory(self, relative: str) -> None:
        try:
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _capture_os_error(
                side=self.pin.side,
                revision=self.revision,
                commit=self.pin.commit,
                path=relative,
                operation="could not materialize captured file",
                error=error,
            ) from error

    def _stage_working(self, scope: _Scope) -> None:
        """Walk only the selection of the live tree and copy admitted files."""
        failing_path = self._worktree
        try:
            stack: list[str] = [_ROOT]
            while stack:
                relative = stack.pop()
                directory = self._worktree if relative == _ROOT else self._worktree / relative
                failing_path = directory
                # A subtree lists its children; a route directory is entered
                # only toward the coordinates below it, so siblings are never
                # listed or opened.
                if scope.in_tree(relative):
                    names = sorted(os.listdir(directory))
                else:
                    names = sorted(scope.route.get(relative, ()))
                descend: list[str] = []
                for name in names:
                    if name == ".git":
                        continue
                    child = name if relative == _ROOT else f"{relative}/{name}"
                    if not scope.admits(child):
                        continue
                    source = directory / name
                    # A failure to inspect an entry means its directory could
                    # not be searched, so the directory is the location.
                    failing_path = directory
                    try:
                        info = source.lstat()
                    except FileNotFoundError:
                        if scope.in_tree(relative):
                            raise
                        continue
                    failing_path = source
                    target = self.root / child
                    if stat.S_ISDIR(info.st_mode):
                        target.mkdir(parents=True, exist_ok=True)
                        if child in scope.route or scope.in_tree(child):
                            descend.append(child)
                    elif stat.S_ISREG(info.st_mode) and child not in self._written:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                        os.chmod(target, stat.S_IMODE(info.st_mode))
                        self._written.add(child)
                stack.extend(reversed(descend))
        except OSError as error:
            raise _capture_os_error(
                side=self.pin.side,
                revision="WORKTREE",
                commit=self.pin.commit,
                path=failing_path,
                operation="could not capture working-tree input",
                error=error,
            ) from error


def capture_pair(
    worktree_root: Path,
    before: str,
    after: str | None,
    *,
    plan: Callable[[CaptureStage, CaptureStage], tuple[CaptureSelection, _T]],
) -> tuple[RevisionSnapshot, RevisionSnapshot, _T]:
    """Capture both sides of a comparison, scoped to the planned selection.

    *before* is a revision; *after* is a revision, or ``None`` for the working
    tree (whose ``HEAD`` is pinned for diagnostics). Both sides are pinned
    first, then opened as stages and handed to *plan*, which stages the files
    its selection is derived from and returns ``(selection, result)``. The
    union of that selection and every path staged on either side is included
    into both stages, which are sealed. Every temporary directory is removed on
    any failure, and the plan's own exceptions propagate unchanged.
    """
    try:
        root = Path(worktree_root).resolve()
    except OSError as error:
        raise _capture_os_error(
            side="before",
            revision=before,
            commit=None,
            path=worktree_root,
            operation="could not resolve capture worktree",
            error=error,
        ) from error
    before_pin = PinnedCommit.resolve(root, before, side="before")
    after_pin = PinnedCommit.resolve(root, "HEAD" if after is None else after, side="after")
    stages: list[CaptureStage] = []
    try:
        first = CaptureStage(root, before_pin, before, working=False)
        stages.append(first)
        second = CaptureStage(
            root, after_pin, "WORKTREE" if after is None else after, working=after is None
        )
        stages.append(second)
        selection, result = plan(first, second)
        union = selection.union(CaptureSelection(files=first.requested | second.requested))
        first._stage(union)
        second._stage(union)
        captured = first.seal(), second.seal()
    except BaseException:
        for stage in stages:
            stage.discard()
        raise
    return captured[0], captured[1], result


__all__ = [
    "CaptureManifest",
    "CaptureSelection",
    "CaptureStage",
    "GitInputError",
    "ManifestEntry",
    "RevisionSnapshot",
    "SnapshotError",
    "SnapshotMutationError",
    "capture_pair",
]
