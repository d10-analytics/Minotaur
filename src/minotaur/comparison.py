"""Read one complete historical Minotaur snapshot from a pinned commit.

Historical acquisition is intentionally independent from current workspace
discovery.  A single :class:`~minotaur.git.PinnedCommit` supplies every tree
listing and blob, while this module interprets the declarations in the
historical configuration and composes the existing strict loaders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from minotaur import config, git, system
from minotaur.config import ValidatedConfig
from minotaur.graph_model import loading
from minotaur.graph_model.loading import LoadedGraph


class HistoricalInputError(git.GitInputError):
    """A historical input failed after the commit had been pinned."""


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
        selection = validate_saved_selection(
            _raw_selection(loaded_graph.document), normalized_targets
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
