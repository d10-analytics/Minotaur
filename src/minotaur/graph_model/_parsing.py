"""Shared wire-format checks for graph-model deserialization."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast


def reject_unknown_fields(data: dict[str, object], allowed: frozenset[str], context: str) -> None:
    """Reject fields that the v1 schema does not define for ``context``."""
    unknown = data.keys() - allowed
    if unknown:
        fields = ", ".join(repr(field) for field in sorted(unknown))
        raise ValueError(f"{context} has unsupported field(s): {fields}")


def validate_extensions(
    extensions: Mapping[str, Mapping[str, object]] | None,
) -> None:
    """Validate the v1 extension-object shape shared by all model objects."""
    if extensions is None:
        return
    for name, value in extensions.items():
        if not isinstance(name, str) or not name:
            raise ValueError("extension names must be non-empty strings")
        if not isinstance(value, dict):
            raise ValueError(f"extension {name!r} must be an object")


def freeze_extensions(
    extensions: Mapping[str, Mapping[str, object]] | None,
) -> Mapping[str, Mapping[str, object]] | None:
    """Deep-freeze an extension map held by a frozen graph-model object."""
    validate_extensions(extensions)
    if extensions is None:
        return None
    return MappingProxyType(
        {
            name: cast(Mapping[str, object], _freeze_json(value))
            for name, value in extensions.items()
        }
    )


def serialize_extensions(
    extensions: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, dict[str, object]] | None:
    """Return an immutable extension map as JSON-compatible mutable values."""
    if extensions is None:
        return None
    result: dict[str, dict[str, object]] = {}
    for name, value in extensions.items():
        thawed = _thaw_json(value)
        if not isinstance(thawed, dict):  # pragma: no cover - protected by freeze_extensions
            raise AssertionError("an extension value must remain an object")
        result[name] = thawed
    return result


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
