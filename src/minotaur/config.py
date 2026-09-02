"""Versioned project configuration: discovery, validation, anchoring, and merge.

This module is the canonical owner of the versioned ``.minotaur.toml``
project contract.  One typed :class:`ProjectConfig` and one resolver entry
(:func:`resolve_config`) locate, parse, validate, anchor, and merge the
per-invocation project contract, so every current and future consumer reads
the same resolved value set.  This module never analyzes source or writes
graph output; it only resolves configuration.

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

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]

_CONFIG_FILENAME = ".minotaur.toml"
_SECTION = "minotaur"
_SCHEMA_VERSION = 1
_DEFAULT_GRAPH_FILENAME = "minotaur-graph.json"
_KNOWN_FIELDS = frozenset({"schema_version", "root", "graph", "targets"})


class ConfigError(ValueError):
    """A project configuration cannot be located, parsed, or validated.

    The message names the offending field or path so a caller (the CLI maps
    this error to exit status 2) can point the user at the exact problem.
    """


@dataclass(frozen=True, slots=True)
class _ParsedConfig:
    """One validated config file with all config-sourced paths anchored.

    Anchoring happens here, against the config file's own directory and the
    declared project root, so every value is already absolute and canonical
    when the resolver merges it with explicit CLI values (which pass through
    untouched and never undergo this anchoring).
    """

    root: Path
    graph: Path
    targets: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """The resolved project contract for a single config-consuming invocation.

    ``config_file`` is the located or explicitly selected config that governed
    the resolution, or ``None`` when discovery found no config.  ``root`` and
    ``graph`` are always present: they come from an explicit CLI value or from
    the config, and at least one source always exists for a config-consuming
    invocation.  ``targets`` is ``None`` only for invocations that do not
    consume targets (no explicit targets and no config supplying them).
    """

    config_file: Path | None
    root: Path
    targets: tuple[Path, ...] | None
    graph: Path


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
    raised rather than returning a partial contract.
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
    return ProjectConfig(config_file=located, root=root, targets=targets, graph=graph)


def _parse_config(path: Path) -> _ParsedConfig:
    """Parse and validate one config file, returning its anchored values.

    Every :class:`ConfigError` raised here names the offending field or the
    config path, and no resolved set is returned until every R-05/R-06
    violation has been rejected.
    """
    config_dir = path.resolve().parent
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read config file: {path}") from error
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error
    section = raw.get(_SECTION)
    if section is None:
        raise ConfigError(f"missing [{_SECTION}] section in {path}")
    if not isinstance(section, dict):
        raise ConfigError(f"[{_SECTION}] must be a table in {path}")
    for name in section:
        if name not in _KNOWN_FIELDS:
            raise ConfigError(f"unknown config field: {name}")
    _validate_schema_version(section, path)
    config_root = _config_root(section, config_dir)
    graph = _config_graph(section, config_root)
    targets = _config_targets(section, config_root)
    return _ParsedConfig(root=config_root, graph=graph, targets=targets)


def _validate_schema_version(section: dict[object, object], path: Path) -> None:
    """Reject a missing, mistyped, or unsupported ``schema_version``."""
    version = section.get("schema_version")
    if version is None:
        raise ConfigError(f"missing required field: schema_version (in {path})")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(f"schema_version must be an integer (in {path})")
    if version != _SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version: {version} (expected {_SCHEMA_VERSION})")


def _config_root(section: dict[object, object], config_dir: Path) -> Path:
    """Resolve ``root`` against the config directory, defaulting to it."""
    value = section.get("root")
    if value is None:
        return config_dir
    if not isinstance(value, str):
        raise ConfigError("config root must be a string")
    return (config_dir / value).resolve()


def _config_graph(section: dict[object, object], config_root: Path) -> Path:
    """Resolve ``graph`` against the declared root, defaulting its name.

    The configured graph is anchored but never root-containment-checked; an
    explicit output outside the root keeps today's explicit-output freedom.
    """
    value = section.get("graph")
    if value is None:
        return (config_root / _DEFAULT_GRAPH_FILENAME).resolve()
    if not isinstance(value, str):
        raise ConfigError("config graph must be a string")
    return (config_root / value).resolve()


def _config_targets(section: dict[object, object], config_root: Path) -> tuple[Path, ...]:
    """Validate ``targets`` and return each one anchored inside the root.

    ``targets`` is required and must be a non-empty list of strings.  Every
    config-sourced target is anchored at the declared project root and must
    stay inside it; an escaping target is rejected naming the offending path.
    """
    value = section.get("targets")
    if value is None:
        raise ConfigError("missing required field: targets")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError("config targets must be a list of strings")
    if not value:
        raise ConfigError("config targets must not be empty")
    anchored: list[Path] = []
    for raw_target in value:
        target = (config_root / raw_target).resolve()
        try:
            target.relative_to(config_root)
        except ValueError as error:
            raise ConfigError(
                f"config target escapes root: {raw_target} (root is {config_root})"
            ) from error
        anchored.append(target)
    return tuple(anchored)


def _git_work_tree_root(start: Path) -> Path | None:
    """Return the enclosing Git work-tree top for ``start``, or ``None``.

    ``None`` covers both "not inside a Git work tree" and "the guarded probe
    is unavailable or failed"; discovery then continues to the filesystem
    root instead of stopping at an assumed boundary.
    """
    completed = _run_git(start, ("rev-parse", "--show-toplevel"))
    if completed is None or completed.returncode != 0:
        return None
    top_level = completed.stdout.strip()
    if not top_level:
        return None
    return Path(top_level).resolve()


def _run_git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """Run one Git probe, treating unavailable or failed commands as unknown."""
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
