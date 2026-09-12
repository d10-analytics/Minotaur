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


def select_system(result: SystemDiffResult, name: str | None = None) -> SystemDiffResult:
    """Select one system from a complete result, replacing the prior view.

    ``result`` remains the caller-retained complete value.  Passing ``None``
    returns every category, while a name retains every change whose stored
    ``involved_systems`` contains it.  A later selection must therefore call
    this function with that same complete value; cumulative narrowing is not
    the default because it would hide a change involving the newly selected
    system.  An explicit intersection need is the point at which this policy
    should be revisited.
    """
    if not isinstance(result, SystemDiffResult):
        raise TypeError("select_system requires a SystemDiffResult")
    if name is None:
        return result

    names = tuple(sorted(set(result.old_system_names) | set(result.new_system_names)))
    if name not in names:
        raise UnknownSystem(name, difflib.get_close_matches(name, names, n=5))

    def retain(change: SystemChange) -> bool:
        return name in change.involved_systems

    kwargs: dict[str, object] = {
        "old_system_names": result.old_system_names,
        "new_system_names": result.new_system_names,
        "added_systems": tuple(item for item in result.added_systems if item == name),
        "removed_systems": tuple(item for item in result.removed_systems if item == name),
        "old_coverage": result.old_coverage,
        "new_coverage": result.new_coverage,
        "old_selection": result.old_selection,
        "new_selection": result.new_selection,
    }
    for field_name, _label in _CHANGE_GROUPS:
        kwargs[field_name] = tuple(
            change for change in getattr(result, field_name) if retain(change)
        )
    return SystemDiffResult(**cast(Any, kwargs))


def filter_result(result: SystemDiffResult, name: str | None = None) -> SystemDiffResult:
    """Compatibility spelling for :func:`select_system`."""
    return select_system(result, name)


def filter_system_diff(result: SystemDiffResult, name: str | None = None) -> SystemDiffResult:
    """Select a system from a complete typed comparison result."""
    return select_system(result, name)


def render_context(result: SystemDiffResult) -> str:
    """Render the four stored context mappings in their fixed order."""
    lines = []
    for label, value in (
        ("old coverage", result.old_coverage),
        ("new coverage", result.new_coverage),
        ("old selection", result.old_selection),
        ("new selection", result.new_selection),
    ):
        lines.append(f"{label} {dump_json(_thaw(value)).rstrip(chr(10))}\n")
    return "".join(lines)


def render_text(result: SystemDiffResult, *, details: bool = False) -> str:
    """Render structural changes followed by stored coverage and selection."""
    lines = [
        _change_line(label, change)
        for field_name, label in _CHANGE_GROUPS
        for change in _ordered(getattr(result, field_name))
    ]
    if result.added_systems:
        lines[0:0] = [f"system added: {_safe_atom(name)}\n" for name in result.added_systems]
    if result.removed_systems:
        offset = len(result.added_systems)
        lines[offset:offset] = [
            f"system removed: {_safe_atom(name)}\n" for name in result.removed_systems
        ]
    if not lines:
        lines.append("no changes\n")
    output = "".join(lines) + render_context(result)
    if details:
        output += render_details(result)
    return output


def render_details(result: SystemDiffResult) -> str:
    """Append exact stored records, endpoint projections, and evidence.

    Details are a JSON projection of the typed changes.  In particular, a
    missing old or new side remains JSON ``null`` rather than being inferred
    from the opaque boundary key.
    """
    lines: list[str] = []
    for change in result.differences:
        lines.append(f"details {dump_json(change.to_dict()).rstrip(chr(10))}\n")
    return "".join(lines)


def render_json(result: SystemDiffResult) -> str:
    """Render the selected typed result through the canonical JSON owner."""
    return dump_json(result.to_dict())


def render_system_diff_text(result: SystemDiffResult, *, details: bool = False) -> str:
    """Explicit alias for callers naming the result type."""
    return render_text(result, details=details)


def render_system_diff_json(result: SystemDiffResult) -> str:
    """Explicit alias for callers naming the result type."""
    return render_json(result)


def _ordered(changes: Sequence[SystemChange]) -> tuple[SystemChange, ...]:
    return tuple(
        sorted(
            changes,
            key=lambda change: (
                change.kind,
                change.key,
                repr(change.old),
                repr(change.new),
            ),
        )
    )


def _change_line(label: str, change: SystemChange) -> str:
    kind = _safe_atom(change.kind)
    if label == "membership":
        old = _mapping_side(change.old)
        new = _mapping_side(change.new)
        file = (
            _mapping_value(change.new, "file")
            or _mapping_value(change.old, "file")
            or _key_part(change, 0)
        )
        return f"membership {kind}: {_safe_atom(file)} — {_safe_atom(old)} -> {_safe_atom(new)}\n"
    if label == "surface":
        path = _key_part(change, 1)
        symbol = _key_part(change, 2)
        return f"surface {kind}: {_safe_atom(path)}  {_safe_atom(symbol)}\n"
    if label == "consumer":
        file = _key_part(change, 1)
        category = _record_category(change.new) or _record_category(change.old)
        suffix = f" ({_safe_atom(category)})" if category else ""
        return f"consumer {kind}: {_safe_atom(file)}{suffix}\n"
    if label == "dependency":
        source = _key_part(change, 0)
        category = (
            _record_category(change.new) or _record_category(change.old) or _key_part(change, 1)
        )
        return f"dependency {kind}: {_safe_atom(source)} -> {_safe_atom(category)}\n"
    if label == "boundary":
        payload = _mapping(change.new) or _mapping(change.old)
        source = _endpoint_display(payload, "source_endpoint", "source_category")
        target = _endpoint_display(payload, "target_endpoint", "target_category")
        relation = _mapping_value(payload, "kind") or "unknown"
        return (
            f"boundary {kind}: {_safe_atom(source)} -> {_safe_atom(target)} "
            f"({_safe_atom(relation)})\n"
        )
    raise AssertionError(f"unsupported change label: {label}")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mapping_value(value: object, key: str) -> object | None:
    return _mapping(value).get(key)


def _mapping_side(value: object) -> str:
    side = _mapping_value(value, "system")
    if side is None:
        return "unassigned"
    text = str(side)
    return text.removeprefix("system: ")


def _record_category(value: object) -> str | None:
    record = _mapping_value(value, "record")
    category = _mapping_value(record, "category")
    if category is None:
        category = _mapping_value(value, "category")
    if category is None:
        return None
    return str(category).removeprefix("system: ")


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
    return str(change.key[index]) if index < len(change.key) else ""


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


__all__ = [
    "filter_result",
    "filter_system_diff",
    "render_context",
    "render_details",
    "render_json",
    "render_system_diff_json",
    "render_system_diff_text",
    "render_text",
    "select_system",
]
