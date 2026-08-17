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
  - Dict keys sorted by JCS UTF-16 code-unit order (RFC 8785 §3.2.3).
"""

from __future__ import annotations

from typing import Any, cast

from minotaur.graph_model._parsing import _jcs_serialize, _sort_keys_recursive
from minotaur.graph_model.document import GraphDocument


def canonicalize(document: GraphDocument) -> dict[str, object]:
    """Return a canonical dict representation of the document.

    Calls ``document.to_dict()`` then applies canonical ordering to the
    resulting dict tree. The input document is not mutated.
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

    return cast(dict[str, object], _sort_keys_recursive(raw))


def serialize(document: GraphDocument) -> bytes:
    """Return the canonical JCS UTF-8 byte serialization of the document.

    Equivalent to JCS-serializing the result of ``canonicalize(document)``.
    Suitable for hashing and byte-level comparison.
    """
    return _jcs_serialize(canonicalize(document))


def _location_sort_key(loc: dict[str, Any]) -> tuple[str, int, int, int, int]:
    r = loc["range"]
    return (
        loc["path"],
        r["start"]["line"],
        r["start"]["character"],
        r["end"]["line"],
        r["end"]["character"],
    )
