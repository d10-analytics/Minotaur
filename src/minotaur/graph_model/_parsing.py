"""Shared wire-format helpers for graph-model serialization and deserialization."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import cast


def type_error(context: str, value: object, expected: str) -> ValueError:
    """Build the model-layer type rejection shared by every ``__post_init__``.

    ``R-02`` makes the model layer the sole owner of the wire-format invariant
    on every path, including in-process construction and
    ``dataclasses.replace``.  The wire parsers type-check before constructing,
    so these guards are the only thing standing between a hand-built object and
    a serializer that would happily encode, say, ``"label": 1.5`` into bytes
    that no reader can reproduce.  Every guard raises ``ValueError`` — the error
    type the surrounding blocks already use — so a caller's ``except
    ValueError`` keeps catching the whole class of construction failures.
    """
    return ValueError(f"{context} must be {expected}, got {type(value).__name__}")


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
    # ``dict_keys`` can compare itself to the allowed set without materializing
    # a difference set.  Graph loading visits this helper for every model
    # object, so keep the common, conforming path allocation-free and only
    # build the set needed to render the diagnostic when a field is unknown.
    if data.keys() <= allowed:
        return
    fields = ", ".join(repr(field) for field in sorted(data.keys() - allowed))
    raise ValueError(f"{context} has unsupported field(s): {fields}")


def validate_extensions(
    extensions: Mapping[str, Mapping[str, object]] | None,
) -> None:
    """Validate the v1 extension-object shape shared by all model objects."""
    if extensions is None:
        return
    if not isinstance(extensions, Mapping):
        raise type_error("extensions", extensions, "an object")
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


def _json_pointer(segments: Sequence[str | int]) -> str:
    """Render accumulated path segments as the JSON-pointer-style error path."""
    return "".join(f"/{segment}" for segment in segments)


# The range ``orjson`` decodes as an ``int``: a literal outside it comes back as
# a float and is rejected by the model layer, so an in-process document must
# not be allowed to serialize an integer that no reader can load back (M-4).
_INT_MIN = -(2**63)
_INT_MAX = 2**64 - 1
_MAX_EXTENSION_DEPTH = 64


def _freeze_json(
    value: object,
    path: list[str | int] | None = None,
    *,
    container_depth: int = -1,
) -> object:
    # ``path`` is a mutable stack of segments rather than a pre-rendered string:
    # every extension value on a graph load walks this function, and the pointer
    # text is only ever consumed by a ``raise``.  Pushing and popping a segment
    # costs no allocation, whereas the previous eager ``f"{path}/{key}"`` built
    # one throwaway string per key and per list index on the hot path.
    if path is None:
        path = []
    if isinstance(value, Mapping):
        if container_depth > _MAX_EXTENSION_DEPTH:
            raise ValueError(
                f"extension nesting at {_json_pointer(path)} exceeds {_MAX_EXTENSION_DEPTH} levels"
            )
        frozen: dict[object, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                # ``json.dumps`` silently stringifies non-``str`` keys, which
                # would let an in-process document serialize to bytes whose keys
                # never round-trip back to the constructed value.
                raise ValueError(
                    f"extension object at {_json_pointer(path)} has a non-string key: {key!r}"
                )
            if not key:
                # Mirrors the schema's ``propertyNames.minLength: 1`` on
                # ``extensionObject`` so the trusted (schema-skipping) path and
                # the full-validation path reach the same verdict.
                raise ValueError(f"extension object at {_json_pointer(path)} has an empty key")
            if max(key) > "\uffff":
                raise ValueError(
                    f"extension value at {_json_pointer([*path, key])} has a non-BMP key"
                )
            # A lone surrogate is a BMP code point, so the check above lets it
            # through, yet UTF-8 cannot encode it and ``serialize`` would fail.
            try:
                key.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError(
                    f"extension key {key!r} at {_json_pointer(path)} must not contain "
                    "unpaired surrogate code points"
                ) from None
            path.append(key)
            item_depth = (
                container_depth + 1 if isinstance(item, Mapping | list | tuple) else container_depth
            )
            frozen[key] = _freeze_json(item, path, container_depth=item_depth)
            path.pop()
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        if container_depth > _MAX_EXTENSION_DEPTH:
            raise ValueError(
                f"extension nesting at {_json_pointer(path)} exceeds {_MAX_EXTENSION_DEPTH} levels"
            )
        frozen_items: list[object] = []
        for index, item in enumerate(value):
            path.append(index)
            item_depth = (
                container_depth + 1 if isinstance(item, Mapping | list | tuple) else container_depth
            )
            frozen_items.append(_freeze_json(item, path, container_depth=item_depth))
            path.pop()
        return tuple(frozen_items)
    # Leaves are whitelisted, not blacklisted: the schema's ``extensionValue``
    # admits exactly string | integer | boolean | null, and anything else
    # (``set``, ``bytes``, ``Decimal`` ...) would only fail later inside the
    # encoder, after the model claimed to own the invariant.
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not _INT_MIN <= value <= _INT_MAX:
            raise ValueError(
                f"extension value at {_json_pointer(path)} must fit in 64 bits, got {value!r}"
            )
        return value
    if isinstance(value, float):
        raise ValueError(
            f"extension value at {_json_pointer(path)} must be an integer, got float: {value!r}"
        )
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                f"extension value at {_json_pointer(path)} must not contain "
                "unpaired surrogate code points"
            ) from None
        return value
    raise ValueError(
        f"extension value at {_json_pointer(path)} must be a string, integer, boolean,"
        f" null, array or object, got {type(value).__name__}"
    )


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
# The model layer rejects non-integer extension values and non-BMP extension
# keys before typed output reaches this encoder; this function is not a
# validation boundary.
#
# The previous hand-written encoder now lives in
# ``tests/test_graph_model_serialization.py`` as the oracle, ensuring
# byte-identical output.


def _jcs_serialize(value: object) -> bytes:
    """Serialize typed model output to RFC 8785 JCS UTF-8 bytes.

    Validation belongs to the model layer; this encoder only performs the
    canonical JSON encoding and retains the C encoder's non-finite-number
    guard.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
