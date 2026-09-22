"""Deterministic multiset comparison of source call-expression observations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from minotaur.graph_model.location import Location
from minotaur.language_interpreter.call_expressions import CallExpressionObservation
from minotaur.query.correspondence import CorrespondenceIndex
from minotaur.query.graph_comparison import relationship_display_id
from minotaur.system import EndpointKind, System, classify_endpoint

if TYPE_CHECKING:
    from minotaur.query.system import ReportingSnapshot


def _location_key(location: Location) -> tuple[object, ...]:
    span = location.range
    return (
        location.path,
        span.start.line,
        span.start.character,
        span.end.line,
        span.end.character,
    )


def _location_dict(location: Location) -> dict[str, object]:
    return location.to_dict()


def _observation_dict(observation: CallExpressionObservation) -> dict[str, object]:
    return {
        "language": observation.language,
        "callee": _location_dict(observation.callee_location),
        "expression": _location_dict(observation.expression_location),
        "fingerprint": observation.fingerprint,
    }


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _fallback_relationship_id(observation: CallExpressionObservation) -> str:
    payload = json.dumps(
        {
            "language": observation.language,
            "callee": _location_key(observation.callee_location),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"call\0{payload}".encode()).hexdigest()
    return f"relationship:call-site:{digest}"


class CallCorrespondenceAmbiguityError(ValueError):
    """One observation maps to multiple graph call relationships."""

    def __init__(self, observation: CallExpressionObservation, relationship_ids: Iterable[str]):
        self.observation = observation
        self.relationship_ids = tuple(sorted(set(relationship_ids)))
        super().__init__(
            "ambiguous call observation at "
            f"{observation.callee_location.path!r}: "
            f"{', '.join(self.relationship_ids)}"
        )


@dataclass(frozen=True, slots=True)
class CallChange:
    """One call-site residual classification."""

    relationship_id: str
    status: str
    reasons: tuple[str, ...] = ()
    involved_systems: tuple[str, ...] = ()
    before: object = None
    after: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(sorted(set(self.reasons))))
        object.__setattr__(self, "involved_systems", tuple(sorted(set(self.involved_systems))))
        object.__setattr__(self, "before", _freeze(self.before))
        object.__setattr__(self, "after", _freeze(self.after))

    @property
    def id(self) -> str:
        return self.relationship_id

    def to_dict(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "status": self.status,
            "reasons": list(self.reasons),
            "involved_systems": list(self.involved_systems),
            "before": _thaw(self.before),
            "after": _thaw(self.after),
        }


@dataclass(frozen=True, slots=True)
class CallLimitation:
    """A side-local gap that prevents a call residual from proving equality."""

    side: str
    relationship_id: str | None
    code: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "relationship_id": self.relationship_id,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CallComparison:
    changes: tuple[CallChange, ...] = ()
    limitations: tuple[CallLimitation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "limitations", tuple(self.limitations))

    @property
    def changed(self) -> bool:
        return any(item.status in {"added", "removed", "changed"} for item in self.changes)

    def to_dict(self) -> dict[str, object]:
        return {
            "call_changes": [item.to_dict() for item in self.changes],
            "limitations": [item.to_dict() for item in self.limitations],
        }


def _relation_ids(
    observation: CallExpressionObservation,
    index: CorrespondenceIndex | None,
) -> tuple[str, ...]:
    if index is None:
        return (_fallback_relationship_id(observation),)
    location = _location_key(observation.callee_location)
    matches: set[str] = set()
    for key, occurrences in index.relationships_by_key.items():
        if key[2] != "calls":
            continue
        for occurrence in occurrences:
            for evidence in occurrence.relationship.evidence:
                if any(_location_key(site) == location for site in evidence.locations):
                    matches.add(relationship_display_id(key))
    if len(matches) > 1:
        raise CallCorrespondenceAmbiguityError(observation, matches)
    return tuple(matches) or (_fallback_relationship_id(observation),)


def _normalize(
    observations: Sequence[CallExpressionObservation],
    index: CorrespondenceIndex | None,
) -> dict[tuple[str, tuple[object, ...]], list[CallExpressionObservation]]:
    grouped: dict[tuple[str, tuple[object, ...]], list[CallExpressionObservation]] = defaultdict(
        list
    )
    for observation in observations:
        if not isinstance(observation, CallExpressionObservation):
            raise TypeError("call observations must be CallExpressionObservation values")
        for relation_id in _relation_ids(observation, index):
            grouped[(relation_id, _location_key(observation.callee_location))].append(observation)
    return grouped


def _relationship_memberships(
    index: CorrespondenceIndex | None,
    *,
    snapshot: ReportingSnapshot | None,
    systems: Sequence[System] | None,
) -> dict[str, frozenset[str]]:
    """Associate graph relationship IDs with named endpoint systems.

    Call observations identify a relationship through evidence locations, but
    they do not carry endpoint nodes.  Membership therefore comes from the
    matched whole-graph relationship occurrences, using the same exact-file
    endpoint classifier as graph comparison.  Both sides are collected so a
    relationship added or removed across revisions retains all endpoint names.
    """
    if index is None:
        return {}
    declared_systems = snapshot.systems if snapshot is not None else (systems or ())
    result: dict[str, frozenset[str]] = {}
    for key, occurrences in index.relationship_groups.items():
        names: set[str] = set()
        for occurrence in occurrences:
            for endpoint in (occurrence.source, occurrence.target):
                membership = classify_endpoint(declared_systems, endpoint)
                if membership.kind is EndpointKind.SYSTEM and membership.system is not None:
                    names.add(membership.system.name)
        result[relationship_display_id(key)] = frozenset(names)
    return result


def compare_call_observations(
    old: Sequence[CallExpressionObservation],
    new: Sequence[CallExpressionObservation],
    *,
    old_index: CorrespondenceIndex | None = None,
    new_index: CorrespondenceIndex | None = None,
    old_snapshot: ReportingSnapshot | None = None,
    new_snapshot: ReportingSnapshot | None = None,
    old_systems: Sequence[System] | None = None,
    new_systems: Sequence[System] | None = None,
) -> CallComparison:
    """Compare observations as multisets; never pair residuals by position."""
    old_groups, new_groups = _normalize(old, old_index), _normalize(new, new_index)
    old_memberships = _relationship_memberships(
        old_index, snapshot=old_snapshot, systems=old_systems
    )
    new_memberships = _relationship_memberships(
        new_index, snapshot=new_snapshot, systems=new_systems
    )
    changes: list[CallChange] = []
    limitations: list[CallLimitation] = []
    for key in sorted(set(old_groups) | set(new_groups), key=repr):
        relation_id, _location = key
        before_values = old_groups.get(key, [])
        after_values = new_groups.get(key, [])
        before = (
            tuple(sorted((_observation_dict(item) for item in before_values), key=repr)) or None
        )
        after = tuple(sorted((_observation_dict(item) for item in after_values), key=repr)) or None
        for side, values in (("old", before_values), ("new", after_values)):
            if any(item.fingerprint is None for item in values):
                limitations.append(
                    CallLimitation(
                        side,
                        relation_id,
                        "call-expression-unavailable",
                        "structural call expression was unavailable for one or more observations",
                    )
                )
        status: str
        reasons: tuple[str, ...]
        if not before_values:
            status, reasons = "added", ("added",)
        elif not after_values:
            status, reasons = "removed", ("removed",)
        else:
            before_counts = Counter(item.fingerprint for item in before_values)
            after_counts = Counter(item.fingerprint for item in after_values)
            if any(item.fingerprint is None for item in (*before_values, *after_values)):
                status, reasons = "unavailable", ("unavailable",)
            elif before_counts == after_counts:
                status, reasons = "unchanged", ()
            else:
                reason_values = ["expression_changed"]
                if len(before_values) != len(after_values):
                    reason_values.append("multiplicity_changed")
                status, reasons = "changed", tuple(reason_values)
        changes.append(
            CallChange(
                relationship_id=relation_id,
                status=status,
                reasons=reasons,
                involved_systems=tuple(
                    sorted(
                        old_memberships.get(relation_id, frozenset())
                        | new_memberships.get(relation_id, frozenset())
                    )
                ),
                before=before,
                after=after,
            )
        )
    limitations.sort(key=lambda item: (item.side, item.relationship_id or "", item.code))
    return CallComparison(tuple(changes), tuple(limitations))


compare_calls = compare_call_observations
compare_call_diff = compare_call_observations
compare_call_residuals = compare_call_observations
CallResidual = CallChange
ComparisonLimitation = CallLimitation

__all__ = [
    "CallChange",
    "CallComparison",
    "CallCorrespondenceAmbiguityError",
    "CallLimitation",
    "CallResidual",
    "ComparisonLimitation",
    "compare_call_diff",
    "compare_call_observations",
    "compare_call_residuals",
    "compare_calls",
]
