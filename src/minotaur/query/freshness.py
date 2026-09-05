"""Content-based freshness checks for analyzed source selections."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import NodeClass
from minotaur.language_interpreter.registry import default_registry
from minotaur.language_interpreter.selection import SelectionError, select_sources


@dataclass(frozen=True, slots=True)
class Drift:
    """Differences between a graph's recorded selection and the workspace.

    Values are root-relative POSIX paths, sorted for deterministic CLI output.
    ``changed`` is based on bytes, never mtimes: a source can be restored to
    the same content while retaining a newer mtime and remains fresh.
    """

    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    added: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Whether no recorded or newly selected source has drifted."""
        return not (self.changed or self.missing or self.added)

    @property
    def paths(self) -> tuple[str, ...]:
        """Return all stale paths once, in stable order."""
        return tuple(sorted(set(self.changed) | set(self.missing) | set(self.added)))


@dataclass(frozen=True, slots=True)
class RecordedSelection:
    """Presence-aware view of the saved source selection.

    ``targets`` intentionally uses the exact filtering and sorting applied by
    :func:`recorded_selection`.  ``recorded`` distinguishes an explicitly
    recorded empty list from missing or malformed metadata, which is needed
    by reporting without changing refresh's refusal policy.
    """

    recorded: bool
    targets: tuple[str, ...]


def recorded_selection_view(document: GraphDocument) -> RecordedSelection:
    """Return normalized saved-selection targets together with presence."""
    extensions = document.extensions or {}
    minotaur = extensions.get("minotaur", {})
    if not isinstance(minotaur, Mapping):
        return RecordedSelection(recorded=False, targets=())
    selection = minotaur.get("selection")
    if not isinstance(selection, (list, tuple)):
        return RecordedSelection(recorded=False, targets=())
    return RecordedSelection(
        recorded=True,
        targets=tuple(sorted(item for item in selection if isinstance(item, str))),
    )


def recorded_selection(document: GraphDocument) -> tuple[str, ...]:
    """Return the root-relative targets recorded by ``analyze``.

    Graphs produced before selection metadata existed are intentionally
    treated as having no recorded targets.  Query callers can then choose an
    explicit policy for such graphs instead of guessing a source tree.
    """
    return recorded_selection_view(document).targets


def drift(document: GraphDocument, root: Path) -> Drift:
    """Compare recorded file hashes and directory selections with ``root``.

    ``changed`` and ``missing`` are derived from file nodes in the graph.
    ``added`` is limited to supported files discovered below recorded
    directory targets, so an unrelated new file does not force a refresh.
    Directory discovery is delegated to the shared source-selection layer to
    preserve its containment, symlink, and ignored-directory rules.
    """
    workspace_root = root.resolve()
    files = _file_nodes(document)
    changed: list[str] = []
    missing: list[str] = []
    for relative, node in files.items():
        source = workspace_root / relative
        if not source.is_file():
            missing.append(relative)
            continue
        expected = content_sha256(node)
        try:
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError:
            changed.append(relative)
            continue
        if expected != actual:
            changed.append(relative)

    added = _added_files(workspace_root, recorded_selection(document), set(files))
    return Drift(tuple(sorted(changed)), tuple(sorted(missing)), tuple(sorted(added)))


def _file_nodes(document: GraphDocument) -> dict[str, Node]:
    """Index valid file-node paths without relying on node IDs."""
    return {
        node.path: node
        for node in document.nodes
        if node.node_class == NodeClass.FILE and node.path is not None
    }


def content_sha256(node: Node) -> str | None:
    """Return the recorded producer content hash for a registered path.

    Shared by ``drift`` and ``query.context``, which both need the same
    recorded-hash lookup: one to compare against the current workspace, the
    other to decide whether a comparison is possible at all.
    """
    extensions = node.extensions or {}
    registration = default_registry().registration_for(Path(node.path)) if node.path else None
    namespace = registration.namespace if registration else None
    language = extensions.get(namespace, {}) if namespace else {}
    digest = language.get("content_sha256")
    return digest if isinstance(digest, str) else None


def _added_files(root: Path, selection: tuple[str, ...], recorded: set[str]) -> tuple[str, ...]:
    current: set[str] = set()
    for target in selection:
        candidate = root / target
        if not candidate.exists():
            continue
        try:
            _, selected = select_sources(root, (candidate,), default_registry())
        except (OSError, SelectionError, ValueError):
            continue
        for source in selected.files:
            relative = source.relative_to(root).as_posix()
            current.add(relative)
    return tuple(sorted(current - recorded))
