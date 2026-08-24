"""Behavioral coverage for canonical normalization and JCS extensions."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

import minotaur.graph_model.serialization as serialization_module
from minotaur.graph_model._parsing import _jcs_serialize
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.serialization import canonicalize, serialize

EXAMPLES = Path(__file__).parents[1] / "examples/synthetic-graphs"
REPO_ROOT = Path(__file__).parents[1]


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
# AC-08 (c): the oracle is pinned to the Baseline bytes
# ---------------------------------------------------------------------------
#
# The oracle only proves the C-encoder composition correct while it stays the
# code that composition replaced.  Anyone who "fixes" the oracle to agree with
# the implementation has deleted the proof, so the oracle's source text is
# pinned to its Baseline bytes and the pin fails before the equality tests do.

_ORACLE_SEGMENT_START = "_JCS_ESCAPE_MAP: dict[str, str] = {"
_ORACLE_SEGMENT_END = '    return "".join(parts).encode("utf-8")\n'

# SHA-256 of the source text from _ORACLE_SEGMENT_START through
# _ORACLE_SEGMENT_END inclusive, as it stands at Baseline commit fb63689 —
# verified byte-identical to the segment in this file (`git diff fb63689 --
# tests/test_graph_model_serialization.py` produces no hunk inside it).  To
# recompute after a deliberate retirement of the oracle — never to make a
# failing pin pass — write the baseline file out and hash the same slice:
#
#   git show fb63689:tests/test_graph_model_serialization.py > /tmp/base.py
#   python -c "import hashlib, pathlib; \
#       import test_graph_model_serialization as m; \
#       t = pathlib.Path('/tmp/base.py').read_text(); \
#       s = t.index(m._ORACLE_SEGMENT_START); \
#       e = t.index(m._ORACLE_SEGMENT_END, s) + len(m._ORACLE_SEGMENT_END); \
#       print(hashlib.sha256(t[s:e].encode()).hexdigest())"
#
_ORACLE_SEGMENT_SHA256 = "e4726121c36c8c0267e6ced6912dadb3bf343c068cb61493370add75eff1bb88"


def _oracle_segment() -> str:
    """Return this module's oracle source text: escape map through serializer."""
    text = Path(__file__).read_text(encoding="utf-8")
    start = text.index(_ORACLE_SEGMENT_START)
    end = text.index(_ORACLE_SEGMENT_END, start) + len(_ORACLE_SEGMENT_END)
    return text[start:end]


def test_oracle_source_is_byte_unchanged_from_baseline() -> None:
    """The oracle bodies still hash to their Baseline (fb63689) bytes."""
    segment = _oracle_segment()
    # Guard the extraction itself: a truncated slice would hash to something
    # stable but prove nothing.
    assert "def _jcs_encode(" in segment
    assert "def _oracle_serialize(" in segment
    assert hashlib.sha256(segment.encode("utf-8")).hexdigest() == _ORACLE_SEGMENT_SHA256, (
        "the JCS oracle was edited; it is the Baseline encoder and is never "
        "changed to agree with the implementation — if the implementation "
        "disagrees with it, the implementation is wrong"
    )


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


def test_jcs_nested_structure() -> None:
    value = {"b": [True, None, {"z": 1, "a": 2}], "a": "hello"}
    result = _jcs_serialize(value)
    assert result == b'{"a":"hello","b":[true,null,{"a":2,"z":1}]}'


# ---------------------------------------------------------------------------
# AC-10 (a): Oracle equality over every graph fixture
# ---------------------------------------------------------------------------


def _collect_graph_fixtures() -> list[Path]:
    """Return all nine raw JSON graph fixtures under tests/ and examples/."""
    paths: list[Path] = []
    for directory in [REPO_ROOT / "tests", REPO_ROOT / "examples"]:
        paths.extend(sorted(directory.rglob("*.json")))
    return paths


# These collectors intentionally represent three different admission
# boundaries. The raw set feeds the independent JSON encoder and schema
# sweeps, so it includes every syntactically valid JSON fixture even when a
# model or semantic check rejects that fixture. The model set feeds
# ``GraphDocument.from_dict`` and the in-process serializer; it includes the
# dangling-relationship fixture because model construction does not validate
# relationship endpoints. The strict-load set feeds ``load_graph_bytes`` and
# therefore contains only the four example graphs: strict loading validates
# relationship endpoints and must exclude that dangling fixture. Keeping the
# sets separate preserves both model-constructible coverage and strict-load
# coverage without making either consumer claim the wrong admission boundary.


def _collect_model_constructible_graph_fixtures() -> list[Path]:
    """Return the four examples plus the model-constructible dangling graph."""
    dangling = REPO_ROOT / "tests/fixtures/minotaur-graph-v1/invalid/dangling-relationship.json"
    examples = sorted((REPO_ROOT / "examples").rglob("*.json"))
    return examples + [dangling]


def _collect_loadable_graph_fixtures() -> list[Path]:
    """Return the four example graphs accepted by strict graph loading."""
    return sorted((REPO_ROOT / "examples").rglob("*.json"))


@pytest.mark.parametrize(
    "fixture_path",
    _collect_model_constructible_graph_fixtures(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_canonicalize_uses_jcs_code_point_key_order(fixture_path: Path) -> None:
    source = json.loads(fixture_path.read_text(encoding="utf-8"))
    document = GraphDocument.from_dict(source)
    canonical = canonicalize(document)
    encoded_without_sorting = json.dumps(
        canonical,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    assert encoded_without_sorting == _oracle_serialize(canonical)


@pytest.mark.parametrize(
    "fixture_path",
    _collect_model_constructible_graph_fixtures(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_serialize_bypasses_eager_key_sort(
    fixture_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serialization retains bytes without the public dict-key normalization."""
    source = json.loads(fixture_path.read_text(encoding="utf-8"))
    doc = GraphDocument.from_dict(source)
    expected = _oracle_serialize(canonicalize(doc))

    def fail_eager_key_sort(value: object) -> object:
        _ = value
        raise AssertionError("serialize invoked the eager key sort")

    monkeypatch.setattr(serialization_module, "_sort_keys_code_point", fail_eager_key_sort)

    assert serialize(doc) == expected
    with pytest.raises(AssertionError, match="eager key sort"):
        canonicalize(doc)


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


# Non-BMP object keys are absent by construction: R-02 forbids them and the
# model layer rejects them on every path, so the shapes that used to live here
# are rejection cases in test_graph_model_core.py's AC-06 matrix, not encoder
# equality cases. Astral characters in string *values* stay covered below.
_EDGE_CASES: list[tuple[str, object]] = [
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
    # Astral characters in string values remain unrestricted by R-02, over the
    # same nested container shapes the encoder recurses through.
    ("astral_value_nested_dict", {"k": {"z": "\U0001d11e", "a": 2}}),
    ("astral_value_nested_list", {"k": [{"z": "\U0001d11e", "a": 2}]}),
    ("astral_value_nested_tuple", {"k": ({"z": "\U0001d11e", "a": 2},)}),
    (
        "astral_value_deep_mixed_tree",
        {
            "k": [
                {"z": 1, "e": ({"q": "\U0001f600", "b": [{"n": 1, "m": 2}]},)},
                ({"y": 3, "a": {"w": 4, "v": "\U0001d11e"}},),
            ],
            "b": ({"z": 6, "a": 7},),
        },
    ),
]


@pytest.mark.parametrize("name,value", _EDGE_CASES, ids=[c[0] for c in _EDGE_CASES])
def test_oracle_equality_edge_cases(name: str, value: object) -> None:
    """Implementation and oracle agree on every edge case."""
    _ = name
    assert _jcs_serialize(value) == _oracle_serialize(value)


# ---------------------------------------------------------------------------
# AC-08 (d): C encoder rejects non-finite floats but accepts finite floats
# ---------------------------------------------------------------------------


def test_jcs_non_finite_float_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        _jcs_serialize({"a": float("nan")})


def test_float_raises_oracle_top_level() -> None:
    with pytest.raises(TypeError, match="float"):
        _oracle_serialize(3.14)


def test_jcs_finite_float_is_encoded() -> None:
    assert _jcs_serialize({"a": 1.5}) == b'{"a":1.5}'


def test_float_raises_oracle_nested_dict() -> None:
    with pytest.raises(TypeError, match="float"):
        _oracle_serialize({"a": 1.5})


def test_float_raises_oracle_nested_list() -> None:
    with pytest.raises(TypeError, match="float"):
        _oracle_serialize([1, 2.0, 3])


# ---------------------------------------------------------------------------
# AC-11: No production import of pure-Python encoder
# ---------------------------------------------------------------------------


def test_no_production_jcs_encode_or_escape_map() -> None:
    """Removed pure-Python JCS machinery and prechecks must not return to src/."""
    src_dir = REPO_ROOT / "src"
    violations: list[str] = []
    for py_file in sorted(src_dir.rglob("*.py")):
        content = py_file.read_text(encoding="utf-8")
        for name in (
            "_jcs_encode",
            "_JCS_ESCAPE_MAP",
            "_check_canonical_input",
            "_sort_keys_recursive",
            "_utf16_sort_key",
        ):
            for lineno, line in enumerate(content.splitlines(), 1):
                if name in line:
                    rel = py_file.relative_to(REPO_ROOT)
                    violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert violations == [], "Pure-Python encoder names found in src/:\n" + "\n".join(violations)
