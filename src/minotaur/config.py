"""Versioned project configuration: discovery, validation, anchoring, and merge.

This module is the canonical owner of the versioned ``.minotaur.toml``
project contract.  One typed :class:`ProjectConfig` and one resolver entry
(:func:`resolve_config`) locate, parse, validate, anchor, and merge the
per-invocation project contract, so every current and future consumer reads
the same resolved value set.  This module never analyzes source or writes
graph output; besides resolving the project contract it hosts the neutral
file-read/parse seam (:func:`read_toml_file`) through which the system
loader reads committed ``system.toml`` files.

The resolver walks from the start directory (the current directory) toward
the filesystem root and selects the nearest ``.minotaur.toml``.  Inside a Git
work tree the walk stops at the work-tree root, so a config above it never
binds; outside a work tree, or when the guarded Git probe is unavailable, the
walk continues to the filesystem root.  An explicit ``--config`` file selects
exactly that file, disables walk-up discovery, and never merges or composes
two configs.

Validation rejects an unsupported ``schema_version``, unknown fields, wrong
types, missing required fields (``schema_version``, ``targets``), empty
``targets``, a ``--config`` path that does not exist, and config-sourced
targets that escape the declared project ``root`` before any resolved set is
returned.  Config-sourced paths are anchored and emitted absolute and
canonical, while explicit CLI-provided values pass through unmodified and win
per field.

TOML is read only through the guarded ``tomllib``/``tomli`` shim: Python 3.11+
uses the standard-library ``tomllib`` and the conditional dependency installs
``tomli`` on Python < 3.11, keeping exactly one compatibility backport.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from minotaur import git

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]

_CONFIG_FILENAME = ".minotaur.toml"
_SECTION = "minotaur"
_SCHEMA_VERSION = 1
_DEFAULT_GRAPH_FILENAME = "minotaur-graph.json"
_DEFAULT_SYSTEMS_DIR = "docs/systems"
_KNOWN_FIELDS = frozenset({"schema_version", "root", "graph", "targets", "systems_dir"})


class ConfigError(ValueError):
    """A project configuration cannot be located, parsed, or validated.

    The message names the offending field or path so a caller (the CLI maps
    this error to exit status 2) can point the user at the exact problem.
    """


@dataclass(frozen=True, slots=True)
class ValidatedConfig:
    """A validated configuration declaration before filesystem anchoring."""

    root: str
    graph: str
    targets: tuple[str, ...]
    systems_dir: str


@dataclass(frozen=True, slots=True)
class _ParsedConfig:
    """One validated config file with all config-sourced paths anchored.

    Anchoring happens here, against the config file's own directory and the
    declared project root, so every value is already absolute and canonical
    when the resolver merges it with explicit CLI values (which pass through
    untouched and never undergo this anchoring).  ``systems_dir`` follows the
    same rule: a configured relative value anchors at the declared root, and
    an omitted field already resolves the ``docs/systems`` default there.
    """

    root: Path
    graph: Path
    targets: tuple[Path, ...]
    systems_dir: Path


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """The resolved project contract for a single config-consuming invocation.

    ``config_file`` is the located or explicitly selected config that governed
    the resolution, or ``None`` when discovery found no config.  ``root``,
    ``graph``, and ``systems_dir`` are always present: ``root`` and ``graph``
    come from an explicit CLI value or from the config, and at least one
    source always exists for a config-consuming invocation.  ``systems_dir``
    is the config's anchored value when a config governs, and otherwise the
    ``docs/systems`` default under the resolved ``root``; it is always
    emitted so every consumer reads one canonical value.  ``targets`` is
    ``None`` only for invocations that do not consume targets (no explicit
    targets and no config supplying them).
    """

    config_file: Path | None
    root: Path
    targets: tuple[Path, ...] | None
    graph: Path
    systems_dir: Path


def find_config(start: Path, *, config: Path | None = None) -> Path | None:
    """Locate the governing config file for a resolution starting at ``start``.

    ``start`` is the directory discovery begins from (the current directory).
    When ``config`` (an explicit ``--config`` value) is given it is the only
    candidate: it is resolved against ``start`` when relative, must be a file
    (otherwise a :class:`ConfigError` naming the path is raised), and walk-up
    discovery is disabled.  Otherwise the nearest ``.minotaur.toml`` walking
    from ``start`` toward the filesystem root is returned, stopping at the
    enclosing Git work-tree root per the discovery boundary; ``None`` means no
    config governs the resolution.
    """
    if config is not None:
        selected = config if config.is_absolute() else start / config
        if not selected.is_file():
            raise ConfigError(f"config file does not exist: {config}")
        return selected.resolve()
    boundary = _git_work_tree_root(start)
    current = start.resolve()
    while True:
        candidate = current / _CONFIG_FILENAME
        if candidate.is_file():
            return candidate.resolve()
        if boundary is not None and current == boundary:
            return None
        parent = current.parent
        if parent == current:  # Filesystem root reached.
            return None
        current = parent


def resolve_config(
    start: Path,
    *,
    config: Path | None = None,
    explicit_root: Path | None = None,
    explicit_graph: Path | None = None,
    explicit_targets: Sequence[Path] | None = None,
) -> ProjectConfig:
    """Resolve the project contract: locate, parse, validate, anchor, merge.

    The single resolver entry for every config-consuming invocation.  It
    locates the governing config (walk-up discovery or the explicit ``config``
    file), parses and fully validates it when one exists (a config present in
    the tree is validated even when every flag is fully explicit), anchors all
    config-sourced paths, and merges the result field by field with explicit
    values winning.  Explicit CLI values pass through unmodified: a relative
    value stays relative and an absolute value stays absolute.  The resolved
    ``root`` and ``graph`` always exist; when neither the config nor an
    explicit value supplies one, a :class:`ConfigError` naming the field is
    raised rather than returning a partial contract.  ``systems_dir`` is the
    config's anchored value (defaulting to ``docs/systems`` under the
    config's declared root) when a config governs; otherwise it defaults to
    ``docs/systems`` under the resolved root, so every successful resolution
    carries one canonical ``systems_dir`` value.
    """
    located = find_config(start, config=config)
    parsed = _parse_config(located) if located is not None else None
    if explicit_root is not None:
        root = explicit_root
    elif parsed is not None:
        root = parsed.root
    else:
        raise ConfigError("no root configured: pass an explicit root or place a config file")
    if explicit_graph is not None:
        graph = explicit_graph
    elif parsed is not None:
        graph = parsed.graph
    else:
        raise ConfigError("no graph path configured: pass an explicit graph or place a config file")
    if explicit_targets is not None:
        targets = tuple(explicit_targets)
    elif parsed is not None:
        targets = parsed.targets
    else:
        targets = None
    systems_dir = parsed.systems_dir if parsed is not None else root / _DEFAULT_SYSTEMS_DIR
    return ProjectConfig(
        config_file=located,
        root=root,
        targets=targets,
        graph=graph,
        systems_dir=systems_dir,
    )


def read_toml_file(path: Path) -> dict[str, object]:
    """Read and TOML-parse one file, attributing failures to ``path``.

    Config-vocabulary-neutral file-read/parse seam: it applies no
    ``[minotaur]`` section or field checks and no anchoring, so callers read
    any TOML file (the committed ``system.toml`` files read by the system
    loader, for example) through the same guarded ``tomllib`` shim as the
    project contract.  Read and parse failures raise a :class:`ConfigError`
    naming the path, mirroring the project-config read/parse errors.
    """
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ConfigError(f"cannot read TOML file: {path}") from error
    return read_toml_bytes(data, source=path)


def read_toml_bytes(data: bytes, *, source: Path | str) -> dict[str, object]:
    """Decode and TOML-parse supplied UTF-8 bytes without filesystem access."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigError(f"invalid UTF-8 in TOML: {source}") from error
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {source}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"TOML document must be a table: {source}")
    return raw


def parse_config_bytes(data: bytes, *, source: Path | str) -> ValidatedConfig:
    """Validate a supplied config blob without consulting its path or disk."""
    return _validate_config(read_toml_bytes(data, source=source), source=source)


def _parse_config(path: Path) -> _ParsedConfig:
    """Parse and validate one config file, returning its anchored values.

    Every :class:`ConfigError` raised here names the offending field or the
    config path, and no resolved set is returned until every R-05/R-06
    violation has been rejected.
    """
    raw = read_toml_file(path)
    validated = _validate_config(raw, source=path)
    return _anchor_config(validated, source=path)


def _validate_config(raw: Mapping[str, object], *, source: Path | str) -> ValidatedConfig:
    """Validate raw config declarations without consulting the filesystem."""
    section = raw.get(_SECTION)
    if section is None:
        raise ConfigError(f"missing [{_SECTION}] section in {source}")
    if not isinstance(section, Mapping):
        raise ConfigError(f"[{_SECTION}] must be a table in {source}")
    for name in section:
        if name not in _KNOWN_FIELDS:
            raise ConfigError(f"unknown config field: {name} (in {source})")
    _validate_schema_version(section, source)

    root = section.get("root")
    if root is None:
        root = ""
    elif not isinstance(root, str):
        raise ConfigError(f"config root must be a string (in {source})")

    graph = section.get("graph")
    if graph is None:
        graph = _DEFAULT_GRAPH_FILENAME
    elif not isinstance(graph, str):
        raise ConfigError(f"config graph must be a string (in {source})")

    targets = section.get("targets")
    if targets is None:
        raise ConfigError(f"missing required field: targets (in {source})")
    if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
        raise ConfigError(f"config targets must be a list of strings (in {source})")
    if not targets:
        raise ConfigError(f"config targets must not be empty (in {source})")

    systems_dir = section.get("systems_dir")
    if systems_dir is None:
        systems_dir = _DEFAULT_SYSTEMS_DIR
    elif not isinstance(systems_dir, str):
        raise ConfigError(f"config systems_dir must be a string (in {source})")

    return ValidatedConfig(
        root=root,
        graph=graph,
        targets=tuple(targets),
        systems_dir=systems_dir,
    )


def _anchor_config(validated: ValidatedConfig, *, source: Path) -> _ParsedConfig:
    """Apply ordinary disk anchoring to one validated declaration."""
    config_dir = source.resolve().parent
    config_root = (config_dir / validated.root).resolve()
    graph = (config_root / validated.graph).resolve()
    anchored: list[Path] = []
    for raw_target in validated.targets:
        target = (config_root / raw_target).resolve()
        try:
            target.relative_to(config_root)
        except ValueError as error:
            raise ConfigError(
                f"config target escapes root: {raw_target} (root is {config_root}) (in {source})"
            ) from error
        anchored.append(target)
    systems_dir = (config_root / validated.systems_dir).resolve()
    return _ParsedConfig(
        root=config_root,
        graph=graph,
        targets=tuple(anchored),
        systems_dir=systems_dir,
    )


def _validate_schema_version(section: Mapping[object, object], path: Path | str) -> None:
    """Reject a missing, mistyped, or unsupported ``schema_version``."""
    version = section.get("schema_version")
    if version is None:
        raise ConfigError(f"missing required field: schema_version (in {path})")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(f"schema_version must be an integer (in {path})")
    if version != _SCHEMA_VERSION:
        raise ConfigError(
            f"unsupported schema_version: {version} (expected {_SCHEMA_VERSION}) (in {path})"
        )


def _git_work_tree_root(start: Path) -> Path | None:
    """Return the enclosing Git work-tree top for ``start``, or ``None``.

    ``None`` covers both "not inside a Git work tree" and "the guarded probe
    is unavailable or failed"; discovery then continues to the filesystem
    root instead of stopping at an assumed boundary.
    """
    return git.work_tree_root(start)
