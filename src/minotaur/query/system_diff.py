"""Typed, complete comparison of two prepared system snapshots.

The comparison composes the existing correspondence and reporting owners.  It
keeps system declarations, report rows, and semantic boundary relationships in
separate immutable categories so later views can choose their own projection
without rebuilding structural rules or losing row evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from minotaur.graph_model.node import Node
from minotaur.query.correspondence import (
    CorrespondenceAdmissionError,
    CorrespondenceAmbiguityError,
    CorrespondenceEligibilityError,
    CorrespondenceError,
    CorrespondenceIndex,
    RelationshipKey,
    prepare_correspondence,
)
from minotaur.query.system import (
    EndpointDetail,
    RelationshipDetail,
    ReportingSnapshot,
)
from minotaur.system import EndpointKind, classify_endpoint

_REPORT_QUERIES = ("surface", "consumers", "system-deps")
_BOUNDARY_KINDS = frozenset({"calls", "references", "imports"})


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw(item) for item in sorted(value, key=repr)]
    return value


def _sort_token(value: object) -> tuple[object, ...]:
    if value is None:
        return (0, "")
    if isinstance(value, tuple):
        return (1, tuple(_sort_token(item) for item in value))
    if isinstance(value, str):
        return (2, value)
    return (3, repr(value))


def _key_token(key: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(_sort_token(item) for item in key)


def _public_key(key: tuple[object, ...]) -> tuple[str, ...]:
    """Render a correspondence key without exposing mutable/internal values."""
    return tuple(repr(item) for item in key)


def _category(snapshot: ReportingSnapshot, node: Node) -> str:
    membership = classify_endpoint(snapshot.systems, node)
    if membership.kind is EndpointKind.SYSTEM and membership.system is not None:
        return f"system: {membership.system.name}"
    if membership.kind is EndpointKind.NO_SYSTEM:
        return "no_system"
    return "external"


def _named(category: str) -> str | None:
    return category.removeprefix("system: ") if category.startswith("system: ") else None


def _involved(*entries: Mapping[str, object] | None) -> tuple[str, ...]:
    names: set[str] = set()
    for entry in entries:
        if entry is None:
            continue
        direct = entry.get("involved_systems")
        if isinstance(direct, (tuple, list, frozenset, set)):
            names.update(item for item in direct if isinstance(item, str))
        categories = entry.get("categories")
        if isinstance(categories, (tuple, list)):
            for category in categories:
                if isinstance(category, str) and (name := _named(category)) is not None:
                    names.add(name)
        for key in ("system", "subject"):
            value = entry.get(key)
            if isinstance(value, str):
                names.add(value)
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class SystemChange:
    """One complete typed change with exact old/new values."""

    domain: str
    kind: str
    key: tuple[str, ...]
    old: object = None
    new: object = None
    involved_systems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", tuple(self.key))
        object.__setattr__(self, "old", _freeze(self.old))
        object.__setattr__(self, "new", _freeze(self.new))
        involved = self.involved_systems or _involved(
            self.old if isinstance(self.old, Mapping) else None,
            self.new if isinstance(self.new, Mapping) else None,
        )
        object.__setattr__(self, "involved_systems", tuple(sorted(set(involved))))

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "kind": self.kind,
            "key": list(self.key),
            "old": _thaw(self.old),
            "new": _thaw(self.new),
            "involved_systems": list(self.involved_systems),
        }


@dataclass(frozen=True, slots=True)
class SystemDiffResult:
    """Immutable complete result for one old/new snapshot pair."""

    old_system_names: tuple[str, ...] = ()
    new_system_names: tuple[str, ...] = ()
    added_systems: tuple[str, ...] = ()
    removed_systems: tuple[str, ...] = ()
    membership_changes: tuple[SystemChange, ...] = ()
    surface_changes: tuple[SystemChange, ...] = ()
    consumer_changes: tuple[SystemChange, ...] = ()
    dependency_changes: tuple[SystemChange, ...] = ()
    boundary_changes: tuple[SystemChange, ...] = ()
    old_coverage: Mapping[str, object] = field(default_factory=dict)
    new_coverage: Mapping[str, object] = field(default_factory=dict)
    old_selection: Mapping[str, object] = field(default_factory=dict)
    new_selection: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "old_system_names",
            "new_system_names",
            "added_systems",
            "removed_systems",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in (
            "membership_changes",
            "surface_changes",
            "consumer_changes",
            "dependency_changes",
            "boundary_changes",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("old_coverage", "new_coverage", "old_selection", "new_selection"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    @property
    def old_names(self) -> tuple[str, ...]:
        return self.old_system_names

    @property
    def new_names(self) -> tuple[str, ...]:
        return self.new_system_names

    @property
    def old_systems(self) -> tuple[str, ...]:
        return self.old_system_names

    @property
    def new_systems(self) -> tuple[str, ...]:
        return self.new_system_names

    @property
    def changed(self) -> bool:
        return bool(
            self.added_systems
            or self.removed_systems
            or self.membership_changes
            or self.surface_changes
            or self.consumer_changes
            or self.dependency_changes
            or self.boundary_changes
        )

    @property
    def has_changes(self) -> bool:
        return self.changed

    @property
    def exit_code(self) -> int:
        return int(self.changed)

    @property
    def differences(self) -> tuple[SystemChange, ...]:
        result = [
            SystemChange("systems", "added", (name,), new={"name": name}, involved_systems=(name,))
            for name in self.added_systems
        ]
        result.extend(
            SystemChange(
                "systems", "removed", (name,), old={"name": name}, involved_systems=(name,)
            )
            for name in self.removed_systems
        )
        for changes in (
            self.membership_changes,
            self.surface_changes,
            self.consumer_changes,
            self.dependency_changes,
            self.boundary_changes,
        ):
            result.extend(changes)
        return tuple(result)

    def to_dict(self) -> dict[str, object]:
        return {
            "query": "system-diff",
            "changed": self.changed,
            "exit_code": self.exit_code,
            "old_system_names": list(self.old_system_names),
            "new_system_names": list(self.new_system_names),
            "added_systems": list(self.added_systems),
            "removed_systems": list(self.removed_systems),
            "membership_changes": [item.to_dict() for item in self.membership_changes],
            "surface_changes": [item.to_dict() for item in self.surface_changes],
            "consumer_changes": [item.to_dict() for item in self.consumer_changes],
            "dependency_changes": [item.to_dict() for item in self.dependency_changes],
            "boundary_changes": [item.to_dict() for item in self.boundary_changes],
            "coverage": {"old": _thaw(self.old_coverage), "new": _thaw(self.new_coverage)},
            "selection": {"old": _thaw(self.old_selection), "new": _thaw(self.new_selection)},
        }


SystemStructureDiff = SystemDiffResult
SystemStructureChange = SystemChange


def _context(snapshot: ReportingSnapshot) -> tuple[Mapping[str, object], Mapping[str, object]]:
    payload = snapshot.all_systems_report(details=True).to_dict()
    coverage = payload.get("coverage", {})
    if not isinstance(coverage, Mapping):
        raise TypeError("all-systems coverage must be a mapping")
    selection = coverage.get("selection", {})
    return coverage, selection if isinstance(selection, Mapping) else {}


def _membership_changes(old: ReportingSnapshot, new: ReportingSnapshot) -> tuple[SystemChange, ...]:
    before = {path: system.name for system in old.systems for path in system.files}
    after = {path: system.name for system in new.systems for path in system.files}
    result: list[SystemChange] = []
    for path in sorted(set(before) | set(after)):
        left, right = before.get(path), after.get(path)
        if left == right:
            continue
        old_payload = {"file": path, "system": left}
        new_payload = {"file": path, "system": right}
        result.append(
            SystemChange(
                "membership", "changed", (path,), old_payload, new_payload, _names(left, right)
            )
        )
    return tuple(result)


def _names(*categories: str | None) -> tuple[str, ...]:
    return tuple(
        sorted({name for category in categories if category and (name := _named(category))})
    )


def _node_for_detail(snapshot: ReportingSnapshot, detail: EndpointDetail) -> Node | None:
    return snapshot.index.nodes.get(detail.id)


def _endpoint_structure(
    snapshot: ReportingSnapshot, detail: EndpointDetail
) -> Mapping[str, object]:
    node = _node_for_detail(snapshot, detail)
    payload = detail.to_dict()
    return {
        "label": payload["label"],
        "node_class": payload["node_class"],
        "path": payload["path"],
        "semantic_identity": payload["semantic_identity"],
        "symbol_kind": node.symbol_kind
        if node is not None and node.symbol_kind is not None
        else None,
    }


def _structural_relationship(
    snapshot: ReportingSnapshot, relationship: RelationshipDetail
) -> object:
    """Return canonical endpoint fields that are structural for row comparison."""
    return {
        "source": _freeze(_endpoint_structure(snapshot, relationship.source)),
        "target": _freeze(_endpoint_structure(snapshot, relationship.target)),
        "kind": relationship.kind,
    }


def _report_payload(
    snapshot: ReportingSnapshot, query: str, name: str
) -> dict[tuple[str, ...], dict[str, object]]:
    if name not in {system.name for system in snapshot.systems}:
        return {}
    report = cast(Any, snapshot).report(query, name, details=True)
    result: dict[tuple[str, ...], dict[str, object]] = {}
    row_relationships = report.row_relationships or {}
    for record in report.results:
        payload = record.to_dict()
        key: tuple[str, ...]
        if query == "surface":
            key = (name, str(payload["path"]), str(payload["symbol"]))
        elif query == "consumers":
            key = (name, str(payload["file"]))
        else:
            key = (name, str(payload["category"]))
        evidence = tuple(
            row_relationships.get(key[1:] if query != "system-deps" else (key[1],), ())
        )
        evidence_dict = tuple(item.to_dict() for item in evidence)
        involved = {name}
        for relationship in evidence:
            for detail in (relationship.source, relationship.target):
                node = _node_for_detail(snapshot, detail)
                if node is not None:
                    category = _category(snapshot, node)
                    if endpoint_name := _named(category):
                        involved.add(endpoint_name)
        result[key] = {
            "record": payload,
            "relationships": evidence_dict,
            "involved_systems": tuple(sorted(involved)),
            "structural_relationships": tuple(
                _structural_relationship(snapshot, item) for item in evidence
            ),
        }
    return result


def _all_reports(
    snapshot: ReportingSnapshot,
) -> dict[str, dict[tuple[str, ...], dict[str, object]]]:
    result: dict[str, dict[tuple[str, ...], dict[str, object]]] = {}
    names = tuple(system.name for system in snapshot.systems)
    for query in _REPORT_QUERIES:
        result[query] = {
            key: value
            for name in names
            for key, value in _report_payload(snapshot, query, name).items()
        }
    return result


def _row_changes(
    old: ReportingSnapshot, new: ReportingSnapshot
) -> tuple[dict[str, tuple[SystemChange, ...]], set[RelationshipKey]]:
    old_reports, new_reports = _all_reports(old), _all_reports(new)
    changes: dict[str, list[SystemChange]] = {query: [] for query in _REPORT_QUERIES}
    changed_keys: set[RelationshipKey] = set()
    for query in _REPORT_QUERIES:
        left, right = old_reports[query], new_reports[query]
        for key in sorted(set(left) | set(right), key=_key_token):
            old_row, new_row = left.get(key), right.get(key)
            old_record = old_row.get("record") if old_row else None
            new_record = new_row.get("record") if new_row else None
            old_structural = old_row.get("structural_relationships") if old_row else None
            new_structural = new_row.get("structural_relationships") if new_row else None
            if old_record == new_record and (old_structural == new_structural):
                continue
            old_names = old_row.get("involved_systems", ()) if old_row else ()
            new_names = new_row.get("involved_systems", ()) if new_row else ()
            old_name_values = old_names if isinstance(old_names, (tuple, list)) else ()
            new_name_values = new_names if isinstance(new_names, (tuple, list)) else ()
            changes[query].append(
                SystemChange(
                    query,
                    "added" if old_row is None else "removed" if new_row is None else "changed",
                    tuple(str(item) for item in key),
                    old_row,
                    new_row,
                    tuple(
                        sorted(
                            {
                                item
                                for item in (*old_name_values, *new_name_values)
                                if isinstance(item, str)
                            }
                        )
                    ),
                )
            )
            for snapshot, row in ((old, old_row), (new, new_row)):
                if row is None:
                    continue
                relationships = row.get("relationships", ())
                for relation in relationships if isinstance(relationships, (tuple, list)) else ():
                    if not isinstance(relation, Mapping):
                        continue
                    source = relation.get("source")
                    target = relation.get("target")
                    kind = relation.get("kind")
                    if (
                        not isinstance(source, Mapping)
                        or not isinstance(target, Mapping)
                        or not isinstance(kind, str)
                    ):
                        continue
                    source_id, target_id = source.get("id"), target.get("id")
                    if isinstance(source_id, str) and isinstance(target_id, str):
                        index = prepare_correspondence(snapshot.document)
                        for rel_key, occurrences in index.relationship_groups.items():
                            if any(
                                item.relationship.source == source_id
                                and item.relationship.target == target_id
                                and item.relationship.kind == kind
                                for item in occurrences
                            ):
                                changed_keys.add(rel_key)
    return {query: tuple(values) for query, values in changes.items()}, changed_keys


def _is_boundary(snapshot: ReportingSnapshot, source: Node, target: Node) -> bool:
    source_category, target_category = _category(snapshot, source), _category(snapshot, target)
    if not (_named(source_category) or _named(target_category)):
        return False
    return not (source_category == target_category and _named(source_category) is not None)


def _boundary_keys(snapshot: ReportingSnapshot, index: CorrespondenceIndex) -> set[RelationshipKey]:
    keys: set[RelationshipKey] = set()
    for key, occurrences in index.relationship_groups.items():
        if any(
            _is_boundary(snapshot, occurrence.source, occurrence.target)
            for occurrence in occurrences
        ):
            keys.add(key)
    return keys


def _relation_payload(
    snapshot: ReportingSnapshot,
    index: CorrespondenceIndex,
    key: RelationshipKey,
    occurrences: tuple[Any, ...],
) -> Mapping[str, object]:
    grouped: list[dict[str, object]] = []
    projections: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    categories: tuple[str, str] | None = None
    for occurrence in occurrences:
        detail = _detail_for_occurrence(snapshot, occurrence)
        source_category = _category(snapshot, occurrence.source)
        target_category = _category(snapshot, occurrence.target)
        if categories is None:
            categories = (source_category, target_category)
        projection = (
            _freeze(_endpoint_structure(snapshot, detail.source)),
            _freeze(_endpoint_structure(snapshot, detail.target)),
        )
        if projection not in projections:
            projections.append(cast(tuple[Mapping[str, object], Mapping[str, object]], projection))
        grouped.append(detail.to_dict())
    grouped.sort(key=repr)
    assert categories is not None
    involvement = _names(*categories)
    first_projection = sorted(projections, key=repr)[0]
    return {
        "categories": categories,
        "source_category": categories[0],
        "target_category": categories[1],
        "kind": key[2],
        "relationships": tuple(grouped),
        "source_endpoint": _thaw(first_projection[0]),
        "target_endpoint": _thaw(first_projection[1]),
        "projections": tuple(sorted(projections, key=repr)),
        "source_membership": categories[0],
        "target_membership": categories[1],
        "involved_systems": involvement,
    }


def _detail_for_occurrence(snapshot: ReportingSnapshot, occurrence: Any) -> RelationshipDetail:
    for detail in snapshot.relationship_details():
        if (
            detail.source.id == occurrence.source.id
            and detail.target.id == occurrence.target.id
            and detail.kind == occurrence.relationship.kind
        ):
            return detail
    raise AssertionError("correspondence occurrence has no reporting detail")


def _boundary_map(
    snapshot: ReportingSnapshot,
    index: CorrespondenceIndex,
    selected: set[RelationshipKey],
) -> dict[RelationshipKey, Mapping[str, object]]:
    result: dict[RelationshipKey, Mapping[str, object]] = {}
    for key in sorted(selected, key=_key_token):
        occurrences = index.relationship_groups.get(key)
        if not occurrences:
            continue
        result[key] = _relation_payload(snapshot, index, key, occurrences)
    return result


def _boundary_changes(
    old: ReportingSnapshot,
    new: ReportingSnapshot,
    old_index: CorrespondenceIndex,
    new_index: CorrespondenceIndex,
    selected: set[RelationshipKey],
) -> tuple[SystemChange, ...]:
    old_map = _boundary_map(old, old_index, selected)
    new_map = _boundary_map(new, new_index, selected)
    result: list[SystemChange] = []
    for key in sorted(set(old_map) | set(new_map), key=_key_token):
        left, right = old_map.get(key), new_map.get(key)
        if left is None or right is None:
            entry = right or left
            assert entry is not None
            categories = entry.get("categories")
            if (
                isinstance(categories, (tuple, list))
                and len(categories) == 2
                and categories[0] == categories[1]
                and _named(str(categories[0])) is not None
            ):
                # An internal relationship that has no counterpart is not a
                # boundary fact.  It is present here only because its exact
                # key was needed to match a boundary on the other side.
                continue
            result.append(
                SystemChange(
                    "boundary",
                    "added" if left is None else "removed",
                    _public_key(key),
                    left,
                    right,
                    _involved(left, right),
                )
            )
            continue
        if left.get("categories") != right.get("categories"):
            result.append(
                SystemChange(
                    "boundary", "membership", _public_key(key), left, right, _involved(left, right)
                )
            )
        if left.get("projections") != right.get("projections"):
            result.append(
                SystemChange(
                    "boundary", "endpoint", _public_key(key), left, right, _involved(left, right)
                )
            )
    return tuple(result)


def compare_systems(
    old_snapshot: ReportingSnapshot, new_snapshot: ReportingSnapshot
) -> SystemDiffResult:
    """Compare two complete reporting snapshots after canonical admission."""
    if not isinstance(old_snapshot, ReportingSnapshot) or not isinstance(
        new_snapshot, ReportingSnapshot
    ):
        raise TypeError("compare_systems requires two ReportingSnapshot values")

    old_index = prepare_correspondence(old_snapshot.document, side="old")
    new_index = prepare_correspondence(new_snapshot.document, side="new")
    old_names = tuple(sorted(system.name for system in old_snapshot.systems))
    new_names = tuple(sorted(system.name for system in new_snapshot.systems))
    row_changes, changed_row_keys = _row_changes(old_snapshot, new_snapshot)
    selected = _boundary_keys(old_snapshot, old_index) | _boundary_keys(new_snapshot, new_index)
    selected |= changed_row_keys
    old_index.validate_required_keys(selected, side="old")
    new_index.validate_required_keys(selected, side="new")
    old_coverage, old_selection = _context(old_snapshot)
    new_coverage, new_selection = _context(new_snapshot)
    membership = _membership_changes(old_snapshot, new_snapshot)
    boundary = _boundary_changes(old_snapshot, new_snapshot, old_index, new_index, selected)
    return SystemDiffResult(
        old_system_names=old_names,
        new_system_names=new_names,
        added_systems=tuple(sorted(set(new_names) - set(old_names))),
        removed_systems=tuple(sorted(set(old_names) - set(new_names))),
        membership_changes=membership,
        surface_changes=row_changes["surface"],
        consumer_changes=row_changes["consumers"],
        dependency_changes=row_changes["system-deps"],
        boundary_changes=boundary,
        old_coverage=old_coverage,
        new_coverage=new_coverage,
        old_selection=old_selection,
        new_selection=new_selection,
    )


def compare_system_snapshots(
    old_snapshot: ReportingSnapshot, new_snapshot: ReportingSnapshot
) -> SystemDiffResult:
    return compare_systems(old_snapshot, new_snapshot)


def render_system_diff_text(result: SystemDiffResult, *, details: bool = False) -> str:
    lines = [f"+ system {name}\n" for name in result.added_systems]
    lines.extend(f"- system {name}\n" for name in result.removed_systems)
    for label, changes in (
        ("membership", result.membership_changes),
        ("surface", result.surface_changes),
        ("consumer", result.consumer_changes),
        ("dependency", result.dependency_changes),
        ("boundary", result.boundary_changes),
    ):
        lines.extend(f"~ {label} {change.key[0]}\n" for change in changes)
    if not lines:
        lines.append("no system-structure differences\n")
    if details:
        lines.extend(
            (
                f"coverage old {result.old_coverage!r}\n",
                f"coverage new {result.new_coverage!r}\n",
                f"selection old {result.old_selection!r}\n",
                f"selection new {result.new_selection!r}\n",
                f"changes {[item.to_dict() for item in result.differences]!r}\n",
            )
        )
    return "".join(lines)


def render_system_diff_json(result: SystemDiffResult) -> str:
    import json

    return json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"))


def render_system_diff(
    result: SystemDiffResult, *, details: bool = False, json_output: bool = False
) -> str:
    return (
        render_system_diff_json(result)
        if json_output
        else render_system_diff_text(result, details=details)
    )


__all__ = [
    "SystemChange",
    "SystemDiffResult",
    "SystemStructureChange",
    "SystemStructureDiff",
    "CorrespondenceAdmissionError",
    "CorrespondenceAmbiguityError",
    "CorrespondenceEligibilityError",
    "CorrespondenceError",
    "compare_systems",
    "compare_system_snapshots",
    "render_system_diff",
    "render_system_diff_json",
    "render_system_diff_text",
]
