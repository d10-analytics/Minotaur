"""Shared wire-format helpers for graph-model serialization and deserialization."""

from __future__ import annotations

import json
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
    return cast(Mapping[str, Mapping[str, object]], _freeze_json(extensions))


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


def _freeze_json(value: object, path: str = "") -> object:
    if isinstance(value, Mapping):
        frozen: dict[object, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and max(key, default="") > "\uffff":
                raise ValueError(f"extension value at {path}/{key} has a non-BMP key")
            frozen[key] = _freeze_json(item, f"{path}/{key}")
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item, f"{path}/{index}") for index, item in enumerate(value))
    if isinstance(value, float):
        raise ValueError(f"extension value at {path} must be an integer, got float: {value!r}")
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# RFC 8785 JCS (JSON Canonicalization Scheme) — C-encoder composition
# ---------------------------------------------------------------------------
#
# JCS defines a deterministic JSON serialization by specifying:
#   1. Object keys sorted by their UTF-16 code unit values.
#   2. Strings with minimal escaping (only the mandatory JSON escapes).
#   3. Numbers in their shortest representation (no leading zeros, no
#      trailing zeros after decimal point, no positive exponent sign).
#   4. No whitespace between tokens.
#
# This implementation composes CPython's C ``json`` encoder with a
# check-only walk (``_check_canonical_input``) that rejects floats (whose
# C-encoder ``repr`` differs from JCS) and detects non-BMP dict keys
# (whose code-point sort order differs from JCS's UTF-16 order).
#
# Common path (no astral keys, which is every Minotaur-produced graph):
#   ``json.dumps(value, sort_keys=True, ...)`` — code-point key order
#   equals UTF-16 order when no key has a surrogate pair.
#
# Astral path: ``json.dumps(_sort_keys_recursive(value), sort_keys=False,
#   ...)`` — the existing UTF-16 walker orders keys, the C encoder keeps
#   insertion order.
#
# The previous hand-written encoder now lives in
# ``tests/test_graph_model_serialization.py`` as the oracle, ensuring
# byte-identical output.


def _check_canonical_input(value: object) -> bool:
    """Walk *value* checking JCS preconditions; return whether any dict key is non-BMP.

    Raises ``TypeError`` on any ``float`` leaf (JCS float serialization is
    not implemented in Minotaur v1). Returns ``True`` if any dict key
    contains a character outside the Basic Multilingual Plane, which
    requires the astral-key sort path.
    """
    has_astral = False
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, float):
            raise TypeError(
                f"JCS serialization does not support {type(current).__name__}; "
                f"Minotaur v1 does not implement IEEE 754 float serialization"
            )
        elif isinstance(current, dict):
            for key in current:
                if not has_astral and len(key) != len(key.encode("utf-16-le")) // 2:
                    has_astral = True
                stack.append(current[key])
        elif isinstance(current, list | tuple):
            stack.extend(current)
        # str, int, bool, None — no action needed
    return has_astral


def _jcs_serialize(value: object) -> bytes:
    """Serialize a value to RFC 8785 JCS canonical form.

    Returns UTF-8 bytes because SHA-256 operates on bytes, and JCS defines
    the canonical encoding as UTF-8.
    """
    has_astral = _check_canonical_input(value)
    if has_astral:
        ordered = _sort_keys_recursive(value)
        return json.dumps(
            ordered, sort_keys=False, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
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

    Lists and tuples are both recursed into and returned as lists — JSON has a
    single array type, so a tuple encodes exactly as a list does. Their element
    order passes through unchanged: domain-specific array sorting (nodes,
    relationships, evidence, locations) is the caller's responsibility.
    """
    if isinstance(value, dict):
        return {
            k: _sort_keys_recursive(v)
            for k, v in sorted(value.items(), key=lambda item: _utf16_sort_key(item[0]))
        }
    if isinstance(value, list | tuple):
        return [_sort_keys_recursive(item) for item in value]
    return value
