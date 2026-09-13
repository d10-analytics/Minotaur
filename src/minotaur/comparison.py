"""Read one complete historical Minotaur snapshot from a pinned commit.

Historical acquisition is intentionally independent from current workspace
discovery.  A single :class:`~minotaur.git.PinnedCommit` supplies every tree
listing and blob, while this module interprets the declarations in the
historical configuration and composes the existing strict loaders.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from minotaur import config, git, system
from minotaur.config import ValidatedConfig
from minotaur.graph_model import loading
from minotaur.graph_model.loading import LoadedGraph
from minotaur.graph_model.validation import validate_document
from minotaur.language_interpreter.contract import AnalysisResult, Diagnostic
from minotaur.language_interpreter.selection import SourceSelection
from minotaur.language_interpreter.workspace import Workspace
from minotaur.query.system import ReportingSnapshot


class HistoricalInputError(git.GitInputError):
    """A historical input failed after the commit had been pinned."""


class CurrentInputError(ValueError):
    """A current input failed during strict comparison acquisition."""

    def __init__(
        self,
        *,
        path: str | Path,
        detail: str,
        cause: str | None = None,
        diagnostics: Sequence[Diagnostic] = (),
    ) -> None:
        self.side = "current"
        self.path = str(path)
        self.detail = detail
        self.cause = cause
        self.cause_type = cause
        self.diagnostics = tuple(diagnostics)
        self.source_diagnostics = self.diagnostics
        identity = f" ({cause})" if cause else ""
        super().__init__(f"current input at {self.path!r}{identity}: {detail}")


class SelectionProducer(Protocol):
    """The existing in-memory source producer used by comparison."""

    def __call__(
        self,
        root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
    ) -> tuple[Workspace, SourceSelection, AnalysisResult]: ...


@dataclass(frozen=True, slots=True)
class _Route:
    entry: git.TreeEntry | None
    coordinate: str


@dataclass(frozen=True, slots=True)
class HistoricalInputs:
    """All immutable observations needed to compare one historical snapshot."""

    pin: git.PinnedCommit
    config_coordinate: str
    config: ValidatedConfig
    normalized_root: str
    normalized_graph: str
    normalized_systems_dir: str
    normalized_targets: tuple[str, ...]
    graph: LoadedGraph
    graph_bytes: bytes
    sidecar: bytes
    systems: tuple[system.System, ...]
    selection: tuple[str, ...]

    @property
    def pinned(self) -> git.PinnedCommit:
        """Return the one pinned handle retained for later deletion proof."""
        return self.pin

    @property
    def worktree_root(self) -> Path:
        """Return the lexical worktree root of the retained pin."""
        return self.pin.root

    @property
    def commit(self) -> str:
        """Return the immutable commit SHA used by every observation."""
        return self.pin.commit

    @property
    def normalized_config_coordinate(self) -> str:
        """Return the caller-selected repository-relative config coordinate."""
        return self.config_coordinate

    @property
    def historical_config(self) -> ValidatedConfig:
        """Alias for the raw historical declarations."""
        return self.config

    @property
    def analysis_root(self) -> str:
        """Return the normalized historical analysis-root coordinate."""
        return self.normalized_root

    @property
    def graph_coordinate(self) -> str:
        """Return the normalized historical graph coordinate."""
        return self.normalized_graph

    @property
    def systems_coordinate(self) -> str:
        """Return the normalized historical systems coordinate."""
        return self.normalized_systems_dir

    @property
    def target_coordinates(self) -> tuple[str, ...]:
        """Return normalized historical target coordinates."""
        return self.normalized_targets

    @property
    def saved_selection(self) -> tuple[str, ...]:
        """Return the strict normalized selection saved in the graph."""
        return self.selection

    @property
    def graph_content(self) -> bytes:
        """Return the exact historical graph bytes."""
        return self.graph_bytes

    @property
    def sidecar_bytes(self) -> bytes:
        """Return the exact historical graph sidecar bytes."""
        return self.sidecar

    @property
    def targets(self) -> tuple[str, ...]:
        """Return the normalized historical target set."""
        return self.normalized_targets

    @property
    def loaded_graph(self) -> LoadedGraph:
        """Return the canonical graph loaded from the pinned bytes."""
        return self.graph

    @property
    def definitions(self) -> tuple[system.System, ...]:
        """Return the complete validated historical system set."""
        return self.systems

    def entry(self, relative: str) -> git.TreeEntry | None:
        """Look up one repository-relative entry on this result's pin."""
        return self.pin.entry(relative)


def _historical_error(
    pin: git.PinnedCommit,
    path: str,
    detail: str,
    cause: str | None = None,
) -> HistoricalInputError:
    return HistoricalInputError(
        side="historical", commit=pin.commit, path=path, detail=detail, cause=cause
    )


def _wrap_error(pin: git.PinnedCommit, path: str, detail: str, error: Exception) -> Exception:
    if isinstance(error, git.GitInputError):
        return error
    return _historical_error(pin, path, f"{detail}: {error}", cause=type(error).__name__)


def _tree_coordinate(parts: Sequence[str]) -> str:
    return "/".join(parts)


def _public_coordinate(parts: Sequence[str]) -> str:
    return _tree_coordinate(parts) or "."


def _coordinate_parts(coordinate: str) -> tuple[str, ...]:
    return () if coordinate in {"", "."} else tuple(coordinate.split("/"))


def _git_coordinate(coordinate: str) -> str:
    return "" if coordinate == "." else coordinate


def _analysis_relative_coordinate(coordinate: str, root_parts: Sequence[str]) -> str:
    parts = _coordinate_parts(coordinate)
    if parts[: len(root_parts)] != tuple(root_parts):
        raise ValueError("coordinate is outside the analysis root")
    return _public_coordinate(parts[len(root_parts) :])


def _tree_entry(coordinate: Sequence[str]) -> git.TreeEntry:
    return git.TreeEntry(path=_tree_coordinate(coordinate), mode="040000", kind="tree")


def _walk(pin: git.PinnedCommit, raw: str, *, start: Sequence[str] = ()) -> _Route:
    """Walk *raw* by listing each parent tree before normalizing it.

    A missing child proves absence only when no later ``..`` needs to resolve
    through it.  The returned coordinate remains useful for absent targets,
    whose historical absence is a valid observation for the later comparison.
    """
    if raw.startswith("/"):
        raise _historical_error(pin, raw, "path escapes the worktree")
    components = [part for part in raw.split("/") if part not in {"", "."}]
    stack = list(start)
    for index, component in enumerate(components):
        if component == "..":
            if not stack:
                raise _historical_error(pin, raw, "path escapes the worktree")
            stack.pop()
            continue

        parent = _tree_coordinate(stack)
        candidates = {entry.path.rsplit("/", 1)[-1]: entry for entry in pin.entries(parent)}
        found = candidates.get(component)
        if found is None:
            remaining = components[index + 1 :]
            if ".." in remaining:
                raise _historical_error(
                    pin,
                    raw,
                    f"unresolved traversal after missing prefix {component!r}",
                )
            return _Route(None, _public_coordinate((*stack, *components[index:])))
        if found.is_link:
            raise _historical_error(pin, found.path, "path is a symbolic link")
        if found.is_gitlink:
            raise _historical_error(pin, found.path, "path is a gitlink")
        if index < len(components) - 1 and found.kind != "tree":
            raise _historical_error(pin, found.path, "path has a blocked non-tree ancestor")
        stack.append(component)
        if index == len(components) - 1:
            return _Route(found, _public_coordinate(stack))

    return _Route(_tree_entry(stack), _public_coordinate(stack))


def _absolute_route(pin: git.PinnedCommit, raw: str) -> _Route:
    """Interpret an absolute declaration without resolving live symlinks."""
    root = Path(pin.root).absolute()
    candidate = Path(raw)
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise _historical_error(pin, raw, "absolute path escapes the worktree") from error
    return _walk(pin, "/".join(relative.parts))


def _declaration_route(pin: git.PinnedCommit, raw: str, *, start: Sequence[str] = ()) -> _Route:
    if not isinstance(raw, str):
        raise _historical_error(pin, str(raw), "path declaration must be a string")
    if raw.startswith("/"):
        return _absolute_route(pin, raw)
    return _walk(pin, raw, start=start)


def _require_regular(pin: git.PinnedCommit, route: _Route, label: str) -> str:
    entry = route.entry
    if entry is None:
        raise _historical_error(pin, label, "required path is absent")
    if not entry.is_regular_file:
        raise _historical_error(
            pin, route.coordinate or label, "required path is not a regular file"
        )
    return route.coordinate


def _require_tree(pin: git.PinnedCommit, route: _Route, label: str) -> str:
    entry = route.entry
    if entry is None:
        raise _historical_error(pin, label, "required tree is absent")
    if entry.kind != "tree":
        raise _historical_error(pin, route.coordinate or label, "required path is not a tree")
    return route.coordinate


def _raw_selection(document: object) -> object:
    extensions = getattr(document, "extensions", None)
    if not isinstance(extensions, Mapping):
        return None
    namespace = extensions.get("minotaur")
    if not isinstance(namespace, Mapping):
        return None
    return namespace.get("selection")


def _normalize_selection_member(member: str) -> str:
    if not member or member.startswith("/"):
        raise ValueError("selection members must be non-empty relative paths")
    parts: list[str] = []
    for component in member.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            if not parts:
                raise ValueError("selection member escapes the repository root")
            parts.pop()
        else:
            parts.append(component)
    return "/".join(parts) if parts else "."


def validate_saved_selection(
    raw_selection_value: object, expected_normalized_set: set[str] | frozenset[str]
) -> tuple[str, ...]:
    """Strictly validate and normalize saved graph selection metadata."""
    if not isinstance(raw_selection_value, (list, tuple)):
        raise ValueError("saved graph selection is missing or not a list/tuple")
    if not raw_selection_value:
        raise ValueError("saved graph selection must not be empty")
    normalized: list[str] = []
    for member in raw_selection_value:
        if not isinstance(member, str):
            raise ValueError("saved graph selection members must all be strings")
        normalized.append(_normalize_selection_member(member))
    unique = frozenset(normalized)
    if not unique:
        raise ValueError("saved graph selection must not be empty")
    if unique != frozenset(expected_normalized_set):
        raise ValueError(
            f"saved graph selection does not match historical targets: "
            f"{sorted(unique)!r} != {sorted(expected_normalized_set)!r}"
        )
    return tuple(sorted(unique))


def _config_root(pin: git.PinnedCommit, config_coordinate: str, raw_root: str) -> _Route:
    parent = tuple(config_coordinate.split("/")[:-1])
    return _declaration_route(pin, raw_root, start=parent)


def _lexical_source(pin: git.PinnedCommit, coordinate: str) -> Path:
    return Path(pin.root).joinpath(*_coordinate_parts(coordinate))


def _load_system_definitions(
    pin: git.PinnedCommit, systems_coordinate: str
) -> tuple[system.System, ...]:
    root_route = _walk(pin, _git_coordinate(systems_coordinate))
    if root_route.entry is None or root_route.entry.kind != "tree":
        if root_route.entry is not None and (
            root_route.entry.is_link or root_route.entry.is_gitlink
        ):
            raise _historical_error(pin, systems_coordinate, "systems root is a link")
        return ()

    definitions: dict[Path, bytes] = {}
    for child in pin.entries(_git_coordinate(systems_coordinate)):
        if child.is_link:
            raise _historical_error(pin, child.path, "system directory is a symbolic link")
        if child.is_gitlink:
            raise _historical_error(pin, child.path, "system directory is a gitlink")
        if child.kind != "tree":
            continue
        child_parts = tuple(child.path.split("/"))
        candidate = _walk(pin, "system.toml", start=child_parts)
        if candidate.entry is None:
            continue
        if candidate.entry.is_link:
            raise _historical_error(
                pin, candidate.coordinate, "system definition is a symbolic link"
            )
        if candidate.entry.is_gitlink:
            raise _historical_error(pin, candidate.coordinate, "system definition is a gitlink")
        if not candidate.entry.is_regular_file:
            continue
        source = _lexical_source(pin, candidate.coordinate)
        try:
            definitions[source] = pin.read_blob(candidate.coordinate)
        except Exception as error:
            raise _wrap_error(
                pin, candidate.coordinate, "could not read system definition", error
            ) from error
    try:
        return system.load_systems_data(definitions)
    except Exception as error:
        path = next(
            (str(source) for source in definitions if str(source) in str(error)),
            str(next(iter(definitions), Path(systems_coordinate))),
        )
        raise _wrap_error(pin, path, "invalid historical system definitions", error) from error


def load_historical_inputs(
    worktree_root: Path, selected_config_coordinate: str, validate: bool = False
) -> HistoricalInputs:
    """Acquire configuration, graph, systems, and selection from one pinned HEAD."""
    pin = git.PinnedCommit.pin(worktree_root)
    selected = _walk(pin, selected_config_coordinate)
    config_coordinate = _require_regular(pin, selected, selected_config_coordinate)
    config_source = _lexical_source(pin, config_coordinate)
    try:
        raw_config = config.parse_config_bytes(
            pin.read_blob(config_coordinate), source=config_source
        )
    except Exception as error:
        raise _wrap_error(
            pin, config_coordinate, "invalid historical configuration", error
        ) from error

    root_route = _config_root(pin, config_coordinate, raw_config.root)
    normalized_root = _require_tree(pin, root_route, raw_config.root or config_coordinate)

    root_parts = _coordinate_parts(normalized_root)
    graph_route = _declaration_route(pin, raw_config.graph, start=root_parts)
    graph_coordinate = _require_regular(pin, graph_route, raw_config.graph)
    sidecar_route = _walk(pin, graph_coordinate + ".sha256")
    sidecar_coordinate = _require_regular(pin, sidecar_route, graph_coordinate + ".sha256")
    try:
        graph_bytes = pin.read_blob(graph_coordinate)
        sidecar = pin.read_blob(sidecar_coordinate)
    except Exception as error:
        raise _wrap_error(
            pin, graph_coordinate, "could not read historical graph inputs", error
        ) from error
    try:
        loaded_graph = loading.load_graph_blob(graph_bytes, sidecar, validate=validate)
    except Exception as error:
        raise _wrap_error(
            pin, graph_coordinate, "could not load historical graph", error
        ) from error

    systems_route = _declaration_route(pin, raw_config.systems_dir, start=root_parts)
    systems_coordinate = systems_route.coordinate
    try:
        historical_systems = _load_system_definitions(pin, systems_coordinate)
    except Exception as error:
        raise _wrap_error(
            pin, systems_coordinate, "could not load historical systems", error
        ) from error

    normalized_targets: set[str] = set()
    for target in raw_config.targets:
        target_route = _declaration_route(pin, target, start=root_parts)
        target_coordinate = target_route.coordinate
        target_parts = tuple(target_coordinate.split("/")) if target_coordinate else ()
        if target_parts[: len(root_parts)] != root_parts:
            raise _historical_error(
                pin, target, "configured target escapes the historical analysis root"
            )
        normalized_targets.add(target_coordinate)

    try:
        expected_selection = {
            _analysis_relative_coordinate(target, root_parts) for target in normalized_targets
        }
        selection = validate_saved_selection(
            _raw_selection(loaded_graph.document), expected_selection
        )
    except Exception as error:
        raise _wrap_error(
            pin,
            f"{graph_coordinate}: extensions.minotaur.selection",
            "invalid historical selection",
            error,
        ) from error

    return HistoricalInputs(
        pin=pin,
        config_coordinate=config_coordinate,
        config=raw_config,
        normalized_root=normalized_root,
        normalized_graph=graph_coordinate,
        normalized_systems_dir=systems_coordinate,
        normalized_targets=tuple(sorted(normalized_targets)),
        graph=loaded_graph,
        graph_bytes=graph_bytes,
        sidecar=sidecar,
        systems=historical_systems,
        selection=selection,
    )


@dataclass(frozen=True, slots=True)
class CurrentInputs:
    """All immutable observations acquired from the current work tree."""

    config_coordinate: str
    config: ValidatedConfig
    normalized_root: str
    normalized_graph: str
    normalized_systems_dir: str
    normalized_targets: tuple[str, ...]
    systems: tuple[system.System, ...]
    selection: tuple[str, ...]

    @property
    def current_config(self) -> ValidatedConfig:
        return self.config

    @property
    def analysis_root(self) -> str:
        return self.normalized_root

    @property
    def graph_coordinate(self) -> str:
        return self.normalized_graph

    @property
    def systems_coordinate(self) -> str:
        return self.normalized_systems_dir

    @property
    def target_coordinates(self) -> tuple[str, ...]:
        return self.normalized_targets

    @property
    def definitions(self) -> tuple[system.System, ...]:
        return self.systems

    @property
    def saved_selection(self) -> tuple[str, ...]:
        return self.selection


@dataclass(frozen=True, slots=True)
class PreparedComparison:
    """The fully validated historical/current pair and reporting snapshots."""

    historical: HistoricalInputs
    current: CurrentInputs
    old_snapshot: ReportingSnapshot
    new_snapshot: ReportingSnapshot


@dataclass(frozen=True, slots=True)
class _CurrentRoute:
    path: Path
    coordinate: str
    entry: os.stat_result | None


def _current_error(
    path: str | Path,
    detail: str,
    error: Exception | None = None,
    *,
    diagnostics: Sequence[Diagnostic] = (),
) -> CurrentInputError:
    return CurrentInputError(
        path=path,
        detail=detail,
        cause=type(error).__name__ if error is not None else None,
        diagnostics=diagnostics,
    )


def _route_coordinate(parts: Sequence[str]) -> str:
    return "/".join(parts) or "."


def _relative_parts(root: Path, path: Path, *, label: str) -> tuple[str, ...]:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise _current_error(path, f"{label} escapes the selected worktree", error) from error
    return relative.parts


def _inspect_current_route(
    root: Path,
    path: Path,
    *,
    label: str,
    allow_missing: bool = True,
) -> _CurrentRoute:
    """Inspect a lexical route one component at a time, retaining aliases."""
    parts = _relative_parts(root, path, label=label)
    current = root
    logical: list[str] = []
    missing = False
    for index, component in enumerate(parts):
        if component in {"", "."}:
            continue
        if component == "..":
            if missing or not logical:
                raise _current_error(path, f"unresolved traversal in {label}")
            logical.pop()
            current = current.parent
            continue
        if missing:
            logical.append(component)
            current = current / component
            continue
        candidate = current / component
        try:
            observed = os.lstat(candidate)
        except FileNotFoundError as error:
            if ".." in parts[index + 1 :]:
                raise _current_error(
                    path, f"unresolved traversal after missing prefix {component!r}", error
                ) from error
            missing = True
            logical.append(component)
            current = candidate
            continue
        except OSError as error:
            raise _current_error(candidate, f"could not inspect {label}", error) from error
        if stat.S_ISLNK(observed.st_mode):
            raise _current_error(candidate, "path is a symbolic link")
        if component == ".git":
            raise _current_error(candidate, "path contains a nested repository marker")
        if index < len(parts) - 1 and not stat.S_ISDIR(observed.st_mode):
            raise _current_error(candidate, "path has a blocked non-directory ancestor")
        logical.append(component)
        current = candidate
        if index == len(parts) - 1:
            return _CurrentRoute(candidate, _route_coordinate(logical), observed)

    if missing:
        if not allow_missing:
            raise _current_error(path, f"required {label} is absent")
        return _CurrentRoute(current, _route_coordinate(logical), None)
    try:
        observed = os.lstat(root)
    except OSError as error:  # pragma: no cover - root was inspected by the caller.
        raise _current_error(root, f"could not inspect {label}", error) from error
    return _CurrentRoute(root, ".", observed)


def _lexical_declaration(root: Path, base: Path, raw: str, *, label: str) -> _CurrentRoute:
    if not isinstance(raw, str):
        raise _current_error(str(raw), f"{label} declaration must be a string")
    path = Path(raw) if raw.startswith("/") else base / raw
    parts = _relative_parts(root, path, label=label)
    logical: list[str] = []
    for component in parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not logical:
                raise _current_error(path, f"{label} escapes the selected worktree")
            logical.pop()
        else:
            logical.append(component)
    return _CurrentRoute(path, _route_coordinate(logical), None)


def _select_worktree(start: Path) -> tuple[Path, Path]:
    preserved_start = Path.cwd() / start if not start.is_absolute() else start
    result = git.run_git(preserved_start, ("rev-parse", "--show-toplevel"))
    if result is None:
        error = _current_error(preserved_start, "Git worktree probe was unavailable")
        raise error
    if isinstance(result.stdout, bytes):
        value = result.stdout.decode("utf-8", errors="replace").strip()
    elif isinstance(result.stdout, str):
        value = result.stdout.strip()
    else:
        value = ""
    if result.returncode != 0 or not value:
        raise _current_error(preserved_start, "Git did not return a worktree root")
    root = Path(value)
    if not root.is_absolute():
        raise _current_error(root, "Git returned a non-absolute worktree root")
    try:
        os.lstat(root)
    except OSError as error:
        raise _current_error(root, "could not inspect the Git worktree root", error) from error
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise _current_error(root, "Git worktree root is not an ordinary directory")
    start_route = _inspect_current_route(root, preserved_start, label="start", allow_missing=False)
    if start_route.entry is None or not stat.S_ISDIR(start_route.entry.st_mode):
        raise _current_error(preserved_start, "start must be an ordinary directory")
    return root, preserved_start


def _discover_current_config(root: Path, start: Path) -> _CurrentRoute:
    current = start
    while True:
        candidate = current / ".minotaur.toml"
        route = _inspect_current_route(root, candidate, label="config")
        if route.entry is not None:
            if not stat.S_ISREG(route.entry.st_mode):
                raise _current_error(candidate, "selected config is not an ordinary file")
            return route
        if route.coordinate == "." or current == root:
            raise _current_error(candidate, "no config was found within the selected worktree")
        parent = current.parent
        if parent == current:
            raise _current_error(candidate, "no config was found within the selected worktree")
        current = parent


def _current_config_route(root: Path, start: Path, raw_config_path: Path | None) -> _CurrentRoute:
    if raw_config_path is None:
        return _discover_current_config(root, start)
    selected = raw_config_path if raw_config_path.is_absolute() else start / raw_config_path
    route = _inspect_current_route(root, selected, label="config", allow_missing=False)
    if route.entry is None:
        raise _current_error(selected, "required config is absent")
    if not stat.S_ISREG(route.entry.st_mode):
        raise _current_error(selected, "selected config is not an ordinary file")
    return route


def _current_systems(root: Path, route: _CurrentRoute) -> tuple[system.System, ...]:
    if route.entry is None:
        return ()
    mode = route.entry.st_mode
    if not stat.S_ISDIR(mode):
        if stat.S_ISREG(mode):
            return ()
        raise _current_error(route.path, "systems root is not an ordinary directory")
    definitions: dict[Path, bytes] = {}
    try:
        children = sorted(os.scandir(route.path), key=lambda entry: entry.name)
    except OSError as error:
        raise _current_error(route.path, "could not inspect systems root", error) from error
    for child in children:
        child_path = route.path / child.name
        child_route = _inspect_current_route(root, child_path, label="system directory")
        if child_route.entry is None:
            continue
        child_mode = child_route.entry.st_mode
        if not stat.S_ISDIR(child_mode):
            if stat.S_ISREG(child_mode):
                continue
            raise _current_error(child_path, "system directory candidate is not ordinary")
        # A system directory is an authoritative route boundary.  Check only
        # its own repository marker; valid source targets retain ordinary
        # selector behavior without a recursive nested-repository scan.
        _inspect_current_route(root, child_path / ".git", label="nested repository marker")
        definition_path = child_path / "system.toml"
        definition = _inspect_current_route(root, definition_path, label="system definition")
        if definition.entry is None:
            continue
        if not stat.S_ISREG(definition.entry.st_mode):
            if stat.S_ISDIR(definition.entry.st_mode):
                continue
            raise _current_error(definition_path, "system definition is not an ordinary file")
        try:
            definitions[definition.path] = definition.path.read_bytes()
        except OSError as error:
            raise _current_error(
                definition.path, "could not read system definition", error
            ) from error
    try:
        return system.load_systems_data(definitions)
    except Exception as error:
        path = next(
            (str(source) for source in definitions if str(source) in str(error)), str(route.path)
        )
        raise _current_error(path, "invalid current system definitions", error) from error


def _pinned_target_entry(historical: HistoricalInputs, coordinate: str) -> git.TreeEntry | None:
    if coordinate == ".":
        return git.TreeEntry(path="", mode="040000", kind="tree")
    return historical.pin.entry(coordinate)


def prepare_comparison(
    start: Path,
    raw_config_path: Path | None,
    producer: SelectionProducer,
    validate: bool = False,
) -> PreparedComparison:
    """Acquire, validate, and publish one complete historical/current pair."""
    worktree, preserved_start = _select_worktree(start)
    selected_config = _current_config_route(worktree, preserved_start, raw_config_path)
    try:
        raw_config_bytes = selected_config.path.read_bytes()
        current_config = config.parse_config_bytes(raw_config_bytes, source=selected_config.path)
    except CurrentInputError:
        raise
    except Exception as error:
        raise _current_error(
            selected_config.path, "invalid current configuration", error
        ) from error

    root_path = _declaration_path(worktree, selected_config.path.parent, current_config.root)
    root_route = _inspect_current_route(
        worktree, root_path, label="analysis root", allow_missing=False
    )
    if root_route.entry is None or not stat.S_ISDIR(root_route.entry.st_mode):
        raise _current_error(root_path, "analysis root is not an ordinary directory")

    graph_route = _lexical_declaration(
        worktree, root_path, current_config.graph, label="configured graph"
    )
    systems_path = _declaration_path(worktree, root_path, current_config.systems_dir)
    systems_route = _inspect_current_route(worktree, systems_path, label="systems root")

    target_routes: list[_CurrentRoute] = []
    analyzed_targets: list[Path] = []
    metadata_targets: list[Path] = []
    target_coordinates: list[str] = []
    seen_existing: set[str] = set()
    for raw_target in current_config.targets:
        target_path = _declaration_path(worktree, root_path, raw_target)
        target_route = _inspect_current_route(worktree, target_path, label="target")
        root_parts = _coordinate_parts(root_route.coordinate)
        target_parts = _coordinate_parts(target_route.coordinate)
        if target_parts[: len(root_parts)] != root_parts:
            raise _current_error(target_path, "configured target escapes the current analysis root")
        target_routes.append(target_route)
        target_coordinates.append(target_route.coordinate)
        metadata_targets.append(target_route.path)
        if target_route.entry is None:
            continue
        else:
            mode = target_route.entry.st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise _current_error(target_path, "target is not an ordinary file or directory")
            if target_route.coordinate not in seen_existing:
                analyzed_targets.append(target_route.path)
                seen_existing.add(target_route.coordinate)

    # Historical acquisition is deliberately after the single current config read.
    historical = load_historical_inputs(worktree, selected_config.coordinate, validate=validate)
    current_targets = tuple(sorted(set(target_coordinates)))
    if root_route.coordinate != historical.normalized_root:
        raise _current_error(
            selected_config.path,
            f"current analysis root {root_route.coordinate!r} does not match historical "
            f"root {historical.normalized_root!r} at pinned commit {historical.commit}",
        )
    if current_targets != historical.normalized_targets:
        raise _current_error(
            selected_config.path,
            f"current targets {current_targets!r} do not match historical targets "
            f"{historical.normalized_targets!r} at pinned commit {historical.commit}",
        )

    for target_route in target_routes:
        if target_route.entry is None:
            pinned_entry = _pinned_target_entry(historical, target_route.coordinate)
            if pinned_entry is None or not (
                pinned_entry.is_regular_file or pinned_entry.kind == "tree"
            ):
                raise _current_error(
                    target_route.path,
                    f"absent current target is not proven at historical pin {historical.commit}",
                )

    current_systems = _current_systems(worktree, systems_route)
    try:
        produced_workspace, produced_selection, produced = producer(
            root_route.path,
            tuple(analyzed_targets),
            tuple(metadata_targets),
        )
    except CurrentInputError:
        raise
    except Exception as error:
        raise _current_error(root_route.path, "current source production failed", error) from error

    diagnostics = tuple(produced.diagnostics)
    if diagnostics:
        path = diagnostics[0].path if diagnostics else "<source diagnostics>"
        raise _current_error(
            path,
            "current source analysis produced diagnostics",
            diagnostics=diagnostics,
        )
    try:
        expected_selection = {
            _analysis_relative_coordinate(target, _coordinate_parts(root_route.coordinate))
            for target in current_targets
        }
        selection = validate_saved_selection(_raw_selection(produced.document), expected_selection)
    except Exception as error:
        raise _current_error("<produced graph>", "invalid produced selection", error) from error
    report = validate_document(produced.document, verify_node_ids=True)
    if not report.is_valid:
        raise _current_error("<produced graph>", f"invalid produced graph: {report.issues!r}")
    current = CurrentInputs(
        config_coordinate=selected_config.coordinate,
        config=current_config,
        normalized_root=root_route.coordinate,
        normalized_graph=graph_route.coordinate,
        normalized_systems_dir=systems_route.coordinate,
        normalized_targets=current_targets,
        systems=current_systems,
        selection=selection,
    )
    old_snapshot = ReportingSnapshot.prepare(historical.graph.document, historical.systems)
    new_snapshot = ReportingSnapshot.prepare(produced.document, current_systems)
    return PreparedComparison(historical, current, old_snapshot, new_snapshot)


def _declaration_path(root: Path, base: Path, raw: str) -> Path:
    if not isinstance(raw, str):
        raise _current_error(str(raw), "path declaration must be a string")
    return Path(raw) if raw.startswith("/") else base / raw
