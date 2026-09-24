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

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from types import MappingProxyType

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
_KNOWN_FIELDS = frozenset({"schema_version", "root", "graph", "targets", "systems_dir", "sql"})
_SQL_KNOWN_FIELDS = frozenset(
    {"view_depth_threshold", "migration_patterns", "foreign_key_target_files"}
)
_DEFAULT_VIEW_DEPTH_THRESHOLD = 3
_DEFAULT_MIGRATION_PATTERNS: tuple[str, ...] = ()
_FOREIGN_KEY_TARGET_FILES_ASSIGNMENT = re.compile(
    r"""(?mx)
    ^[ \t]*
    (?:(?:minotaur[ \t]*\.[ \t]*)?(?:sql[ \t]*\.[ \t]*)?)
    foreign_key_target_files[ \t]*=[ \t]*\{
    """
)


class ConfigError(ValueError):
    """A project configuration cannot be located, parsed, or validated.

    The message names the offending field or path so a caller (the CLI maps
    this error to exit status 2) can point the user at the exact problem.
    """


class _TomlDocument(dict[str, object]):
    """A generic parsed TOML table retaining its text for config-only checks."""

    def __init__(self, values: Mapping[str, object], text: str) -> None:
        super().__init__(values)
        self.text = text


@dataclass(frozen=True, slots=True)
class SqlSettings:
    """Immutable SQL analysis settings resolved for one invocation."""

    view_depth_threshold: int = _DEFAULT_VIEW_DEPTH_THRESHOLD
    migration_patterns: tuple[str, ...] = _DEFAULT_MIGRATION_PATTERNS
    foreign_key_target_files: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.view_depth_threshold, bool) or not isinstance(
            self.view_depth_threshold, int
        ):
            raise ValueError("view_depth_threshold must be an integer")
        if self.view_depth_threshold <= 0:
            raise ValueError("view_depth_threshold must be positive")
        if not isinstance(self.migration_patterns, (list, tuple)):
            raise ValueError("migration_patterns must be a list of strings")
        patterns = tuple(self.migration_patterns)
        for pattern in patterns:
            _validate_migration_pattern(pattern)
        object.__setattr__(self, "migration_patterns", patterns)
        object.__setattr__(
            self,
            "foreign_key_target_files",
            _normalize_foreign_key_target_files(self.foreign_key_target_files),
        )

    def matches_migration(self, path: Path | str) -> bool:
        """Return whether a root-relative POSIX path matches a migration glob.

        Matching is performed component by component.  Ordinary wildcards stay
        within one component, while a component consisting of ``**`` spans
        zero or more complete path components.
        """
        path_parts = _path_components(path)
        if path_parts is None:
            return False
        return any(_match_pattern(pattern, path_parts) for pattern in self.migration_patterns)


@dataclass(frozen=True, slots=True)
class ValidatedConfig:
    """A validated configuration declaration before filesystem anchoring."""

    root: str
    graph: str
    targets: tuple[str, ...]
    systems_dir: str
    sql: SqlSettings = field(default_factory=SqlSettings)


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
    sql: SqlSettings


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
    sql: SqlSettings = field(default_factory=SqlSettings)


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
    sql = parsed.sql if parsed is not None else SqlSettings()
    return ProjectConfig(
        config_file=located,
        root=root,
        targets=targets,
        graph=graph,
        systems_dir=systems_dir,
        sql=sql,
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
    return _TomlDocument(raw, text)


def parse_config_bytes(data: bytes, *, source: Path | str) -> ValidatedConfig:
    """Validate a supplied config blob without consulting its path or disk."""
    raw = read_toml_bytes(data, source=source)
    _validate_foreign_key_target_file_key_quotes(raw, source=source)
    return _validate_config(raw, source=source)


def _parse_config(path: Path) -> _ParsedConfig:
    """Parse and validate one config file, returning its anchored values.

    Every :class:`ConfigError` raised here names the offending field or the
    config path, and no resolved set is returned until every R-05/R-06
    violation has been rejected.
    """
    raw = read_toml_file(path)
    _validate_foreign_key_target_file_key_quotes(raw, source=path)
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

    sql = _validate_sql_settings(section.get("sql"), source)

    return ValidatedConfig(
        root=root,
        graph=graph,
        targets=tuple(targets),
        systems_dir=systems_dir,
        sql=sql,
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
        sql=validated.sql,
    )


def _validate_sql_settings(raw: object, source: Path | str) -> SqlSettings:
    """Validate the optional ``[minotaur.sql]`` settings table."""
    if raw is None:
        return SqlSettings()
    if not isinstance(raw, Mapping):
        raise ConfigError(f"[minotaur.sql] must be a table (in {source})")
    for name in raw:
        if name not in _SQL_KNOWN_FIELDS:
            raise ConfigError(f"unknown SQL config field: {name} (in {source})")
    threshold = raw.get("view_depth_threshold", _DEFAULT_VIEW_DEPTH_THRESHOLD)
    migration_patterns = raw.get("migration_patterns", [])
    if not isinstance(migration_patterns, list) or any(
        not isinstance(item, str) for item in migration_patterns
    ):
        raise ConfigError(
            f"invalid minotaur.sql.migration_patterns: must be a list of strings (in {source})"
        )
    foreign_key_target_files = raw.get("foreign_key_target_files", {})
    if not isinstance(foreign_key_target_files, Mapping):
        raise ConfigError(
            f"invalid minotaur.sql.foreign_key_target_files: must be a mapping (in {source})"
        )
    try:
        return SqlSettings(threshold, tuple(migration_patterns), foreign_key_target_files)
    except ValueError as error:
        message = str(error)
        if "foreign_key_target_files" in message:
            field = "foreign_key_target_files"
        else:
            field = (
                "migration_patterns" if "migration_patterns" in message else "view_depth_threshold"
            )
        raise ConfigError(f"invalid minotaur.sql.{field}: {error} (in {source})") from error


def _validate_foreign_key_target_file_key_quotes(
    raw: Mapping[str, object], *, source: Path | str
) -> None:
    """Require quoted keys in the one config field whose names are SQL targets.

    TOML parsing intentionally turns bare and quoted keys into the same string.
    The generic reader therefore retains the original text, and only project
    configuration validation inspects that text for this documented grammar.
    """
    if not isinstance(raw, _TomlDocument):
        return
    for match in _FOREIGN_KEY_TARGET_FILES_ASSIGNMENT.finditer(raw.text):
        contents = _inline_table_contents(raw.text, match.end() - 1)
        if contents is None:
            continue
        for entry in _split_inline_table_entries(contents):
            if entry.strip() and not entry.lstrip().startswith(("'", '"')):
                raise ConfigError(
                    "invalid minotaur.sql.foreign_key_target_files: "
                    f"target keys must be quoted (in {source})"
                )


def _inline_table_contents(text: str, opening_brace: int) -> str | None:
    """Return one inline table's contents while preserving quoted values."""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening_brace, len(text)):
        character = text[index]
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[opening_brace + 1 : index]
    return None


def _split_inline_table_entries(contents: str) -> tuple[str, ...]:
    """Split an inline table on its top-level commas without parsing values."""
    entries: list[str] = []
    start = 0
    nested = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(contents):
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "[{":
            nested += 1
        elif character in "]}":
            nested -= 1
        elif character == "," and nested == 0:
            entries.append(contents[start:index])
            start = index + 1
    entries.append(contents[start:])
    return tuple(entries)


def _validate_migration_pattern(pattern: object) -> None:
    """Validate one nonempty root-relative POSIX glob pattern."""
    if not isinstance(pattern, str):
        raise ValueError("migration_patterns must be a list of strings")
    if not pattern:
        raise ValueError("migration_patterns entries must not be empty")
    if "\\" in pattern:
        raise ValueError("migration_patterns must use POSIX '/' separators")
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:/", pattern):
        raise ValueError("migration_patterns must be root-relative")
    components = pattern.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("migration_patterns cannot contain empty, '.', or '..' components")
    if "{" in pattern or "}" in pattern:
        raise ValueError("migration_patterns cannot contain replacement fields")
    for component in components:
        if component.count("[") != component.count("]"):
            raise ValueError("migration_patterns contains an unterminated bracket expression")


def _normalize_foreign_key_target_files(raw: object) -> Mapping[str, str]:
    """Validate and freeze exact SQL target to source path mappings."""
    if not isinstance(raw, Mapping):
        raise ValueError("foreign_key_target_files must be a mapping")
    normalized: dict[str, str] = {}
    for target, path in raw.items():
        if not isinstance(target, str):
            raise ValueError("foreign_key_target_files keys must be strings")
        target_parts = target.split(".")
        if (
            len(target_parts) not in {1, 2}
            or any(not part or part in {".", ".."} for part in target_parts)
            or any(char in target for char in "\\/[]{}*?!")
        ):
            raise ValueError(f"foreign_key_target_files has invalid target key: {target!r}")
        normalized_target = ".".join(part.casefold() for part in target_parts)
        if normalized_target in normalized:
            raise ValueError(
                f"foreign_key_target_files has duplicate normalized target: {target!r}"
            )
        if not isinstance(path, str):
            raise ValueError("foreign_key_target_files values must be strings")
        if (
            not path
            or "\\" in path
            or path.startswith("/")
            or re.match(r"^[A-Za-z]:[/\\]", path)
            or any(char in path for char in "*?[]{}")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not path.casefold().endswith(".sql")
        ):
            raise ValueError(f"foreign_key_target_files has invalid SQL path: {path!r}")
        normalized[normalized_target] = path
    return MappingProxyType(normalized)


def _path_components(path: Path | str) -> tuple[str, ...] | None:
    """Return validated root-relative POSIX components, or ``None``."""
    value = path.as_posix() if isinstance(path, Path) else path
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        return None
    components = tuple(value.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        return None
    return components


def _match_pattern(pattern: str, path_parts: tuple[str, ...]) -> bool:
    """Match one validated pattern against root-relative path components."""
    pattern_parts = tuple(pattern.split("/"))
    pending = [(0, 0)]
    visited = {(0, 0)}

    while pending:
        pattern_index, path_index = pending.pop()
        if pattern_index == len(pattern_parts):
            if path_index == len(path_parts):
                return True
            continue

        component = pattern_parts[pattern_index]
        if component == "**":
            next_states = [(pattern_index + 1, path_index)]
            if path_index < len(path_parts):
                next_states.append((pattern_index, path_index + 1))
        elif path_index < len(path_parts) and fnmatchcase(path_parts[path_index], component):
            next_states = [(pattern_index + 1, path_index + 1)]
        else:
            continue

        for state in next_states:
            if state not in visited:
                visited.add(state)
                pending.append(state)

    return False


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
