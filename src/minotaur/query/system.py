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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar, overload

from minotaur.graph_model._parsing import _jcs_serialize
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import RelationshipKind
from minotaur.graph_model.relationship import Relationship
from minotaur.query.freshness import recorded_selection_view
from minotaur.query.index import GraphIndex
from minotaur.system import EndpointKind, System, classify_endpoint, resolve_system, system_for_file

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


# ---------------------------------------------------------------------------
# Snapshot-bound reporting projection (D-08)
# ---------------------------------------------------------------------------

_QUERY_NAMES = ("surface", "consumers", "system-deps")
RecordT = TypeVar("RecordT", SurfaceRecord, ConsumersRecord, SystemDepsRecord)


def _freeze_json(value: object) -> object:
    """Recursively freeze JSON containers held by reporting values."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    """Return fresh mutable JSON containers for a public ``to_dict`` view."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SystemCoverage:
    """Immutable five-key coverage projection for one prepared system."""

    selection: Mapping[str, object]
    graph_files: Mapping[str, object]
    declared_files: Mapping[str, object]
    recorded_unresolved_references: Mapping[str, object]
    source_diagnostics: Mapping[str, object]

    def __post_init__(self) -> None:
        for name in (
            "selection",
            "graph_files",
            "declared_files",
            "recorded_unresolved_references",
            "source_diagnostics",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a mapping")
            object.__setattr__(self, name, _freeze_json(value))

    def to_dict(self) -> dict[str, object]:
        return {
            "selection": _thaw_json(self.selection),
            "graph_files": _thaw_json(self.graph_files),
            "declared_files": _thaw_json(self.declared_files),
            "recorded_unresolved_references": _thaw_json(self.recorded_unresolved_references),
            "source_diagnostics": _thaw_json(self.source_diagnostics),
        }


@dataclass(frozen=True, slots=True)
class EndpointDetail:
    """Exact semantic and graph identity projection of one endpoint."""

    id: str
    label: str
    node_class: str
    semantic_identity: Mapping[str, object]
    path: Mapping[str, object]
    location: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_identity", _freeze_json(self.semantic_identity))
        object.__setattr__(self, "path", _freeze_json(self.path))
        object.__setattr__(self, "location", _freeze_json(self.location))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "node_class": self.node_class,
            "semantic_identity": _thaw_json(self.semantic_identity),
            "path": _thaw_json(self.path),
            "location": _thaw_json(self.location),
        }


@dataclass(frozen=True, slots=True)
class EvidenceDetail:
    """One immutable attributed evidence record and all its sites."""

    provenance: str
    producer: Mapping[str, object]
    rule: Mapping[str, object]
    evidence_extensions: Mapping[str, object]
    sites: tuple[Mapping[str, object], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "producer", _freeze_json(self.producer))
        object.__setattr__(self, "rule", _freeze_json(self.rule))
        object.__setattr__(self, "evidence_extensions", _freeze_json(self.evidence_extensions))
        object.__setattr__(
            self,
            "sites",
            tuple(_freeze_json(site) for site in self.sites),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provenance": self.provenance,
            "producer": _thaw_json(self.producer),
            "rule": _thaw_json(self.rule),
            "evidence_extensions": _thaw_json(self.evidence_extensions),
            "sites": [_thaw_json(site) for site in self.sites],
        }


@dataclass(frozen=True, slots=True)
class RelationshipDetail:
    """One qualifying original edge with endpoint and evidence projections."""

    source: EndpointDetail
    target: EndpointDetail
    kind: str
    relationship_extensions: Mapping[str, object]
    evidence: tuple[EvidenceDetail, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "relationship_extensions", _freeze_json(self.relationship_extensions)
        )
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "kind": self.kind,
            "relationship_extensions": _thaw_json(self.relationship_extensions),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class QueryInvocation:
    """Immutable freshness facts from one graph query invocation."""

    refreshed: bool
    stale: tuple[str, ...] = field(default_factory=tuple)
    source_diagnostics: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.refreshed, bool):
            raise ValueError("refreshed must be a boolean")
        if not isinstance(self.stale, (list, tuple)) or any(
            not isinstance(path, str) for path in self.stale
        ):
            raise ValueError("stale must be a sequence of strings")
        object.__setattr__(self, "stale", tuple(sorted(self.stale)))
        if self.source_diagnostics is not None and (
            not isinstance(self.source_diagnostics, int)
            or isinstance(self.source_diagnostics, bool)
            or self.source_diagnostics < 0
        ):
            raise ValueError("source_diagnostics must be a non-negative integer or None")
        if self.refreshed != (self.source_diagnostics is not None):
            raise ValueError("refreshed and source_diagnostics disagree")

    def to_dict(self) -> dict[str, object]:
        return {
            "refreshed": self.refreshed,
            "stale": list(self.stale),
            "source_diagnostics": (
                {"status": "observed_on_refresh", "count": self.source_diagnostics}
                if self.source_diagnostics is not None
                else {"status": "unavailable"}
            ),
        }


@dataclass(frozen=True, slots=True)
class SystemReport(Generic[RecordT]):
    """Pure report produced from one immutable prepared graph snapshot."""

    query: str
    system_name: str
    results: tuple[RecordT, ...]
    coverage: SystemCoverage
    relationships: tuple[RelationshipDetail, ...] | None = None

    def __post_init__(self) -> None:
        expected: type[object]
        if self.query == "surface":
            expected = SurfaceRecord
        elif self.query == "consumers":
            expected = ConsumersRecord
        elif self.query == "system-deps":
            expected = SystemDepsRecord
        else:
            raise ValueError(f"unknown system query: {self.query}")
        if not isinstance(self.results, (list, tuple)) or any(
            not isinstance(item, expected) for item in self.results
        ):
            raise ValueError(f"query/result mismatch for {self.query}")
        object.__setattr__(self, "results", tuple(self.results))
        if not isinstance(self.coverage, SystemCoverage):
            raise ValueError("coverage must be SystemCoverage")
        if self.relationships is not None:
            if not isinstance(self.relationships, (list, tuple)) or any(
                not isinstance(item, RelationshipDetail) for item in self.relationships
            ):
                raise ValueError("relationships must be RelationshipDetail values")
            object.__setattr__(self, "relationships", tuple(self.relationships))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "query": self.query,
            "system_name": self.system_name,
            "results": [item.to_dict() for item in self.results],
            "coverage": self.coverage.to_dict(),
        }
        if self.relationships is not None:
            result["relationships"] = [item.to_dict() for item in self.relationships]
        return result


@dataclass(frozen=True, slots=True)
class SystemQueryResult(Generic[RecordT]):
    """The report plus invocation facts, retaining the report object."""

    report: SystemReport[RecordT]
    invocation: QueryInvocation

    def __post_init__(self) -> None:
        if not isinstance(self.report, SystemReport):
            raise ValueError("report must be SystemReport")
        if not isinstance(self.invocation, QueryInvocation):
            raise ValueError("invocation must be QueryInvocation")

    def to_dict(self) -> dict[str, object]:
        coverage = self.report.coverage.to_dict()
        coverage["source_diagnostics"] = self.invocation.to_dict()["source_diagnostics"]
        result: dict[str, object] = {
            "query": self.report.query,
            "refreshed": self.invocation.refreshed,
            "results": [item.to_dict() for item in self.report.results],
            "stale": list(self.invocation.stale),
            "coverage": coverage,
        }
        if self.report.relationships is not None:
            result["relationships"] = [item.to_dict() for item in self.report.relationships]
        return result


def compose_system_query(
    report: SystemReport[RecordT], invocation: QueryInvocation
) -> SystemQueryResult[RecordT]:
    """Compose typed snapshot and invocation facts without reparsing output."""
    if not isinstance(report, SystemReport) or not isinstance(invocation, QueryInvocation):
        raise ValueError("compose_system_query requires a SystemReport and QueryInvocation")
    if report.coverage.source_diagnostics.get("status") != "unavailable":
        raise ValueError("snapshot report source diagnostics must be unavailable")
    return SystemQueryResult(report=report, invocation=invocation)


@dataclass(frozen=True, slots=True, init=False)
class ReportingSnapshot:
    """Prepared immutable graph index and strict-loaded systems."""

    document: GraphDocument
    systems: tuple[System, ...]
    index: GraphIndex

    def __init__(self, document: GraphDocument, systems: Sequence[System]) -> None:
        if not isinstance(document, GraphDocument):
            raise ValueError("document must be GraphDocument")
        if not isinstance(systems, (list, tuple)) or any(
            not isinstance(system, System) for system in systems
        ):
            raise ValueError("systems must be a sequence of System values")
        copied_systems = tuple(systems)
        object.__setattr__(self, "document", document)
        object.__setattr__(self, "systems", copied_systems)
        object.__setattr__(self, "index", GraphIndex.build(document))

    @classmethod
    def prepare(cls, document: GraphDocument, systems: Sequence[System]) -> ReportingSnapshot:
        return cls(document, systems)

    @overload
    def report(
        self, query: Literal["surface"], system_name: str, details: bool = False
    ) -> SystemReport[SurfaceRecord]: ...

    @overload
    def report(
        self, query: Literal["consumers"], system_name: str, details: bool = False
    ) -> SystemReport[ConsumersRecord]: ...

    @overload
    def report(
        self, query: Literal["system-deps"], system_name: str, details: bool = False
    ) -> SystemReport[SystemDepsRecord]: ...

    def report(self, query: str, system_name: str, details: bool = False) -> SystemReport[Any]:
        if query not in _QUERY_NAMES:
            raise ValueError(f"unknown system query: {query}")
        target = resolve_system(self.systems, system_name)
        if query == "surface":
            records: tuple[Any, ...] = surface(self.systems, self.index, target)
        elif query == "consumers":
            records = consumers(self.systems, self.index, target)
        else:
            records = system_deps(self.systems, self.index, target)
        coverage = self._coverage(target)
        relationship_details = self._relationships(query, target) if details else None
        return SystemReport(
            query=query,
            system_name=target.name,
            results=records,
            coverage=coverage,
            relationships=relationship_details,
        )

    def _coverage(self, target: System) -> SystemCoverage:
        selection = recorded_selection_view(self.document)
        declared = set(target.files)
        represented = {
            membership.file
            for node in self.document.nodes
            if (membership := classify_endpoint(self.systems, node)).kind is EndpointKind.SYSTEM
            and membership.system is not None
            and membership.system.name == target.name
            and membership.file is not None
        }
        unresolved = sum(
            1
            for node in self.index.unresolved_nodes
            if node.location is not None and node.location.path in declared
        )
        return SystemCoverage(
            selection=(
                {"status": "recorded", "targets": selection.targets}
                if selection.recorded
                else {"status": "unavailable"}
            ),
            graph_files={
                "scope": "final_graph_file_nodes",
                "count": sum(node.node_class.value == "file" for node in self.document.nodes),
            },
            declared_files={
                "scope": "selected_system_declared_files",
                "total": len(target.files),
                "represented": len(declared & represented),
                "absent": len(declared - represented),
            },
            recorded_unresolved_references={
                "scope": "selected_system_declared_files",
                "count": unresolved,
            },
            source_diagnostics={"status": "unavailable"},
        )

    def _relationships(self, query: str, target: System) -> tuple[RelationshipDetail, ...]:
        selected: list[tuple[Relationship, Node, Node]] = []
        kinds = _SURFACE_KINDS if query == "surface" else _BOUNDARY_KINDS
        for kind in kinds:
            for relationship in self.index.relationships(kind):
                source = self.index.nodes.get(relationship.source)
                destination = self.index.nodes.get(relationship.target)
                if source is None or destination is None:
                    continue
                source_in = _in_scope(self.systems, target, source)
                destination_in = _in_scope(self.systems, target, destination)
                if query == "surface":
                    qualifies = destination_in and not source_in
                elif query == "consumers":
                    qualifies = destination_in and not source_in
                    if qualifies and classify_endpoint(self.systems, source).file is None:
                        qualifies = False
                else:
                    qualifies = source_in and not destination_in
                if qualifies:
                    selected.append((relationship, source, destination))
        selected.sort(key=lambda item: item[0].tuple_key)
        return tuple(
            _relationship_detail(self.document, relationship, source, destination)
            for relationship, source, destination in selected
        )


def _tag(value: object) -> dict[str, object]:
    return {"status": "recorded", "value": value}


def _optional_mapping(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {"status": "unavailable"}
    return {"status": "recorded", "value": _thaw_json(_freeze_json(value))}


def _location_dict(location: Location, encoding: str, *, status: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": location.path,
        "coordinate_encoding": encoding,
        "range": {
            "start": {
                "line": location.range.start.line + 1,
                "column": location.range.start.character + 1,
            },
            "end": {
                "line": location.range.end.line + 1,
                "column": location.range.end.character + 1,
            },
            "end_exclusive": True,
        },
    }
    return {"status": "recorded", **payload} if status else payload


def _endpoint_detail(document: GraphDocument, node: Node) -> EndpointDetail:
    identity: dict[str, object] = {
        "basis": node.identity.basis.value,
        "namespace": node.identity.namespace,
        "upstream_identifier": (
            _tag(node.identity.upstream_identifier)
            if node.identity.upstream_identifier is not None
            else {"status": "unavailable"}
        ),
        "resource_key": (
            _tag(node.identity.resource_key)
            if node.identity.resource_key is not None
            else {"status": "unavailable"}
        ),
        "reference_text": (
            _tag(node.reference_text)
            if node.reference_text is not None
            else {"status": "unavailable"}
        ),
    }
    location = (
        _location_dict(node.location, document.coordinate_encoding.value, status=True)
        if node.location is not None
        else {"status": "unavailable"}
    )
    path = (
        _tag(node.location.path)
        if node.location is not None
        else (_tag(node.path) if node.path is not None else {"status": "unavailable"})
    )
    return EndpointDetail(
        id=node.id,
        label=node.label,
        node_class=node.node_class.value,
        semantic_identity=identity,
        path=path,
        location=location,
    )


def _evidence_detail(document: GraphDocument, evidence: Any) -> EvidenceDetail:
    locations = tuple(sorted(evidence.locations, key=lambda location: location.sort_key))
    producer = (
        {
            "status": "recorded",
            "name": evidence.producer.name,
            "version": (
                _tag(evidence.producer.version)
                if evidence.producer.version is not None
                else {"status": "unavailable"}
            ),
        }
        if evidence.producer is not None
        else {"status": "unavailable"}
    )
    rule = (
        {
            "status": "recorded",
            "id": evidence.rule.id,
            "version": (
                _tag(evidence.rule.version)
                if evidence.rule.version is not None
                else {"status": "unavailable"}
            ),
        }
        if evidence.rule is not None
        else {"status": "unavailable"}
    )
    return EvidenceDetail(
        provenance=evidence.provenance.value,
        producer=producer,
        rule=rule,
        evidence_extensions=_optional_mapping(evidence.extensions),
        sites=tuple(
            _location_dict(location, document.coordinate_encoding.value, status=False)
            for location in locations
        ),
    )


def _evidence_sort_key(evidence: Any) -> bytes:
    raw = evidence.to_dict()
    if "locations" in raw:
        raw["locations"] = sorted(
            raw["locations"],
            key=lambda item: (
                item["path"],
                item["range"]["start"]["line"],
                item["range"]["start"]["character"],
                item["range"]["end"]["line"],
                item["range"]["end"]["character"],
            ),
        )
    return _jcs_serialize(raw)


def _relationship_detail(
    document: GraphDocument, relationship: Relationship, source: Node, target: Node
) -> RelationshipDetail:
    evidence = tuple(
        _evidence_detail(document, item)
        for item in sorted(relationship.evidence, key=_evidence_sort_key)
    )
    return RelationshipDetail(
        source=_endpoint_detail(document, source),
        target=_endpoint_detail(document, target),
        kind=relationship.kind,
        relationship_extensions=_optional_mapping(relationship.extensions),
        evidence=evidence,
    )
