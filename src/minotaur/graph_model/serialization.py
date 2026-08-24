"""Canonical normalization and serialization for Minotaur graph documents.

This module sits after semantic validation in the processing pipeline:
parse JSON → schema validation → model construction → semantic validation →
**canonical normalization** → render or slice.

Canonical normalization produces a deterministic dict (and optionally JCS
bytes) from a validated ``GraphDocument``. Two independently assembled
snapshots of the same analysis produce identical canonical output. The
normalizer only sorts validated content — it never coerces values, inserts
defaults, or invents missing data.

Ordering rules (accepted 2026-08-16):
  - Nodes sorted by ``id`` (lexicographic).
  - Relationships sorted by ``(source, target, kind)``.
  - Evidence sorted by JCS serialization of each record after its location
    list is normalized.
  - Locations sorted by ``(path, start.line, start.character, end.line,
    end.character)``.
  - Dict keys sorted by Unicode code point, which equals JCS UTF-16 code-unit
    order for every format-valid document (RFC 8785 §3.2.3).
"""

from __future__ import annotations

from typing import Any, cast

from minotaur.graph_model._parsing import _jcs_serialize
from minotaur.graph_model.document import GraphDocument


def canonicalize(document: GraphDocument) -> dict[str, object]:
    """Return a canonical dict representation of the document.

    Calls ``document.to_dict()`` then applies canonical array and dict-key
    ordering to the resulting dict tree. The input document is not mutated.
    """
    return cast(dict[str, object], _sort_keys_code_point(_canonical_arrays(document)))


def _sort_keys_code_point(value: object) -> object:
    """Recursively order JSON object keys by Unicode code point.

    Valid graph-model extension keys are restricted to the Basic Multilingual
    Plane, where this order is identical to RFC 8785's UTF-16 code-unit order.
    Arrays retain their order because semantic array ordering is performed by
    ``_canonical_arrays`` before this dict-only normalization.
    """
    if isinstance(value, dict):
        return {
            key: _sort_keys_code_point(item)
            for key, item in sorted(value.items(), key=lambda entry: entry[0])
        }
    if isinstance(value, list | tuple):
        return [_sort_keys_code_point(item) for item in value]
    return value


def _canonical_arrays(document: GraphDocument) -> dict[str, object]:
    """Return a canonical dict with semantic arrays ordered.

    This performs the array ordering shared by the public ``canonicalize``
    view and the byte serializer. Dict-key ordering belongs to
    ``canonicalize`` for dict consumers and to ``_jcs_serialize`` for bytes.
    """
    raw = document.to_dict()
    nodes = cast(list[dict[str, Any]], raw["nodes"])
    relationships = cast(list[dict[str, Any]], raw["relationships"])

    for rel in relationships:
        for ev in rel["evidence"]:
            if "locations" in ev:
                ev["locations"] = sorted(ev["locations"], key=_location_sort_key)
        rel["evidence"] = sorted(
            rel["evidence"],
            key=lambda ev: _jcs_serialize(ev),
        )

    raw["relationships"] = sorted(
        relationships,
        key=lambda r: (r["source"], r["target"], r["kind"]),
    )

    raw["nodes"] = sorted(nodes, key=lambda n: n["id"])

    return raw


def serialize(document: GraphDocument) -> bytes:
    """Return the canonical JCS UTF-8 byte serialization of the document.

    Semantic arrays are ordered by ``_canonical_arrays``; dict keys are
    ordered by the JCS encoder. This avoids the public ``canonicalize``
    dict-key pass while producing the same bytes. Suitable for hashing and
    byte-level comparison.
    """
    return _jcs_serialize(_canonical_arrays(document))


def _location_sort_key(loc: dict[str, Any]) -> tuple[str, int, int, int, int]:
    r = loc["range"]
    return (
        loc["path"],
        r["start"]["line"],
        r["start"]["character"],
        r["end"]["line"],
        r["end"]["character"],
    )
