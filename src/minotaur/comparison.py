"""Acquire complete historical and current inputs for a no-write comparison.

Both sides are captured from their own revision or work tree and analyzed from
source; no committed graph or sidecar is read. Current routes are anchored and
inspected before source production; shared loaders validate definitions on both
sides. Only a comparable, fully validated pair is published to reporting
consumers.
"""

from __future__ import annotations

import inspect
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

from minotaur import config, git, system
from minotaur.comparison_snapshot import (
    CaptureSelection,
    CaptureStage,
    RevisionSnapshot,
    capture_pair,
)
from minotaur.config import SqlSettings, ValidatedConfig
from minotaur.graph_model import loading
from minotaur.graph_model.loading import LoadedGraph
from minotaur.graph_model.serialization import serialize
from minotaur.graph_model.validation import validate_document
from minotaur.language_interpreter import exclusions
from minotaur.language_interpreter.contract import AnalysisResult, Diagnostic
from minotaur.language_interpreter.registry import default_registry
from minotaur.language_interpreter.selection import SourceSelection
from minotaur.language_interpreter.workspace import Workspace
from minotaur.query.system import ReportingSnapshot


class CurrentInputError(ValueError):
    """A current input failed during strict comparison acquisition."""

    def __init__(
        self,
        *,
        path: str | Path,
        detail: str,
        cause: str | None = None,
        diagnostics: Sequence[Diagnostic] = (),
        side: str = "current",
        revision: str | None = None,
    ) -> None:
        self.side = side
        self.revision = revision
        self.path = str(path)
        self.detail = detail
        self.cause = cause
        self.cause_type = cause
        self.diagnostics = tuple(diagnostics)
        self.source_diagnostics = self.diagnostics
        identity = f" ({cause})" if cause else ""
        revision_text = f" revision {revision!r}" if revision else ""
        super().__init__(f"{side} input{revision_text} at {self.path!r}{identity}: {detail}")


class SelectionProducer(Protocol):
    """The existing in-memory source producer used by comparison."""

    def __call__(
        self,
        root: Path,
        targets: tuple[Path, ...],
        metadata_targets: tuple[Path, ...] | None = None,
        sql_settings: SqlSettings | None = None,
    ) -> tuple[Workspace, SourceSelection, AnalysisResult]: ...


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
    diagnostics: tuple[Diagnostic, ...] = ()

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


def _tree_coordinate(parts: Sequence[str]) -> str:
    return "/".join(parts)


def _public_coordinate(parts: Sequence[str]) -> str:
    return _tree_coordinate(parts) or "."


def _coordinate_parts(coordinate: str) -> tuple[str, ...]:
    return () if coordinate in {"", "."} else tuple(coordinate.split("/"))


def _analysis_relative_coordinate(coordinate: str, root_parts: Sequence[str]) -> str:
    parts = _coordinate_parts(coordinate)
    if parts[: len(root_parts)] != tuple(root_parts):
        raise ValueError("coordinate is outside the analysis root")
    return _public_coordinate(parts[len(root_parts) :])


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
    diagnostics: tuple[Diagnostic, ...] = ()

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
    side: str = "current",
    revision: str | None = None,
) -> CurrentInputError:
    return CurrentInputError(
        path=path,
        detail=detail,
        cause=type(error).__name__ if error is not None else None,
        diagnostics=diagnostics,
        side=side,
        revision=revision,
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
    observed_directories: list[tuple[Path, os.stat_result]] = []
    missing = False
    for index, component in enumerate(parts):
        if component in {"", "."}:
            continue
        if component == "..":
            if missing or not logical:
                raise _current_error(path, f"unresolved traversal in {label}")
            logical.pop()
            current = current.parent
            observed_directories.pop()
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
        if stat.S_ISDIR(observed.st_mode):
            marker = candidate / ".git"
            try:
                marker_info = os.lstat(marker)
            except FileNotFoundError:
                marker_info = None
            except OSError as error:
                raise _current_error(
                    marker, "could not inspect nested repository marker", error
                ) from error
            if marker_info is not None:
                if stat.S_ISLNK(marker_info.st_mode):
                    raise _current_error(marker, "nested repository marker is a symbolic link")
                if stat.S_ISDIR(marker_info.st_mode) or stat.S_ISREG(marker_info.st_mode):
                    raise _current_error(candidate, "path contains a nested repository")
                raise _current_error(marker, "nested repository marker is not an ordinary entry")
            observed_directories.append((candidate, observed))
        logical.append(component)
        current = candidate
        if index == len(parts) - 1:
            return _CurrentRoute(candidate, _route_coordinate(logical), observed)

    if missing:
        if not allow_missing:
            raise _current_error(path, f"required {label} is absent")
        return _CurrentRoute(current, _route_coordinate(logical), None)
    if observed_directories:
        candidate, observed = observed_directories[-1]
        return _CurrentRoute(candidate, _route_coordinate(logical), observed)
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


@dataclass(frozen=True, slots=True)
class _CapturedAnalysis:
    """One complete analysis produced from one immutable captured root."""

    snapshot: RevisionSnapshot
    config_coordinate: str
    config: ValidatedConfig
    normalized_root: str
    normalized_graph: str
    normalized_systems_dir: str
    normalized_targets: tuple[str, ...]
    systems: tuple[system.System, ...]
    selection: tuple[str, ...]
    graph: LoadedGraph
    graph_bytes: bytes
    diagnostics: tuple[Diagnostic, ...] = ()


def _relabel_snapshot_error(
    error: CurrentInputError, snapshot: RevisionSnapshot
) -> CurrentInputError:
    """Attach a route or producer failure to the captured side that observed it."""
    public_path = error.path
    with suppress(TypeError, ValueError):
        public_path = Path(error.path).relative_to(snapshot.root).as_posix()
    if (
        error.side == snapshot.side
        and error.revision == snapshot.revision
        and public_path == error.path
    ):
        return error
    return _current_error(
        public_path,
        error.detail,
        diagnostics=error.diagnostics,
        side=snapshot.side,
        revision=snapshot.revision,
    )


def _captured_declaration_path(
    worktree_root: Path, capture_root: Path, base: Path, raw: str
) -> Path:
    """Map declarations written for the live checkout into a captured root."""
    if not isinstance(raw, str):
        raise _current_error(str(raw), "path declaration must be a string")
    if not raw.startswith("/"):
        return base / raw
    candidate = Path(raw)
    try:
        relative = candidate.relative_to(worktree_root.resolve())
    except ValueError:
        return candidate
    return capture_root / relative


def _refuse_outside_config_folder(
    config_route: _CurrentRoute,
    root_route: _CurrentRoute,
    graph_route: _CurrentRoute,
    systems_route: _CurrentRoute,
) -> None:
    """Refuse a declared route whose normalized coordinate leaves the config folder.

    Route inspection has already rejected worktree escapes, links, and
    unresolved traversal, so only the narrower config-folder bound remains.
    The graph route is compared lexically because comparison deliberately
    allows a saved-graph link and never reads it.
    """
    folder = _coordinate_parts(config_route.coordinate)[:-1]
    allowed = _route_coordinate(folder)
    for field, route in (
        ("root", root_route),
        ("graph", graph_route),
        ("systems_dir", systems_route),
    ):
        if _coordinate_parts(route.coordinate)[: len(folder)] != folder:
            raise _current_error(
                route.path,
                f"configured {field} escapes the config folder (allowed folder is {allowed})",
            )


def _captured_analysis(
    snapshot: RevisionSnapshot,
    config_coordinate: str,
    producer: SelectionProducer,
    *,
    validate: bool,
) -> _CapturedAnalysis:
    """Parse declarations and analyze only the bytes in *snapshot*.

    The saved graph and sidecar are deliberately never read here. They are
    outputs of an earlier analysis, not evidence for the source currently being
    compared. Both old and new sides use this same route/selection/diagnostic
    path, which prevents a live checkout path or the other side's root from
    entering a result.
    """
    root = snapshot.root
    config_path = root.joinpath(*_coordinate_parts(config_coordinate))
    try:
        config_route = _inspect_current_route(
            root, config_path, label="config", allow_missing=False
        )
        if config_route.entry is None:
            raise _current_error(config_path, "required config is absent")
        try:
            raw_config = config.parse_config_bytes(config_path.read_bytes(), source=config_path)
        except Exception as error:
            raise _current_error(config_path, "invalid captured configuration", error) from error

        root_path = _captured_declaration_path(
            snapshot.pin.root, root, config_path.parent, raw_config.root
        )
        root_route = _inspect_current_route(
            root, root_path, label="analysis root", allow_missing=False
        )
        if root_route.entry is None or not stat.S_ISDIR(root_route.entry.st_mode):
            raise _current_error(root_path, "analysis root is not an ordinary directory")
        graph_path = _captured_declaration_path(
            snapshot.pin.root, root, root_path, raw_config.graph
        )
        graph_route = _lexical_declaration(
            root, root_path, str(graph_path), label="configured graph"
        )
        systems_path = _captured_declaration_path(
            snapshot.pin.root, root, root_path, raw_config.systems_dir
        )
        systems_route = _inspect_current_route(root, systems_path, label="systems root")
        _refuse_outside_config_folder(config_route, root_route, graph_route, systems_route)

        analyzed_targets: list[Path] = []
        metadata_targets: list[Path] = []
        target_coordinates: list[str] = []
        seen_existing: set[str] = set()
        root_parts = _coordinate_parts(root_route.coordinate)
        for raw_target in raw_config.targets:
            target_path = _captured_declaration_path(snapshot.pin.root, root, root_path, raw_target)
            target_route = _inspect_current_route(root, target_path, label="target")
            target_parts = _coordinate_parts(target_route.coordinate)
            if target_parts[: len(root_parts)] != root_parts:
                raise _current_error(
                    target_path, "configured target escapes the captured analysis root"
                )
            metadata_targets.append(target_route.path)
            target_coordinates.append(target_route.coordinate)
            if target_route.entry is None:
                continue
            mode = target_route.entry.st_mode
            if stat.S_ISREG(mode) and not default_registry().supports(target_route.path):
                raise _current_error(target_path, "target is an unsupported source file")
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise _current_error(target_path, "target is not an ordinary file or directory")
            if target_route.coordinate not in seen_existing:
                analyzed_targets.append(target_route.path)
                seen_existing.add(target_route.coordinate)

        systems = _current_systems(root, systems_route)
        try:
            producer_arguments = (
                root_route.path,
                tuple(analyzed_targets),
                tuple(metadata_targets),
            )
            if "sql_settings" in inspect.signature(producer).parameters:
                workspace, selected, produced = producer(
                    *producer_arguments,
                    sql_settings=raw_config.sql,
                )
            else:
                workspace, selected, produced = producer(*producer_arguments)
        except CurrentInputError:
            raise
        except Exception as error:
            raise _current_error(
                root_route.path, "captured source production failed", error
            ) from error

        expected_root = root_route.path.resolve()
        if workspace.root != expected_root:
            raise _current_error(
                workspace.root,
                f"producer workspace crossed the {snapshot.side} capture root",
            )
        for source in selected.files:
            try:
                source.resolve().relative_to(expected_root)
            except ValueError as error:
                raise _current_error(
                    source, f"producer selected a path outside the {snapshot.side} capture root"
                ) from error

        diagnostics = tuple(produced.diagnostics)
        errors = tuple(item for item in diagnostics if item.is_error)
        if errors:
            path = errors[0].path
            raise _current_error(
                path,
                "captured source analysis produced diagnostics",
                diagnostics=diagnostics,
            )
        current_targets = tuple(sorted(set(target_coordinates)))
        expected_selection = {
            _analysis_relative_coordinate(target, root_parts) for target in current_targets
        }
        try:
            selection = validate_saved_selection(
                _raw_selection(produced.document), expected_selection
            )
        except Exception as error:
            raise _current_error("<produced graph>", "invalid produced selection", error) from error
        report = validate_document(produced.document, verify_node_ids=True)
        if not report.is_valid:
            raise _current_error("<produced graph>", f"invalid produced graph: {report.issues!r}")
        graph_bytes = serialize(produced.document)
        graph = loading.load_graph_blob(graph_bytes, validate=validate)
        snapshot.verify_unchanged()
        return _CapturedAnalysis(
            snapshot=snapshot,
            config_coordinate=config_coordinate,
            config=raw_config,
            normalized_root=root_route.coordinate,
            normalized_graph=graph_route.coordinate,
            normalized_systems_dir=systems_route.coordinate,
            normalized_targets=current_targets,
            systems=systems,
            selection=selection,
            graph=graph,
            graph_bytes=graph_bytes,
            diagnostics=diagnostics,
        )
    except CurrentInputError as error:
        raise _relabel_snapshot_error(error, snapshot) from error.__cause__


def _capture_selection(stage: CaptureStage, config_coordinate: str) -> CaptureSelection:
    """Derive one side's capture selection from its staged config bytes.

    The selection holds the config, the analysis root (as a directory only),
    ``systems_dir`` (unpruned) and each target (pruned by the analysis's own
    exclusion rule unless the target itself is excluded, judged relative to
    the analysis root). An absent or non-regular config, or an unparseable
    one, contributes only the config coordinate. A declaration that leaves
    the capture or the analysis root stops the derivation and keeps what was
    resolved before it, so analysis of the captured side reports that error
    rather than a consequence of a missing root.
    """
    config_only = CaptureSelection(files=(config_coordinate,))
    root = stage.root
    config_path = root.joinpath(*_coordinate_parts(config_coordinate))
    worktree_root = stage.pin.root
    directories: set[str] = set()
    trees: dict[str, bool] = {}

    def resolve(base: Path, raw: str, label: str) -> tuple[_CurrentRoute, set[str]]:
        path = _captured_declaration_path(worktree_root, root, base, raw)
        route = _lexical_declaration(root, base, str(path), label=label)
        # A ``..`` is walked physically by analysis, so every directory it
        # passes through must be captured as well.
        walked: list[str] = []
        passed: set[str] = set()
        for part in _relative_parts(root, route.path, label=label):
            if part in {"", "."}:
                continue
            if part == "..":
                walked.pop()
            else:
                walked.append(part)
                passed.add("/".join(walked))
        return route, passed

    try:
        if not stat.S_ISREG(os.lstat(config_path).st_mode):
            return config_only
        raw_config = config.parse_config_bytes(config_path.read_bytes(), source=config_path)
    except Exception:
        return config_only
    try:
        root_route, passed = resolve(config_path.parent, raw_config.root, "analysis root")
        directories |= passed | {root_route.coordinate}
        root_parts = _coordinate_parts(root_route.coordinate)
        systems_route, passed = resolve(root_route.path, raw_config.systems_dir, "systems root")
        directories |= passed
        trees[systems_route.coordinate] = False
        for raw_target in raw_config.targets:
            target_route, passed = resolve(root_route.path, raw_target, "target")
            target = target_route.coordinate
            directories |= passed - {target}
            if _coordinate_parts(target)[: len(root_parts)] != root_parts:
                break
            relative = _analysis_relative_coordinate(target, root_parts)
            # An unpruned subtree wins over a pruned one at the same coordinate.
            trees[target] = trees.get(target, True) and not exclusions.is_excluded(Path(relative))
    except Exception:
        pass
    return CaptureSelection(files=(config_coordinate,), directories=directories, trees=trees)


def _require_config_coordinate(value: str | None) -> str:
    if value is None:  # pragma: no cover - an override, shared path, or discovery supplies one.
        raise ValueError("no configuration path could be determined for a comparison side")
    return value


def _discovery_candidates(worktree: Path, preserved_start: Path) -> tuple[str, ...]:
    """Name every ``.minotaur.toml`` the discovery walk can inspect."""
    candidates: list[str] = []
    current = preserved_start
    while True:
        candidates.append(
            _route_coordinate((*current.relative_to(worktree).parts, ".minotaur.toml"))
        )
        if current == worktree or current.parent == current:
            return tuple(candidates)
        current = current.parent


def _comparison_plan(
    worktree: Path,
    preserved_start: Path,
    shared: str | None,
    before_override: str | None,
    after_override: str | None,
    before_stage: CaptureStage,
    after_stage: CaptureStage,
) -> tuple[CaptureSelection, tuple[str, str]]:
    """Plan one comparison capture: stage configs, fix coordinates, select.

    Each side first stages its explicit config coordinate and, when a side has
    none, the discovery candidates from the start directory to the worktree
    root, all in one ``include`` call. The unchanged live-route discovery then
    runs on the staged after root. The selection of each side comes from the
    config bytes staged for it, so the bytes the selection was derived from
    are the bytes analyzed. Returns the union selection and the
    ``(before, after)`` config coordinates.
    """
    needs_discovery = shared is None and (before_override is None or after_override is None)
    candidates = _discovery_candidates(worktree, preserved_start) if needs_discovery else ()
    explicit = (before_override or shared, after_override or shared)
    for stage, coordinate in ((before_stage, explicit[0]), (after_stage, explicit[1])):
        stage.include((*([coordinate] if coordinate else []), *candidates))
    discovered: str | None = None
    if needs_discovery:
        after_start = after_stage.root / preserved_start.relative_to(worktree)
        discovered = _current_config_route(after_stage.root, after_start, None).coordinate
    before_coordinate = _require_config_coordinate(explicit[0] or discovered)
    after_coordinate = _require_config_coordinate(explicit[1] or discovered)
    selection = _capture_selection(before_stage, before_coordinate).union(
        _capture_selection(after_stage, after_coordinate)
    )
    return selection, (before_coordinate, after_coordinate)


def _validate_live_current_contract(worktree: Path, selected_config: _CurrentRoute) -> None:
    """Reject unsafe current declarations before either side is analyzed."""
    try:
        current_config = config.parse_config_bytes(
            selected_config.path.read_bytes(), source=selected_config.path
        )
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
    _refuse_outside_config_folder(selected_config, root_route, graph_route, systems_route)
    _current_systems(worktree, systems_route)
    root_parts = _coordinate_parts(root_route.coordinate)
    for raw_target in current_config.targets:
        target_path = _declaration_path(worktree, root_path, raw_target)
        target_route = _inspect_current_route(worktree, target_path, label="target")
        target_parts = _coordinate_parts(target_route.coordinate)
        if target_parts[: len(root_parts)] != root_parts:
            raise _current_error(target_path, "configured target escapes the current analysis root")
        if target_route.entry is None:
            continue
        mode = target_route.entry.st_mode
        if stat.S_ISREG(mode) and not default_registry().supports(target_route.path):
            raise _current_error(target_path, "target is an unsupported source file")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise _current_error(target_path, "target is not an ordinary file or directory")


def prepare_comparison(
    start: Path,
    raw_config_path: Path | None,
    producer: SelectionProducer,
    validate: bool = False,
) -> PreparedComparison:
    """Analyze a complete old/new pair from isolated source captures.

    The old side is a pinned ``HEAD`` tree and the new side is a copy of the
    working tree's analyzed selection, so untracked and unstaged source in it
    participate without mutating the checkout. Each side parses its own config and system
    declarations and runs the same producer against its own root. Saved graph
    files and sidecars are intentionally not inputs to this operation.
    """
    worktree, preserved_start = _select_worktree(start)
    selected_config = _current_config_route(worktree, preserved_start, raw_config_path)
    _validate_live_current_contract(worktree, selected_config)
    plan = partial(
        _comparison_plan, worktree, preserved_start, selected_config.coordinate, None, None
    )
    before, after, (before_coordinate, after_coordinate) = capture_pair(
        worktree, "HEAD", None, plan=plan
    )
    try:
        with before, after:
            old = _captured_analysis(before, before_coordinate, producer, validate=validate)
            new = _captured_analysis(after, after_coordinate, producer, validate=True)
            historical = HistoricalInputs(
                pin=before.pin,
                config_coordinate=old.config_coordinate,
                config=old.config,
                normalized_root=old.normalized_root,
                normalized_graph=old.normalized_graph,
                normalized_systems_dir=old.normalized_systems_dir,
                normalized_targets=old.normalized_targets,
                graph=old.graph,
                graph_bytes=old.graph_bytes,
                sidecar=(loading.graph_digest(old.graph_bytes) + "\n").encode("ascii"),
                systems=old.systems,
                selection=old.selection,
                diagnostics=old.diagnostics,
            )
            current = CurrentInputs(
                config_coordinate=new.config_coordinate,
                config=new.config,
                normalized_root=new.normalized_root,
                normalized_graph=new.normalized_graph,
                normalized_systems_dir=new.normalized_systems_dir,
                normalized_targets=new.normalized_targets,
                systems=new.systems,
                selection=new.selection,
                diagnostics=new.diagnostics,
            )
            old_reporting = ReportingSnapshot.prepare(old.graph.document, old.systems)
            new_reporting = ReportingSnapshot.prepare(new.graph.document, new.systems)
            return PreparedComparison(historical, current, old_reporting, new_reporting)
    except CurrentInputError:
        raise


def _declaration_path(root: Path, base: Path, raw: str) -> Path:
    if not isinstance(raw, str):
        raise _current_error(str(raw), "path declaration must be a string")
    return Path(raw) if raw.startswith("/") else base / raw
