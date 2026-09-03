"""Committed system definitions: model, strict loader, and membership.

This module owns the committed-declaration contract (D-01, R-01): the
declared system model — a unique ``name`` plus a deduplicated ``files`` list
of root-relative individual repository file paths — and the loader that
discovers and strictly validates the committed ``system.toml`` files.

Systems are flat peers under the resolved ``systems_dir`` (D-03).  Only an
immediate child *directory* of ``systems_dir`` that contains a
``system.toml`` defines a system, so a ``system.toml`` in a nested
subdirectory and a stray file anywhere under ``systems_dir`` are narrative
and never define anything.  Narrative documentation may coexist inside a
system directory; everything except the definition file is ignored (AR-02).

Loading is deterministic and strict (R-03): directories are scanned in
sorted order, every read and TOML parse goes exclusively through the config
owner's guarded neutral helper (:func:`config.read_toml_file`) — this module
never imports or calls a TOML parser itself — and every unsupported
``schema_version``, unknown field (hand-recorded, expectation-shaped, and
curated-rule-shaped relationship keys included, AR-01/AR-06), duplicate
``name``, empty ``files`` list, non-file or root-escaping ``files`` entry,
and file listed in two systems raises a typed, file-attributed
:class:`ValueError`-family error before any system set is produced.  The
overlap error names both defining files.

Membership (R-04, D-04, AR-03) is the deterministic exact-file test "is
this file listed — Y/N": a listed file, and every graph endpoint whose
derived file is listed, belongs to the one listing system; an unlisted
path-carrying file is ``no_system``; a path-less upstream endpoint is
``external``; and no package, module, or label ever implies membership.  A
declared file with no analyzed node is surfaced for diagnosis rather than
silently dropped.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from minotaur.config import read_toml_file
from minotaur.graph_model.identity import is_valid_node_id_format
from minotaur.graph_model.node import Node

_SYSTEM_FILENAME = "system.toml"
_SCHEMA_VERSION = 1
_KNOWN_SYSTEM_FIELDS = frozenset({"schema_version", "name", "files"})
# The characters that make an entry a glob or pattern rather than one file.
_GLOB_CHARACTERS = frozenset("*?[]{}")


class SystemError(ValueError):
    """A committed system definition cannot be loaded, resolved, or classified.

    Subclasses :class:`ValueError` on purpose: the CLI already maps that to
    exit status 2 for every other input error, so an invalid committed
    definition or an unknown system name can never be rendered as an
    empty-but-successful result.
    """


class SystemDefinitionError(SystemError):
    """A committed ``system.toml`` violates the strict load contract (R-03).

    Every raised message names the offending definition file (and, for a
    name or a file declared twice, both defining files) so a caller can
    point the user at the exact committed artifact.
    """


class UnsupportedSchemaVersion(SystemDefinitionError):
    """``schema_version`` is missing, mistyped, or not the supported value."""


class UnknownSystemField(SystemDefinitionError):
    """The definition declares a field the schema does not define.

    This is the single rejection point for every unknown key, so
    hand-recorded, expectation-shaped, and curated-rule-shaped relationship
    keys (``depends_on``, ``expectations``, edge lists, and friends) are all
    rejected here (AR-01, AR-06).
    """


class MissingField(SystemDefinitionError):
    """A required definition field (``schema_version``, ``name``, ``files``) is absent."""


class InvalidSystemName(SystemDefinitionError):
    """``name`` is present but is not a non-empty string."""


class InvalidFileList(SystemDefinitionError):
    """``files`` is present but is not a list, or is an empty list."""


class InvalidFileEntry(SystemDefinitionError):
    """A ``files`` entry is not a root-relative individual repository file path.

    Covers non-string and empty entries, absolute and root-escaping
    traversals, globs and patterns, node IDs, and directory-like forms.
    """


class DuplicateSystemName(SystemDefinitionError):
    """Two definition files declare the same system ``name``.

    Duplicate names are a load error (R-03), so only unknown names remain
    for runtime resolution (D-12).
    """


class FileListedInTwoSystems(SystemDefinitionError):
    """One repository file is declared by two systems.

    A file belongs to at most one system (D-02), so overlap is a load-time
    error naming both defining files.
    """


class UnknownSystem(SystemError):
    """No loaded system carries the queried name (D-12, F-02 family).

    Mirrors ``GraphIndex.resolve``: a :class:`ValueError`-family resolution
    failure carrying the nearest loaded names when any are plausible.
    """

    def __init__(self, name: str, nearest: Sequence[str] = ()) -> None:
        self.name = name
        self.nearest = tuple(nearest)
        message = f"unknown system: {name}"
        if self.nearest:
            message = f"{message}; nearest systems: {', '.join(self.nearest)}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class System:
    """One valid declared system: a unique name and its deduplicated files.

    ``files`` holds root-relative individual repository file paths in
    declaration order, deduplicated (a path declared twice in one definition
    appears once).  Name uniqueness and the absence of cross-system file
    overlap are guaranteed by the loader, so a file is in at most one system.
    """

    name: str
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LoadedSystem:
    """One parsed definition paired with its defining file for attribution."""

    system: System
    definition: Path


def load_systems(systems_dir: Path) -> tuple[System, ...]:
    """Discover and strictly load every committed system under ``systems_dir``.

    Only immediate child directories of ``systems_dir`` containing a
    ``system.toml`` define a system (D-03); every other entry — stray files
    and nested subdirectories anywhere under ``systems_dir`` — is narrative
    and ignored (AR-02).  A missing or non-directory ``systems_dir`` yields
    the empty set: a repository without committed systems declares none.
    Directories are scanned in sorted name order and every definition is
    fully parsed and validated before the cross-system checks run, so an
    invalid file anywhere fails the whole load (R-03) deterministically, and
    the returned systems are ordered by declared name.
    """
    if not systems_dir.is_dir():
        return ()
    loaded: list[_LoadedSystem] = []
    for child in sorted(systems_dir.iterdir(), key=lambda entry: entry.name):
        if not child.is_dir():
            continue  # D-03/AR-02: a stray file under systems_dir defines nothing.
        definition = child / _SYSTEM_FILENAME
        if not definition.is_file():
            continue  # D-03: a directory without system.toml defines no system.
        loaded.append(_LoadedSystem(system=_parse_definition(definition), definition=definition))
    _reject_cross_system_conflicts(loaded)
    return tuple(sorted((item.system for item in loaded), key=lambda system: system.name))


def resolve_system(systems: Sequence[System], name: str) -> System:
    """Return the one loaded system named ``name`` or raise.

    Mirrors ``GraphIndex.resolve`` (F-02, D-12): an unknown name raises
    :class:`UnknownSystem` carrying the nearest loaded names rather than
    choosing one definition.  Duplicate names are already a load error
    (R-03), so only the unknown outcome remains.
    """
    for system in systems:
        if system.name == name:
            return system
    loaded_names = [system.name for system in systems]
    raise UnknownSystem(name, difflib.get_close_matches(name, loaded_names, n=5))


def system_for_file(systems: Sequence[System], file: str) -> System | None:
    """Return the one system whose ``files`` list contains ``file`` (R-04).

    The deterministic exact-file test "is this file listed — Y/N": the
    listing system when listed, else ``None`` (``no_system``).  No package,
    module, or directory name ever implies membership (AR-03).
    """
    for system in systems:
        if file in system.files:
            return system
    return None


class EndpointKind(Enum):
    """Where one graph endpoint sits relative to the loaded systems (D-04).

    ``SYSTEM`` means the endpoint's derived file is listed by a system;
    ``NO_SYSTEM`` means the endpoint carries a file that no system lists;
    ``EXTERNAL`` means the endpoint carries no path at all (a path-less
    upstream node) and so can never belong to a system.
    """

    SYSTEM = "system"
    NO_SYSTEM = "no_system"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class EndpointMembership:
    """The classification of one graph endpoint (D-04).

    ``file`` is the endpoint's derived repository-relative file — from
    ``location.path`` when the node carries a location (symbols,
    unresolved-reference sites, located resources), otherwise from
    ``node.path`` (file nodes and other path-carrying nodes) — and is
    ``None`` only for ``EXTERNAL`` endpoints.  ``system`` is set exactly when
    ``kind`` is ``SYSTEM``.
    """

    kind: EndpointKind
    system: System | None = None
    file: str | None = None


def classify_endpoint(systems: Sequence[System], node: Node) -> EndpointMembership:
    """Classify one relationship endpoint against the loaded systems (R-04).

    The endpoint's file derives from its location when present, else from
    ``node.path``; a node with neither — a path-less upstream symbol, for
    example — is always ``EXTERNAL``.  A path-carrying endpoint goes through
    the exact-file membership test; no label, package, or module name ever
    implies membership (AR-03).
    """
    derived = _node_file(node)
    if derived is None:
        return EndpointMembership(kind=EndpointKind.EXTERNAL)
    system = system_for_file(systems, derived)
    if system is None:
        return EndpointMembership(kind=EndpointKind.NO_SYSTEM, file=derived)
    return EndpointMembership(kind=EndpointKind.SYSTEM, system=system, file=derived)


@dataclass(frozen=True, slots=True)
class AbsentFile:
    """One declared file with no analyzed node deriving from it (R-03).

    ``system`` is the declaring system and ``file`` the declared
    root-relative path, so a consumer can emit a per-file diagnostic instead
    of silently dropping the declared file.
    """

    system: System
    file: str


def absent_files(systems: Sequence[System], nodes: Iterable[Node]) -> tuple[AbsentFile, ...]:
    """Listed files with no analyzed node in ``nodes``, surfaced in order (R-03).

    A listed file counts as present when at least one analyzed node derives
    from it — the same exact-file derivation membership uses, so a file node
    with that path or any node located in the file proves the file was
    analyzed.  Absent rows are ordered by system (declared order) then by
    declared file order, never silently dropped.
    """
    present: set[str] = set()
    for node in nodes:
        derived = _node_file(node)
        if derived is not None:
            present.add(derived)
    return tuple(
        AbsentFile(system=system, file=file)
        for system in systems
        for file in system.files
        if file not in present
    )


def _node_file(node: Node) -> str | None:
    """Derive the repository-relative file an endpoint node lives in (D-04).

    ``location.path`` wins when the node carries a location (symbols and
    unresolved-reference sites are resolved through it); otherwise the
    node's own ``path`` (file nodes) is used.  ``None`` means the node is
    path-less — an upstream endpoint that is always ``external``.
    """
    if node.location is not None:
        return node.location.path
    return node.path


def _parse_definition(path: Path) -> System:
    """Parse and strictly validate one ``system.toml``, returning its system.

    Every read and TOML parse goes through :func:`config.read_toml_file`, the
    config owner's guarded neutral helper (this module never imports or calls
    a TOML parser), so an unreadable or invalid file already raises the
    helper's file-attributed :class:`~minotaur.config.ConfigError`.  Every
    other rejection below raises a typed, file-attributed error naming this
    definition file, and no partial system is returned.
    """
    raw = read_toml_file(path)
    for field in raw:
        if field not in _KNOWN_SYSTEM_FIELDS:
            raise UnknownSystemField(f"unknown system field: {field} (in {path})")

    version = raw.get("schema_version")
    if version is None:
        raise MissingField(f"missing required field: schema_version (in {path})")
    if isinstance(version, bool) or not isinstance(version, int):
        raise UnsupportedSchemaVersion(f"schema_version must be an integer (in {path})")
    if version != _SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"unsupported schema_version: {version} (expected {_SCHEMA_VERSION}) (in {path})"
        )

    name = raw.get("name")
    if name is None:
        raise MissingField(f"missing required field: name (in {path})")
    if not isinstance(name, str) or not name:
        raise InvalidSystemName(f"system name must be a non-empty string (in {path})")

    files_value = raw.get("files")
    if files_value is None:
        raise MissingField(f"missing required field: files (in {path})")
    if not isinstance(files_value, list):
        raise InvalidFileList(f"system files must be a list of file paths (in {path})")
    if not files_value:
        raise InvalidFileList(f"system files must not be empty (in {path})")

    # D-02: the scope vocabulary is an explicit list of root-relative
    # individual file paths — never directories, globs, or node IDs.  The
    # "all files under X" convenience entry is deliberately not offered in
    # this version; it belongs here, at the file-list validation site, and
    # should be added only if real repositories demonstrate the need.
    entries = tuple(_require_file_entry(entry, path) for entry in files_value)
    return System(name=name, files=_dedupe(entries))


def _require_file_entry(entry: object, path: Path) -> str:
    """Validate one ``files`` entry as a root-relative individual file path.

    Entries are validated purely against the root-relative repository file
    vocabulary (the same slash-separated relative space graph node and
    location paths live in): an entry is rejected when it is not a string,
    is empty, is absolute, escapes the root through ``..``, names a glob or
    pattern, is a node ID, or carries empty/dot segments or a directory
    marker.  The loader never probes the source tree, so an entry can only
    name an existing directory indistinguishably from a file; such a path
    then never matches an analyzed file and is surfaced by the absent-file
    report instead of being silently dropped.
    """
    if not isinstance(entry, str):
        raise InvalidFileEntry(f"system files entries must be strings (in {path}): {entry!r}")
    if entry == "":
        raise InvalidFileEntry(f"system file entry must not be empty (in {path})")
    if entry.startswith("/"):
        raise InvalidFileEntry(
            f"system file entry must be root-relative, not an absolute path: {entry} (in {path})"
        )
    if is_valid_node_id_format(entry):
        raise InvalidFileEntry(
            f"system file entry must name a repository file, not a node ID: {entry} (in {path})"
        )
    if any(character in entry for character in _GLOB_CHARACTERS):
        raise InvalidFileEntry(
            f"system file entry must name one file, not a glob or pattern: {entry} (in {path})"
        )
    if "\\" in entry or "\x00" in entry:
        raise InvalidFileEntry(
            f"system file entry must be a slash-separated repository path: {entry} (in {path})"
        )
    segments = entry.split("/")
    if any(segment == ".." for segment in segments):
        raise InvalidFileEntry(
            f"system file entry escapes the repository root: {entry} (in {path})"
        )
    if any(segment in ("", ".") for segment in segments):
        raise InvalidFileEntry(
            f"system file entry must name an individual root-relative file: {entry} (in {path})"
        )
    return entry


def _dedupe(entries: Sequence[str]) -> tuple[str, ...]:
    """Deduplicate validated entries, keeping each path's first position."""
    seen: set[str] = set()
    unique: list[str] = []
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        unique.append(entry)
    return tuple(unique)


def _reject_cross_system_conflicts(loaded: Sequence[_LoadedSystem]) -> None:
    """Reject duplicate system names and files declared by two systems.

    Runs after every definition parsed, in sorted-directory order, so the
    reported conflict (and the two defining files it names) is deterministic.
    """
    by_name: dict[str, Path] = {}
    by_file: dict[str, Path] = {}
    for item in loaded:
        system = item.system
        previous = by_name.get(system.name)
        if previous is not None:
            raise DuplicateSystemName(
                f"duplicate system name: {system.name} "
                f"(declared in {previous} and {item.definition})"
            )
        by_name[system.name] = item.definition
        for file in system.files:
            prior = by_file.get(file)
            if prior is not None:
                raise FileListedInTwoSystems(
                    f"file listed in two systems: {file} "
                    f"(declared in {prior} and {item.definition})"
                )
            by_file[file] = item.definition
