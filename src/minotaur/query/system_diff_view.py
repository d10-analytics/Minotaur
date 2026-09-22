"""Pure views over one complete :class:`SystemDiffResult`.

The comparator owns admission, structural categories, status, and the complete
context.  This module only projects that already-computed value: selecting a
system never reacquires a snapshot or recomputes a relationship.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
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


def render_text(result: SystemDiffResult, *, details: bool = False) -> str:
    """Render structural changes followed by stored coverage and selection."""
    lines = [
        _change_lines(label, change, details=details)
        for field_name, label in _CHANGE_GROUPS
        for change in _ordered(getattr(result, field_name))
    ]
    if result.added_systems:
        lines[0:0] = [
            _change_lines(
                "systems",
                SystemChange(
                    "systems", "added", (name,), new={"name": name}, involved_systems=(name,)
                ),
                details=details,
            )
            for name in result.added_systems
        ]
    if result.removed_systems:
        offset = len(result.added_systems)
        lines[offset:offset] = [
            _change_lines(
                "systems",
                SystemChange(
                    "systems", "removed", (name,), old={"name": name}, involved_systems=(name,)
                ),
                details=details,
            )
            for name in result.removed_systems
        ]
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
    if label == "graph-node":
        return f"node {kind}: {_safe_atom(_key_part(change, 0))}\n"
    if label == "graph-relationship":
        return f"relationship {kind}: {_safe_atom(_key_part(change, 0))}\n"
    if label == "call":
        return f"call {kind}: {_safe_atom(_key_part(change, 0))}\n"
    raise AssertionError(f"unsupported change label: {label}")


def _side_evidence(value: object) -> object:
    evidence = _mapping_value(value, "relationships")
    if evidence is None:
        return "unavailable"
    return evidence


def _change_before(change: object) -> object:
    return getattr(change, "old", getattr(change, "before", None))


def _change_after(change: object) -> object:
    return getattr(change, "new", getattr(change, "after", None))


def _detail_value(value: object) -> str:
    if value is None or value == "unavailable":
        return "unavailable"
    return dump_json(_thaw(value)).rstrip(chr(10))


def _display_category(value: object) -> str:
    return str(value).removeprefix("system: ")


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
    return str(side)


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
    category = str(payload.get(category_key, "unknown")).removeprefix("system: ")
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
    value = getattr(change, "kind", None)
    if isinstance(value, str):
        return value
    value = getattr(change, "status", "changed")
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
