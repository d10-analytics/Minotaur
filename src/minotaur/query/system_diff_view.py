"""Pure views over one complete :class:`SystemDiffResult`.

The comparator owns admission, structural categories, status, and the complete
context.  This module only projects that already-computed value: selecting a
system never reacquires a snapshot or recomputes a relationship.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from minotaur.query.render import dump_json
from minotaur.query.system_diff import SystemChange, SystemDiffResult
from minotaur.system import UnknownSystem

_CHANGE_GROUPS: tuple[tuple[str, str], ...] = (
    ("membership_changes", "membership"),
    ("surface_changes", "surface"),
    ("consumer_changes", "consumer"),
    ("dependency_changes", "dependency"),
    ("boundary_changes", "boundary"),
)

_COMPLETE_GROUPS = ("nodes", "relationships", "call_changes")


def filter_system_diff(
    complete_result: SystemDiffResult, system_name: str | None = None
) -> SystemDiffResult:
    """Select one system from a complete result, replacing the prior view.

    ``complete_result`` remains the caller-retained complete value.  Passing
    ``None`` copies every category, while a name retains every change whose stored
    ``involved_systems`` contains it.  A later selection must therefore call
    this function with that same complete value; cumulative narrowing is not
    the default because it would hide a change involving the newly selected
    system.  An explicit intersection need is the point at which this policy
    should be revisited.
    """
    if not isinstance(complete_result, SystemDiffResult):
        raise TypeError("filter_system_diff requires a SystemDiffResult")

    names = tuple(
        sorted(set(complete_result.old_system_names) | set(complete_result.new_system_names))
    )
    if system_name is not None and system_name not in names:
        raise UnknownSystem(system_name, difflib.get_close_matches(system_name, names, n=5))

    def retain(change: SystemChange) -> bool:
        return system_name is None or system_name in change.involved_systems

    kwargs: dict[str, object] = {
        "old_system_names": complete_result.old_system_names,
        "new_system_names": complete_result.new_system_names,
        "added_systems": tuple(
            item
            for item in complete_result.added_systems
            if system_name is None or item == system_name
        ),
        "removed_systems": tuple(
            item
            for item in complete_result.removed_systems
            if system_name is None or item == system_name
        ),
        "old_coverage": complete_result.old_coverage,
        "new_coverage": complete_result.new_coverage,
        "old_selection": complete_result.old_selection,
        "new_selection": complete_result.new_selection,
        "old_revision": complete_result.old_revision,
        "new_revision": complete_result.new_revision,
    }
    for field_name, _label in _CHANGE_GROUPS:
        kwargs[field_name] = tuple(
            change for change in getattr(complete_result, field_name) if retain(change)
        )
    for field_name in _COMPLETE_GROUPS:
        kwargs[field_name] = tuple(
            change for change in getattr(complete_result, field_name) if retain(change)
        )
    kwargs["limitations"] = tuple(complete_result.limitations)
    return SystemDiffResult(**cast(Any, kwargs))


def render_context_text(result: SystemDiffResult) -> str:
    """Render the four stored context mappings in their fixed order."""
    lines = []
    for label, value in (
        ("old coverage", result.old_coverage),
        ("new coverage", result.new_coverage),
        ("old selection", result.old_selection),
        ("new selection", result.new_selection),
    ):
        lines.append(f"{label}: {dump_json(_thaw(value)).rstrip(chr(10))}\n")
    return "".join(lines)


def render_text(
    result: SystemDiffResult,
    *,
    details: bool = False,
    complete: SystemDiffResult | None = None,
) -> str:
    """Render every difference of ``result`` followed by stored context.

    Rows, limitation lines, the empty-state sentence and the caller's exit
    status all come from ``result``.  ``complete`` (default ``result``) only
    supplies a private index of readable names, built once per call, so a
    selected view names the same nodes as the complete comparison.
    """
    differences = result.differences
    names = _NameIndex.build(result if complete is None else complete)
    reasons = _ReasonIndex.build(result)
    lines = [
        _change_lines("systems", change, details=details)
        for change in differences
        if change.domain == "systems"
    ]
    lines.extend(
        _change_lines(label, change, details=details)
        for field_name, label in _CHANGE_GROUPS
        for change in _ordered(getattr(result, field_name))
    )
    for domain in ("graph-node", "graph-relationship", "call"):
        rows = sorted(
            (
                (_change_status(change), text, change.key, change)
                for change in differences
                if change.domain == domain
                for text in (_internal_row(domain, change, names, reasons),)
            ),
            key=lambda row: row[:3],
        )
        lines.extend(
            text + (_render_details(change) if details else "") for _, text, _, change in rows
        )
    if not lines:
        lines.append("no system differences\n")
    if result.limitations:
        lines.extend(
            f"limitation {item.side}: {item.code}: {item.message}\n" for item in result.limitations
        )
    return "".join(lines) + render_context_text(result)


def _render_details(change: SystemChange) -> str:
    """Append exact stored records, endpoint projections, and evidence.

    Details expose the typed changes with explicit ``unavailable`` sides,
    rather than inferring a missing old or new value from the opaque boundary
    key.
    """
    return "".join(
        f"{label}: {_detail_value(value)}\n"
        for label, value in (
            ("old", _change_before(change)),
            ("new", _change_after(change)),
            ("old evidence", _side_evidence(_change_before(change))),
            ("new evidence", _side_evidence(_change_after(change))),
        )
    )


def render_json(result: SystemDiffResult) -> str:
    """Render the selected typed result through the canonical JSON owner."""
    return dump_json(result.to_dict())


def _ordered(changes: Sequence[SystemChange]) -> tuple[SystemChange, ...]:
    return tuple(
        sorted(
            tuple(change for change in changes if getattr(change, "status", None) != "unchanged"),
            key=lambda change: (
                _change_status(change),
                change.key,
                repr(_change_before(change)),
                repr(_change_after(change)),
            ),
        )
    )


def _change_lines(label: str, change: SystemChange, *, details: bool) -> str:
    output = _change_line(label, change)
    if details:
        output += _render_details(change)
    return output


def _change_line(label: str, change: SystemChange) -> str:
    """Render one typed change, preserving literal owners and report subjects.

    Only category-valued destinations/endpoints carry a removable prefix. A
    literal name may itself begin with that prefix or equal a sentinel label.
    """
    if label == "systems":
        return f"system {change.kind}: {_safe_atom(_key_part(change, 0))}\n"
    kind = _safe_atom(_change_status(change))
    if label == "membership":
        old = _mapping_side(_change_before(change))
        new = _mapping_side(_change_after(change))
        file = (
            _mapping_value(_change_after(change), "file")
            or _mapping_value(_change_before(change), "file")
            or _key_part(change, 0)
        )
        return f"membership {kind}: {_safe_atom(file)} — {_safe_atom(old)} -> {_safe_atom(new)}\n"
    if label == "surface":
        system = _key_part(change, 0)
        path = _key_part(change, 1)
        symbol = _key_part(change, 2)
        return f"surface {kind}: {_safe_atom(system)} {_safe_atom(path)}.{_safe_atom(symbol)}\n"
    if label == "consumer":
        system = _key_part(change, 0)
        file = _key_part(change, 1)
        return f"consumer {kind}: {_safe_atom(system)} <- {_safe_atom(file)}\n"
    if label == "dependency":
        source = _key_part(change, 0)
        category = (
            _record_category(_change_after(change))
            or _record_category(_change_before(change))
            or _key_part(change, 1)
        )
        category = _display_category(category)
        return f"dependency {kind}: {_safe_atom(source)} -> {_safe_atom(category)}\n"
    if label == "boundary":
        payload = _mapping(_change_after(change)) or _mapping(_change_before(change))
        source = _endpoint_display(payload, "source_endpoint", "source_category")
        target = _endpoint_display(payload, "target_endpoint", "target_category")
        relation = _mapping_value(payload, "kind") or "unknown"
        return (
            f"boundary {kind}: {_safe_atom(source)} -> {_safe_atom(target)} "
            f"({_safe_atom(relation)})\n"
        )
    raise AssertionError(f"unsupported change label: {label}")


@dataclass(frozen=True, slots=True)
class _NameIndex:
    """Readable names resolved from one complete result, built once per render."""

    nodes: Mapping[str, tuple[object, object]]
    relationships: Mapping[str, tuple[str, str, str]]

    @classmethod
    def build(cls, complete: SystemDiffResult) -> _NameIndex:
        return cls(
            nodes={item.id: (_first(item.before), _first(item.after)) for item in complete.nodes},
            relationships={
                item.id: (item.source, item.target, item.kind) for item in complete.relationships
            },
        )

    def node_record(self, node_id: str, status: str) -> Mapping[str, object] | None:
        """Return the new side (old side for a removed row), else the other one."""
        before, after = self.nodes.get(node_id, (None, None))
        order = (before, after) if status == "removed" else (after, before)
        for side in order:
            record = _mapping(_mapping_value(side, "node") or side)
            if record:
                return record
        return None

    def label(self, node_id: str, status: str) -> str:
        record = self.node_record(node_id, status)
        label = record.get("label") if record else None
        return str(label) if label is not None else node_id


@dataclass(frozen=True, slots=True)
class _ReasonIndex:
    """Stored reasons of the selected result, one lookup per owning group."""

    nodes: Mapping[str, tuple[str, ...]]
    relationships: Mapping[str, tuple[str, ...]]
    calls: Mapping[str, tuple[str, ...]]

    @classmethod
    def build(cls, result: SystemDiffResult) -> _ReasonIndex:
        return cls(
            nodes={item.id: item.reasons for item in result.nodes},
            relationships={item.id: item.reasons for item in result.relationships},
            calls={item.relationship_id: item.reasons for item in result.call_changes},
        )


def _first(payload: object) -> object:
    """Read the first record of a several-record group."""
    if isinstance(payload, (tuple, list)):
        return payload[0] if payload else None
    return payload


def _internal_row(
    domain: str, change: SystemChange, names: _NameIndex, reasons: _ReasonIndex
) -> str:
    """Render one node, relationship or call difference with readable names."""
    status = _change_status(change)
    identifier = _key_part(change, 0)
    if domain == "graph-node":
        prefix, group = "node", reasons.nodes
        record = names.node_record(identifier, status)
        if record is None:
            text = _safe_atom(identifier)
        else:
            location = _mapping(record.get("location"))
            path = record.get("path") or location.get("path")
            kind = record.get("symbol_kind") or record.get("node_class")
            where = ", ".join(_safe_atom(item) for item in (path, kind) if item)
            text = _safe_atom(record.get("label", identifier))
            if where:
                text += f" ({where})"
    elif domain == "graph-relationship":
        prefix, group = "relationship", reasons.relationships
        endpoints = names.relationships.get(identifier)
        if endpoints is None:
            text = _safe_atom(identifier)
        else:
            source, target, kind = endpoints
            text = (
                f"{_safe_atom(names.label(source, status))} -> "
                f"{_safe_atom(names.label(target, status))} ({_safe_atom(kind)})"
            )
    else:
        prefix, group = "call", reasons.calls
        endpoints = names.relationships.get(identifier)
        if endpoints is not None:
            text = (
                f"{_safe_atom(names.label(endpoints[0], status))} -> "
                f"{_safe_atom(names.label(endpoints[1], status))}"
            )
        else:
            text = _callee_location(change, status) or _safe_atom(identifier)
    line = f"{prefix} {_safe_atom(status)}: {text}"
    if status == "changed":
        stored = group.get(identifier, ())
        if stored:
            line += f" [{', '.join(_safe_atom(item) for item in stored)}]"
    return line + "\n"


def _callee_location(change: SystemChange, status: str) -> str | None:
    """Name a call without a graph relationship by its 1-based callee location."""
    side = _change_before(change) if status == "removed" else _change_after(change)
    callee = _mapping(_mapping_value(_first(side), "callee"))
    path = callee.get("path")
    start = _mapping(_mapping(callee.get("range")).get("start"))
    line = start.get("line")
    if path is None or not isinstance(line, int):
        return None
    return f"{_safe_atom(path)}:{line + 1}"


def _side_evidence(value: object) -> object:
    """Project the stored evidence of one change side, or ``unavailable``.

    Report and boundary payloads carry ``relationships``; relationship payloads
    carry ``evidence``; call payloads carry callee locations; a several-record
    group is a tuple whose records contribute in order.
    """
    if isinstance(value, (tuple, list)):
        collected: list[object] = []
        for record in value:
            found = _record_evidence(record)
            if isinstance(found, (tuple, list)):
                collected.extend(found)
            elif found is not None:
                collected.append(found)
        return tuple(collected) if collected else "unavailable"
    found = _record_evidence(value)
    return "unavailable" if found is None else found


def _record_evidence(value: object) -> object | None:
    for key in ("relationships", "evidence"):
        evidence = _mapping_value(value, key)
        if evidence is not None:
            return evidence
    callee = _mapping_value(value, "callee")
    return None if callee is None else (callee,)


def _change_before(change: object) -> object:
    return getattr(change, "old", getattr(change, "before", None))


def _change_after(change: object) -> object:
    return getattr(change, "new", getattr(change, "after", None))


def _detail_value(value: object) -> str:
    if value is None or value == "unavailable":
        return "unavailable"
    return dump_json(_thaw(value)).rstrip(chr(10))


_SENTINEL_LABELS = frozenset({"no_system", "external", "unassigned"})


def _literal_system_name(name: str) -> str:
    """Render a declared system name where a sentinel label could also appear.

    A name equal to a sentinel, or beginning with a quote, is JSON-quoted so it
    cannot read as the sentinel or as the quoted form of another name; every
    other name keeps its plain form.
    """
    if name in _SENTINEL_LABELS or name.startswith('"'):
        return json.dumps(name, ensure_ascii=True)
    return name


def _display_category(value: object) -> str:
    text = str(value)
    if text.startswith("system: "):
        return _literal_system_name(text.removeprefix("system: "))
    return text


def _mapping(value: object) -> Mapping[str, object]:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return converted
    return value if isinstance(value, Mapping) else {}


def _mapping_value(value: object, key: str) -> object | None:
    return _mapping(value).get(key)


def _mapping_side(value: object) -> str:
    """Read a literal membership owner; only a missing owner is unassigned."""
    side = _mapping_value(value, "system")
    if side is None:
        return "unassigned"
    return _literal_system_name(str(side))


def _record_category(value: object) -> str | None:
    """Read the stored destination category without decoding its prefix yet."""
    record = _mapping_value(value, "record")
    category = _mapping_value(record, "category")
    if category is None:
        category = _mapping_value(value, "category")
    if category is None:
        return None
    return str(category)


def _endpoint_display(payload: Mapping[str, object], endpoint_key: str, category_key: str) -> str:
    endpoint = _mapping(payload.get(endpoint_key))
    category = _display_category(payload.get(category_key, "unknown"))
    label = endpoint.get("label")
    if label is None:
        label = endpoint.get("path")
    if label is None:
        return category
    return f"{category}.{label}"


def _key_part(change: SystemChange, index: int) -> str:
    key = getattr(change, "key", ())
    if isinstance(key, str):
        return key if index == 0 else ""
    return str(key[index]) if index < len(key) else ""


def _change_status(change: object) -> str:
    value = getattr(change, "status", None)
    if not isinstance(value, str):
        value = getattr(change, "kind", "changed")
    return str(value)


def _safe_atom(value: object) -> str:
    if value is None:
        return "unavailable"
    text = str(value)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text
    ):
        return json.dumps(text, ensure_ascii=True)
    return text


def _thaw(value: object) -> object:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _thaw(to_dict())
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw(item) for item in sorted(value, key=repr)]
    return value


__all__ = ["filter_system_diff", "render_context_text", "render_json", "render_text"]
