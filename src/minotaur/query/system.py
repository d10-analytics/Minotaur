"""System boundary queries: ``surface``, ``consumers``, and ``system-deps``.

These three queries report who reaches across a declared system boundary.
They share one model: a query targets one declared system (D-05, R-04) and
every relationship of the boundary kinds -- the symbol layer ``calls`` /
``references`` and the module layer ``imports`` (D-06) -- is attributed by
its endpoint's *file*, through the loader's exact-file membership
(:func:`minotaur.system.classify_endpoint`).  No package, module, or label
ever implies membership (AR-03), and records expose only semantic labels,
root-relative paths, explicit relationship kinds, and the pinned category
spellings ``system: <name>`` | ``no_system`` | ``external`` -- never node
IDs (R-08, D-07).

Record producers key on the semantic participant, never on the call site
(D-05):

- ``surface(systems, index, target)`` returns one record per *exposed
  in-scope symbol* -- a symbol defined in a file the target system lists and
  reached by an inbound ``calls`` or ``references`` edge whose source sits
  outside the system.  Imports are never surface, and an edge between two
  in-scope endpoints (including a same-file edge) exposes nothing (R-05,
  AC-05).
- ``consumers(systems, index, target)`` returns one record per *outside
  file* participating in a boundary relationship into the target system,
  carrying the distinct relationship kinds that file contributes and the
  concrete in-scope targets it reaches as detail (R-06, AC-06).  An outside
  module that only imports a system module is a consumer through
  ``imports`` even when its calls never resolve.
- ``system_deps(systems, index, target)`` returns one record per *target
  category* -- each named target system plus explicit ``no_system``
  (path-carrying target in no declared system) and ``external`` (path-less
  upstream target) rows -- from the target system's own outgoing boundary
  relationships, each row carrying the deterministic nested target detail
  list (endpoint label, root-relative path, kind) package 03 extends (R-07,
  D-13, AC-07).

Text output is one deterministic line per record; an empty result has its
own text form (``no consumers`` is pinned by AC-10).  JSON rendering uses
the shared envelope from :mod:`minotaur.query.render`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import RelationshipKind
from minotaur.query.index import GraphIndex
from minotaur.system import EndpointKind, System, classify_endpoint, system_for_file

_CALLS = RelationshipKind.CALLS.value
_REFERENCES = RelationshipKind.REFERENCES.value
_IMPORTS = RelationshipKind.IMPORTS.value

#: Surface only ever reports the symbol layer (R-05): imports of a system
#: module are a consumer fact, never an exposed boundary.
_SURFACE_KINDS = (_CALLS, _REFERENCES)

#: Consumers and dependencies report both consumption layers (D-06).
_BOUNDARY_KINDS = (_CALLS, _REFERENCES, _IMPORTS)


@dataclass(frozen=True, slots=True)
class TargetDetail:
    """One concrete target endpoint reached by one relationship kind (D-13).

    ``label`` is the endpoint's semantic label, ``path`` its derived
    root-relative file (``None`` only for a path-less ``external`` endpoint),
    and ``kind`` the explicit relationship kind reaching it.  This nested
    projection is the typed row payload package 03 extends, so its field
    spellings are contract from this package forward.
    """

    label: str | None
    path: str | None
    kind: str

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind, "label": self.label}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True, slots=True)
class SurfaceRecord:
    """One exposed in-scope symbol reached from outside the system (R-05).

    ``category`` is the symbol's own membership (``system: <name>``),
    ``path`` the root-relative file defining the symbol, and ``kinds`` the
    distinct symbol-layer relationship kinds by which outside endpoints
    reach it, sorted.  A second outside call site never changes this record
    set (D-05).
    """

    category: str
    kinds: tuple[str, ...]
    path: str
    symbol: str

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "kinds": list(self.kinds),
            "path": self.path,
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class ConsumersRecord:
    """One outside file consuming the target system (R-06, D-05).

    ``file`` is the consumer's root-relative path, ``category`` the consumer
    file's own membership (``no_system`` or ``system: <name>`` for a file a
    *different* declared system lists), ``kinds`` the distinct relationship
    kinds that file contributes, and ``targets`` the concrete in-scope
    targets it reaches as nested detail.
    """

    category: str
    file: str
    kinds: tuple[str, ...]
    targets: tuple[TargetDetail, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "file": self.file,
            "kinds": list(self.kinds),
            "targets": [target.to_dict() for target in self.targets],
        }


@dataclass(frozen=True, slots=True)
class SystemDepsRecord:
    """One target category of the source system's outgoing boundary edges.

    ``category`` is the row's key -- a named target system spelled
    ``system: <name>``, ``no_system``, or ``external`` (R-07, D-13) -- and
    ``targets`` the deterministic nested detail list.  Only categories with
    at least one target produce a row.
    """

    category: str
    targets: tuple[TargetDetail, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "targets": [target.to_dict() for target in self.targets],
        }


def surface(
    systems: Sequence[System], index: GraphIndex, target: System
) -> tuple[SurfaceRecord, ...]:
    """Return one record per exposed in-scope symbol of ``target``.

    An in-scope symbol is exposed when at least one inbound ``calls`` or
    ``references`` edge reaches it from an endpoint whose derived file the
    system does not list.  Internal edges between two in-scope endpoints
    (including same-file edges) and module-layer ``imports`` expose nothing.
    Rows key on the symbol, so additional outside call sites never add a
    record (AC-05, D-05).
    """
    reached: dict[tuple[str, str], set[str]] = defaultdict(set)
    for kind in _SURFACE_KINDS:
        for relationship in index.relationships(kind):
            target_node = index.nodes.get(relationship.target)
            if target_node is None or not _in_scope(systems, target, target_node):
                continue
            source = index.nodes.get(relationship.source)
            if source is None or _in_scope(systems, target, source):
                continue
            file = _endpoint_file(systems, target_node)
            if file is None:  # pragma: no cover - in-scope implies a listed file.
                continue
            reached[(target_node.label, file)].add(kind)
    records = [
        SurfaceRecord(
            category=f"system: {target.name}",
            kinds=tuple(sorted(kinds)),
            path=file,
            symbol=symbol,
        )
        for (symbol, file), kinds in reached.items()
    ]
    return tuple(sorted(records, key=lambda record: (record.path, record.symbol)))


def consumers(
    systems: Sequence[System], index: GraphIndex, target: System
) -> tuple[ConsumersRecord, ...]:
    """Return one record per outside file consuming ``target``.

    A boundary relationship into the system (any of ``calls``, ``references``,
    ``imports``) whose source endpoint's derived file is not listed by the
    system makes that file a consumer: the row carries the file's distinct
    relationship kinds and the concrete in-scope targets it reaches as
    detail.  A path-less source endpoint has no file and so is not a
    consumer row (R-06, AC-06).
    """
    kinds_by_file: dict[str, set[str]] = defaultdict(set)
    targets_by_file: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for kind in _BOUNDARY_KINDS:
        for relationship in index.relationships(kind):
            target_node = index.nodes.get(relationship.target)
            if target_node is None or not _in_scope(systems, target, target_node):
                continue
            source = index.nodes.get(relationship.source)
            if source is None:
                continue
            source_membership = classify_endpoint(systems, source)
            if (
                source_membership.kind is EndpointKind.SYSTEM
                and source_membership.system is not None
                and source_membership.system.name == target.name
            ):
                continue  # Both endpoints sit inside the queried system.
            source_file = source_membership.file
            if source_file is None:
                continue  # Path-less upstream source: not a consumer file.
            target_file = _endpoint_file(systems, target_node)
            if target_file is None:  # pragma: no cover - in-scope implies a listed file.
                continue
            kinds_by_file[source_file].add(kind)
            targets_by_file[source_file].add((target_node.label, target_file, kind))
    records = []
    for file in sorted(kinds_by_file):
        records.append(
            ConsumersRecord(
                category=_file_category(systems, file),
                file=file,
                kinds=tuple(sorted(kinds_by_file[file])),
                targets=tuple(
                    sorted(
                        (
                            TargetDetail(label=label, path=path, kind=kind)
                            for label, path, kind in targets_by_file[file]
                        ),
                        key=_target_sort_key,
                    )
                ),
            )
        )
    return tuple(records)


def system_deps(
    systems: Sequence[System], index: GraphIndex, target: System
) -> tuple[SystemDepsRecord, ...]:
    """Return one record per target category of ``target``'s dependencies.

    Every outgoing ``calls``, ``references``, or ``imports`` edge whose
    source endpoint lies inside the system classifies its target endpoint:
    a target a *different* declared system lists makes a ``system: <name>``
    row, a path-carrying target in no declared system a ``no_system`` row,
    and a path-less upstream target an ``external`` row.  Same-system and
    same-file edges are internal and never a dependency; no target is
    silently attributed to a system or dropped (R-07, D-13, AC-07).
    """
    targets_by_category: dict[str, set[tuple[str | None, str | None, str]]] = defaultdict(set)
    for kind in _BOUNDARY_KINDS:
        for relationship in index.relationships(kind):
            source = index.nodes.get(relationship.source)
            if source is None or not _in_scope(systems, target, source):
                continue
            target_node = index.nodes.get(relationship.target)
            if target_node is None:
                continue
            membership = classify_endpoint(systems, target_node)
            if membership.kind is EndpointKind.SYSTEM and membership.system is not None:
                if membership.system.name == target.name:
                    continue  # Internal dependency, not a boundary one.
                category = f"system: {membership.system.name}"
            elif membership.kind is EndpointKind.NO_SYSTEM:
                category = "no_system"
            else:
                category = "external"
            targets_by_category[category].add((target_node.label, membership.file, kind))
    records = []
    for category in sorted(targets_by_category):
        records.append(
            SystemDepsRecord(
                category=category,
                targets=tuple(
                    sorted(
                        (
                            TargetDetail(label=label, path=path, kind=kind)
                            for label, path, kind in targets_by_category[category]
                        ),
                        key=_target_sort_key,
                    )
                ),
            )
        )
    return tuple(records)


def render_surface_text(records: Sequence[SurfaceRecord]) -> str:
    """Render one line per exposed symbol: file, symbol, and reaching kinds."""
    if not records:
        return "no exposed symbols\n"
    return "".join(
        f"{record.path}  {record.symbol}  {', '.join(record.kinds)}\n" for record in records
    )


def render_consumers_text(records: Sequence[ConsumersRecord]) -> str:
    """Render one line per consumer file with its grouped target detail."""
    if not records:
        return "no consumers\n"
    return "".join(
        f"{record.file} ({record.category})  {_render_groups(record.targets)}\n"
        for record in records
    )


def render_system_deps_text(records: Sequence[SystemDepsRecord]) -> str:
    """Render one line per target category with its grouped target detail."""
    if not records:
        return "no dependencies\n"
    return "".join(f"{record.category}  {_render_groups(record.targets)}\n" for record in records)


def _in_scope(systems: Sequence[System], target: System, node: Node) -> bool:
    """Return whether ``node`` derives from a file ``target`` lists (R-04)."""
    membership = classify_endpoint(systems, node)
    return (
        membership.kind is EndpointKind.SYSTEM
        and membership.system is not None
        and membership.system.name == target.name
    )


def _endpoint_file(systems: Sequence[System], node: Node) -> str | None:
    """Return the classified endpoint's derived file, or ``None`` (external)."""
    membership = classify_endpoint(systems, node)
    return membership.file


def _file_category(systems: Sequence[System], file: str) -> str:
    """Spell the membership category of one path-carrying file (D-07)."""
    owner = system_for_file(systems, file)
    if owner is None:
        return "no_system"
    return f"system: {owner.name}"


def _target_sort_key(target: TargetDetail) -> tuple[str, str, str]:
    """Deterministically order one nested target detail list (D-13)."""
    return (target.kind, target.label or "", target.path or "")


def _render_groups(targets: Iterable[TargetDetail]) -> str:
    """Render a nested target list grouped by kind, deterministic order."""
    groups: dict[str, list[str]] = defaultdict(list)
    for target in sorted(targets, key=_target_sort_key):
        entry = target.label or ""
        if target.path is not None:
            entry = f"{entry} ({target.path})"
        groups[target.kind].append(entry)
    return "; ".join(f"{kind}: {', '.join(groups[kind])}" for kind in sorted(groups))
