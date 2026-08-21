"""Behavioral coverage for canonical normalization and JCS extensions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur.graph_model._parsing import _jcs_serialize, _utf16_sort_key
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.serialization import canonicalize, serialize

EXAMPLES = Path(__file__).parents[1] / "examples/synthetic-graphs"
REPO_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# Oracle: verbatim previous pure-Python JCS encoder (AC-10)
# ---------------------------------------------------------------------------
# This code was moved verbatim from src/minotaur/graph_model/_parsing.py.
# It serves as the ground-truth reference for the C-encoder composition that
# replaced it. The oracle must never be modified to match the implementation;
# if they disagree the implementation is wrong.

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


def _oracle_serialize(value: object) -> bytes:
    """Oracle JCS serialization — the previous pure-Python encoder."""
    parts: list[str] = []
    _jcs_encode(value, parts)
    return "".join(parts).encode("utf-8")


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


# ---------------------------------------------------------------------------
# AC-10 (a): Oracle equality over every graph fixture
# ---------------------------------------------------------------------------


def _collect_graph_fixtures() -> list[Path]:
    """Return all valid JSON graph fixtures under tests/ and examples/."""
    paths: list[Path] = []
    for directory in [REPO_ROOT / "tests", REPO_ROOT / "examples"]:
        paths.extend(sorted(directory.rglob("*.json")))
    return paths


@pytest.mark.parametrize(
    "fixture_path",
    _collect_graph_fixtures(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_oracle_equality_fixtures(fixture_path: Path) -> None:
    """Implementation and oracle produce identical bytes for every fixture."""
    source = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert _jcs_serialize(source) == _oracle_serialize(source)


# ---------------------------------------------------------------------------
# AC-10 (b): Generated edge-case sweep — oracle equality
# ---------------------------------------------------------------------------


_EDGE_CASES: list[tuple[str, object]] = [
    # Astral-plane dict keys (non-BMP, surrogate pairs in UTF-16)
    ("astral_key_musical", {"\U0001d11e": "treble clef"}),
    ("astral_key_emoji", {"\U0001f600": "grinning"}),
    # Astral keys in both insertion orders
    ("astral_two_keys_ab", {"\U0001d11e": 1, "\U0001f600": 2}),
    ("astral_two_keys_ba", {"\U0001f600": 2, "\U0001d11e": 1}),
    # Private use area U+E000–U+FFFF (BMP, no surrogate pairs)
    ("pua_key_e000", {"": "private use start"}),
    ("pua_key_fffd", {"�": "replacement char"}),
    # Every control character 0x00–0x1F
    *[
        (f"control_0x{i:02x}", {f"key_{i:02x}": f"val\x00contains{chr(i)}ctrl"})
        for i in range(0x20)
    ],
    # Quote and backslash
    ("quote_in_value", {"k": 'say "hello"'}),
    ("backslash_in_value", {"k": "path\\to\\file"}),
    ("quote_in_key", {'"quoted"': "v"}),
    ("backslash_in_key", {"back\\slash": "v"}),
    # Non-ASCII text
    ("non_ascii_value", {"k": "café üñîçøðé"}),
    ("non_ascii_key", {"üñî": "unicode key"}),
    ("cjk_text", {"k": "世界"}),
    # Ints beyond 2**53
    ("big_int_pos", {"n": 2**53 + 1}),
    ("big_int_neg", {"n": -(2**53 + 1)}),
    ("very_big_int", {"n": 2**128}),
    # Nested empty containers
    ("empty_dict", {}),
    ("empty_list", []),
    ("nested_empty", {"a": {}, "b": [], "c": {"d": []}}),
    ("deeply_nested_empty", [[[[{}]]]]),
    # Scalars
    ("bool_true", True),
    ("bool_false", False),
    ("null", None),
    ("int_zero", 0),
    ("int_negative", -42),
    ("string_empty", ""),
    ("string_simple", "hello"),
    # Mixed nesting
    ("mixed_deep", {"z": [1, {"b": 2, "a": [True, None, "x"]}, 3], "a": "first"}),
]


@pytest.mark.parametrize("name,value", _EDGE_CASES, ids=[c[0] for c in _EDGE_CASES])
def test_oracle_equality_edge_cases(name: str, value: object) -> None:
    """Implementation and oracle agree on every edge-case input."""
    _ = name
    assert _jcs_serialize(value) == _oracle_serialize(value)


# ---------------------------------------------------------------------------
# AC-10 (c): Float TypeError — both paths reject floats
# ---------------------------------------------------------------------------


def test_float_raises_implementation_top_level() -> None:
    with pytest.raises(TypeError, match="float"):
        _jcs_serialize(3.14)


def test_float_raises_oracle_top_level() -> None:
    with pytest.raises(TypeError, match="float"):
        _oracle_serialize(3.14)


def test_float_raises_implementation_nested_dict() -> None:
    with pytest.raises(TypeError, match="float"):
        _jcs_serialize({"a": 1.5})


def test_float_raises_oracle_nested_dict() -> None:
    with pytest.raises(TypeError, match="float"):
        _oracle_serialize({"a": 1.5})


def test_float_raises_implementation_nested_list() -> None:
    with pytest.raises(TypeError, match="float"):
        _jcs_serialize([1, 2.0, 3])


def test_float_raises_oracle_nested_list() -> None:
    with pytest.raises(TypeError, match="float"):
        _oracle_serialize([1, 2.0, 3])


# ---------------------------------------------------------------------------
# AC-11: No production import of pure-Python encoder
# ---------------------------------------------------------------------------


def test_no_production_jcs_encode_or_escape_map() -> None:
    """_jcs_encode and _JCS_ESCAPE_MAP must not appear in src/."""
    src_dir = REPO_ROOT / "src"
    violations: list[str] = []
    for py_file in sorted(src_dir.rglob("*.py")):
        content = py_file.read_text(encoding="utf-8")
        for name in ("_jcs_encode", "_JCS_ESCAPE_MAP"):
            for lineno, line in enumerate(content.splitlines(), 1):
                if name in line:
                    rel = py_file.relative_to(REPO_ROOT)
                    violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert violations == [], "Pure-Python encoder names found in src/:\n" + "\n".join(violations)
