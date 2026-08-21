"""Shared wire-format helpers for graph-model serialization and deserialization."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from types import MappingProxyType
from typing import cast


def reject_unpaired_surrogates(value: str, context: str) -> None:
    """Reject strings that are not valid Unicode scalar sequences.

    JSON text may encode a lone surrogate (``"\\ud800"``) that ``json.loads``
    happily turns into a Python ``str``. RFC 8785 JCS has no canonical UTF-8
    form for such a string, so it can never participate in a node identity
    input; rejecting it at construction keeps every identity reconstructible.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{context} must not contain unpaired surrogate code points") from None


def hashable_json(value: object) -> object:
    """Return a hashable, equality-preserving key for a frozen JSON-like value.

    Mappings become sorted ``(key, value)`` tuples and sequences become
    tuples, recursively; scalars pass through. Two values are equal under
    Python equality iff their keys are equal, so this can back a set or dict
    without changing the equality semantics of the original structures.
    """
    if isinstance(value, Mapping):
        return tuple(sorted((key, hashable_json(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(hashable_json(item) for item in value)
    return value


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
        # Accept any mapping, not only dict: dataclasses.replace() re-runs
        # __post_init__ on a model object whose extensions were already
        # frozen into MappingProxyType values, and that must stay valid.
        if not isinstance(value, Mapping):
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
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# RFC 8785 JCS (JSON Canonicalization Scheme)
# ---------------------------------------------------------------------------
#
# JCS defines a deterministic JSON serialization by specifying:
#   1. Object keys sorted by their UTF-16 code unit values.
#   2. Strings with minimal escaping (only the mandatory JSON escapes).
#   3. Numbers in their shortest representation (no leading zeros, no
#      trailing zeros after decimal point, no positive exponent sign).
#   4. No whitespace between tokens.
#
# This implementation covers strings, integers, booleans, null, arrays,
# and nested objects. IEEE 754 float serialization is out of scope for
# Minotaur v1 — floats raise TypeError.
#
# We implement JCS ourselves rather than adding a dependency because:
#   - The subset we need is compact and auditable.
#   - A JCS library would be Minotaur's only non-jsonschema dependency.
#   - Getting the serialization wrong would silently produce different
#     node IDs, so we want the logic visible and testable in-tree.

_JCS_ESCAPE_MAP: dict[str, str] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

for _i in range(0x20):
    _ch = chr(_i)
    if _ch not in _JCS_ESCAPE_MAP:
        _JCS_ESCAPE_MAP[_ch] = f"\\u{_i:04x}"


def _jcs_serialize(value: object) -> bytes:
    """Serialize a value to RFC 8785 JCS canonical form.

    Returns UTF-8 bytes because SHA-256 operates on bytes, and JCS defines
    the canonical encoding as UTF-8.
    """
    parts: list[str] = []
    _jcs_encode(value, parts)
    return "".join(parts).encode("utf-8")


def _jcs_encode(value: object, parts: list[str]) -> None:
    """Recursively encode a value into JCS string fragments."""
    if isinstance(value, bool):
        parts.append("true" if value else "false")

    elif value is None:
        parts.append("null")

    elif isinstance(value, str):
        parts.append('"')
        for ch in value:
            escaped = _JCS_ESCAPE_MAP.get(ch)
            if escaped is not None:
                parts.append(escaped)
            else:
                parts.append(ch)
        parts.append('"')

    elif isinstance(value, int):
        parts.append(str(value))

    elif isinstance(value, dict):
        parts.append("{")
        sorted_keys = sorted(value.keys(), key=_utf16_sort_key)
        for i, key in enumerate(sorted_keys):
            if i > 0:
                parts.append(",")
            _jcs_encode(key, parts)
            parts.append(":")
            _jcs_encode(value[key], parts)
        parts.append("}")

    elif isinstance(value, list | tuple):
        parts.append("[")
        for i, item in enumerate(value):
            if i > 0:
                parts.append(",")
            _jcs_encode(item, parts)
        parts.append("]")

    else:
        raise TypeError(
            f"JCS serialization does not support {type(value).__name__}; "
            f"Minotaur v1 does not implement IEEE 754 float serialization"
        )


def _utf16_sort_key(s: str) -> tuple[int, ...]:
    """Produce a sort key based on UTF-16 code unit values.

    RFC 8785 §3.2.3 specifies that object keys are sorted by comparing
    their UTF-16 representations code unit by code unit. For BMP characters
    this is the same as codepoint order, but supplementary characters
    (U+10000+) are represented as surrogate pairs and sort differently
    than their codepoint values would suggest.
    """
    raw = s.encode("utf-16-le")
    return struct.unpack(f"<{len(raw) // 2}H", raw)


def _sort_keys_recursive(value: object) -> object:
    """Recursively sort all dict keys by JCS UTF-16 code-unit order.

    Lists pass through with their element order preserved — domain-specific
    array sorting (nodes, relationships, evidence, locations) is the caller's
    responsibility.
    """
    if isinstance(value, dict):
        return {
            k: _sort_keys_recursive(v)
            for k, v in sorted(value.items(), key=lambda item: _utf16_sort_key(item[0]))
        }
    if isinstance(value, list):
        return [_sort_keys_recursive(item) for item in value]
    return value
