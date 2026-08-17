"""Behavioral coverage for canonical normalization and JCS extensions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur.graph_model._parsing import _jcs_serialize
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.serialization import canonicalize, serialize

EXAMPLES = Path(__file__).parents[1] / "examples/synthetic-graphs"


# ---------------------------------------------------------------------------
# Helper: build a minimal valid document dict for programmatic tests
# ---------------------------------------------------------------------------


def _make_node(
    suffix: str,
    *,
    label: str = "f",
    line: int = 0,
    char: int = 0,
    end_char: int = 1,
    path: str = "src/a.py",
) -> dict[str, object]:
    """Build a node dict whose id ends with *suffix* for sort-order testing.

    Real IDs are SHA-256 hashes, but the serializer sorts by lexicographic
    string comparison on the full ``id`` value — so using synthetic ids that
    sort predictably is sufficient.
    """
    from minotaur.graph_model.identity import NodeIdentity, compute_node_id
    from minotaur.graph_model.location import Location, Position, Range
    from minotaur.graph_model.provenance import IdentityBasis

    loc = Location(path, Range(Position(line, char), Position(line, end_char)))
    identity = NodeIdentity(basis=IdentityBasis.SOURCE_LOCATION, namespace="test")
    node_id = compute_node_id(
        identity,
        node_class="symbol",
        symbol_kind="function",
        path=path,
        location=loc,
    )
    _ = suffix  # suffix is for the caller's readability, id is computed
    return {
        "id": node_id,
        "identity": {"basis": "source-location", "namespace": "test"},
        "node_class": "symbol",
        "symbol_kind": "function",
        "label": label,
        "language": "python",
        "location": loc.to_dict(),
    }


def _loc(path: str, sl: int, sc: int, el: int, ec: int) -> dict[str, object]:
    return {
        "path": path,
        "range": {
            "start": {"line": sl, "character": sc},
            "end": {"line": el, "character": ec},
        },
    }


def _wrap_document(
    nodes: list[dict[str, object]],
    relationships: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "format": "minotaur-graph",
        "format_version": "0.1.0",
        "coordinate_encoding": "utf-8",
        "nodes": nodes,
        "relationships": relationships or [],
    }


# ---------------------------------------------------------------------------
# Canonicalize: idempotency and fixture round-trips
# ---------------------------------------------------------------------------


def test_canonicalize_idempotent_on_all_fixtures() -> None:
    for path in sorted(EXAMPLES.glob("*.json")):
        source = json.loads(path.read_text(encoding="utf-8"))
        doc = GraphDocument.from_dict(source)
        first = canonicalize(doc)
        second_doc = GraphDocument.from_dict(first)
        second = canonicalize(second_doc)
        assert first == second, f"not idempotent for {path.name}"


# ---------------------------------------------------------------------------
# Canonicalize: node ordering
# ---------------------------------------------------------------------------


def test_canonicalize_sorts_nodes_by_id() -> None:
    n1 = _make_node("a", label="alpha", line=0, path="src/a.py")
    n2 = _make_node("b", label="beta", line=1, path="src/a.py")

    forward_order = [n1, n2] if n1["id"] < n2["id"] else [n2, n1]
    reverse_order = list(reversed(forward_order))

    doc = GraphDocument.from_dict(_wrap_document(reverse_order))
    result = canonicalize(doc)
    ids = [n["id"] for n in result["nodes"]]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Canonicalize: relationship ordering
# ---------------------------------------------------------------------------


def test_canonicalize_sorts_relationships_by_tuple_key() -> None:
    n1 = _make_node("a", label="alpha", line=0, path="src/a.py")
    n2 = _make_node("b", label="beta", line=1, path="src/a.py")
    n3 = _make_node("c", label="gamma", line=2, path="src/a.py")
    nodes = sorted([n1, n2, n3], key=lambda n: n["id"])

    ev = [{"provenance": "static-analysis", "producer": {"name": "test"}}]
    r1 = {"source": nodes[0]["id"], "target": nodes[1]["id"], "kind": "calls", "evidence": ev}
    r2 = {"source": nodes[0]["id"], "target": nodes[2]["id"], "kind": "calls", "evidence": ev}
    r3 = {"source": nodes[1]["id"], "target": nodes[2]["id"], "kind": "calls", "evidence": ev}

    doc = GraphDocument.from_dict(_wrap_document(nodes, [r3, r1, r2]))
    result = canonicalize(doc)
    keys = [(r["source"], r["target"], r["kind"]) for r in result["relationships"]]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Canonicalize: evidence ordering
# ---------------------------------------------------------------------------


def test_canonicalize_sorts_evidence_by_jcs() -> None:
    n1 = _make_node("a", label="alpha", line=0, path="src/a.py")
    n2 = _make_node("b", label="beta", line=1, path="src/a.py")

    ev_imported = {
        "provenance": "imported-graph",
        "producer": {"name": "external-tool", "version": "1.0"},
    }
    ev_static = {
        "provenance": "static-analysis",
        "producer": {"name": "minotaur-python", "version": "0.1.0"},
        "locations": [_loc("src/a.py", 3, 0, 3, 5)],
    }

    rel = {
        "source": n1["id"],
        "target": n2["id"],
        "kind": "calls",
        "evidence": [ev_static, ev_imported],
    }
    doc = GraphDocument.from_dict(_wrap_document([n1, n2], [rel]))
    result = canonicalize(doc)

    evidence = result["relationships"][0]["evidence"]
    jcs_keys = [_jcs_serialize(e) for e in evidence]
    assert jcs_keys == sorted(jcs_keys)


# ---------------------------------------------------------------------------
# Canonicalize: location ordering within evidence
# ---------------------------------------------------------------------------


def test_canonicalize_sorts_locations_within_evidence() -> None:
    n1 = _make_node("a", label="alpha", line=0, path="src/a.py")
    n2 = _make_node("b", label="beta", line=1, path="src/a.py")

    loc_later = _loc("src/a.py", 5, 0, 5, 8)
    loc_earlier = _loc("src/a.py", 2, 0, 2, 8)

    rel = {
        "source": n1["id"],
        "target": n2["id"],
        "kind": "calls",
        "evidence": [
            {
                "provenance": "static-analysis",
                "producer": {"name": "test"},
                "locations": [loc_later, loc_earlier],
            }
        ],
    }
    doc = GraphDocument.from_dict(_wrap_document([n1, n2], [rel]))
    result = canonicalize(doc)

    locs = result["relationships"][0]["evidence"][0]["locations"]
    assert locs[0]["range"]["start"]["line"] == 2
    assert locs[1]["range"]["start"]["line"] == 5


# ---------------------------------------------------------------------------
# Canonicalize: dict key ordering
# ---------------------------------------------------------------------------


def test_canonicalize_sorts_dict_keys_by_utf16() -> None:
    source = json.loads((EXAMPLES / "small-workflow.json").read_text(encoding="utf-8"))
    doc = GraphDocument.from_dict(source)
    result = canonicalize(doc)

    def check_keys_sorted(obj: object) -> None:
        if isinstance(obj, dict):
            keys = list(obj.keys())
            assert keys == sorted(keys), f"keys not sorted: {keys}"
            for v in obj.values():
                check_keys_sorted(v)
        elif isinstance(obj, list):
            for item in obj:
                check_keys_sorted(item)

    check_keys_sorted(result)


# ---------------------------------------------------------------------------
# Canonicalize: no mutation
# ---------------------------------------------------------------------------


def test_canonicalize_does_not_mutate_document() -> None:
    source = json.loads((EXAMPLES / "provenance-demo.json").read_text(encoding="utf-8"))
    doc = GraphDocument.from_dict(source)
    before = doc.to_dict()
    canonicalize(doc)
    after = doc.to_dict()
    assert before == after


# ---------------------------------------------------------------------------
# Serialize: determinism and consistency
# ---------------------------------------------------------------------------


def test_serialize_deterministic_bytes() -> None:
    source = json.loads((EXAMPLES / "provenance-demo.json").read_text(encoding="utf-8"))
    doc = GraphDocument.from_dict(source)
    assert serialize(doc) == serialize(doc)


def test_serialize_matches_jcs_of_canonicalize() -> None:
    source = json.loads((EXAMPLES / "provenance-demo.json").read_text(encoding="utf-8"))
    doc = GraphDocument.from_dict(source)
    assert serialize(doc) == _jcs_serialize(canonicalize(doc))


# ---------------------------------------------------------------------------
# Shuffled input → canonical output
# ---------------------------------------------------------------------------


def test_shuffled_input_produces_canonical_output() -> None:
    n1 = _make_node("a", label="alpha", line=0, path="src/a.py")
    n2 = _make_node("b", label="beta", line=1, path="src/a.py")
    n3 = _make_node("c", label="gamma", line=2, path="src/a.py")
    nodes_sorted = sorted([n1, n2, n3], key=lambda n: n["id"])

    loc1 = _loc("src/a.py", 3, 0, 3, 5)
    loc2 = _loc("src/a.py", 7, 0, 7, 5)

    ev_a = {
        "provenance": "static-analysis",
        "producer": {"name": "test"},
        "locations": [loc2, loc1],
    }
    ev_b = {
        "provenance": "imported-graph",
        "producer": {"name": "external"},
    }
    rel_1 = {
        "source": nodes_sorted[0]["id"],
        "target": nodes_sorted[1]["id"],
        "kind": "calls",
        "evidence": [ev_a, ev_b],
    }
    rel_2 = {
        "source": nodes_sorted[0]["id"],
        "target": nodes_sorted[2]["id"],
        "kind": "calls",
        "evidence": [ev_b],
    }

    shuffled = _wrap_document(
        list(reversed(nodes_sorted)),
        [rel_2, rel_1],
    )
    canonical = _wrap_document(nodes_sorted, [rel_1, rel_2])

    doc_shuffled = GraphDocument.from_dict(shuffled)
    doc_canonical = GraphDocument.from_dict(canonical)

    assert serialize(doc_shuffled) == serialize(doc_canonical)


# ---------------------------------------------------------------------------
# JCS extensions: bool, null, array, float rejection
# ---------------------------------------------------------------------------


def test_jcs_bool() -> None:
    assert _jcs_serialize(True) == b"true"
    assert _jcs_serialize(False) == b"false"


def test_jcs_null() -> None:
    assert _jcs_serialize(None) == b"null"


def test_jcs_array() -> None:
    assert _jcs_serialize([1, "a", True, None]) == b'[1,"a",true,null]'


def test_jcs_float_raises() -> None:
    with pytest.raises(TypeError, match="float"):
        _jcs_serialize(3.14)


def test_jcs_nested_structure() -> None:
    value = {"b": [True, None, {"z": 1, "a": 2}], "a": "hello"}
    result = _jcs_serialize(value)
    assert result == b'{"a":"hello","b":[true,null,{"a":2,"z":1}]}'
