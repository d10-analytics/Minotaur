"""Shared behavioral checks for immutable call-expression observations."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import FrameType
from typing import Any

import esprima  # type: ignore[import-untyped]
import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.graph_model.serialization import serialize
from minotaur.language_interpreter.call_expressions import (
    CallExpressionObservation,
    _json_text,
    _normalize_javascript,
    javascript_call_fingerprint,
    python_call_fingerprint,
)
from minotaur.language_interpreter.contract import AnalysisResult
from minotaur.language_interpreter.javascript import analyze_javascript_files
from minotaur.language_interpreter.workspace import Workspace


def test_analysis_result_call_observations_are_default_empty_and_immutable() -> None:
    result = AnalysisResult(GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8))

    assert result.call_expressions == ()
    with pytest.raises(FrozenInstanceError):
        result.call_expressions = ()  # type: ignore[misc]


def test_missing_call_evidence_is_not_invented_for_unresolved_javascript_calls(tmp_path) -> None:
    path = tmp_path / "app.js"
    path.write_text('function run() { missing("value"); }\n', encoding="utf-8")

    result = analyze_javascript_files(Workspace(tmp_path), (path,))

    assert result.call_expressions == ()


def test_call_observations_remain_out_of_band_from_graph_serialization(tmp_path) -> None:
    path = tmp_path / "app.js"
    path.write_text(
        "\n".join(
            (
                "function helper(value) { return value; }",
                "function run() { helper(1); }",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = analyze_javascript_files(Workspace(tmp_path), (path,))

    assert result.call_expressions
    assert b"call_expressions" not in serialize(result.document)


def test_unavailable_fingerprint_is_explicit_on_an_observation() -> None:
    observation = CallExpressionObservation(
        language="javascript",
        callee_location=Location("app.js", Range(Position(0, 0), Position(0, 1))),
        expression_location=Location("app.js", Range(Position(0, 0), Position(0, 1))),
        fingerprint=None,
    )

    assert observation.available is False
    assert observation.structural_fingerprint is None
    assert javascript_call_fingerprint(object()) is None


_JAVASCRIPT_SAMPLE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "analyzer_semantics" / "javascript"
)
_CHAIN_TERMS = 1_000
_CALLER_BUDGET = 800
_EXPECTED_VALUE_LIMIT = 20_000


def _frame_depth() -> int:
    depth = 0
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def _stdlib_python_digest(node: ast.Call) -> str:
    options: dict[str, Any] = {"show_empty": True} if sys.version_info >= (3, 13) else {}
    text = ast.dump(node, annotate_fields=True, include_attributes=False, **options)
    return hashlib.sha256(b"python\0" + text.encode()).hexdigest()


def _stdlib_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _stdlib_javascript_digest(structure: Any) -> str:
    return hashlib.sha256(b"javascript\0" + _stdlib_json(structure).encode()).hexdigest()


def _parse_python_call(source: str) -> ast.Call:
    node = ast.parse(source, mode="eval").body
    assert isinstance(node, ast.Call)
    return node


def _parse_javascript_module(source: str) -> Any:
    return esprima.parseModule(source, options={"loc": True, "range": True, "tolerant": True})


def _parse_javascript_call(source: str) -> Any:
    statement = _parse_javascript_module(source).body[0]
    assert statement.type == "ExpressionStatement"
    assert statement.expression.type == "CallExpression"
    return statement.expression


def _javascript_calls(tree: Any) -> Iterator[Any]:
    pending = [tree]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(reversed(value))
            continue
        if not hasattr(value, "__dict__"):
            continue
        if getattr(value, "type", None) == "CallExpression":
            yield value
        pending.extend(
            reversed([child for name, child in vars(value).items() if name not in {"loc", "range"}])
        )


def _chain(terms: int, operator: str = "+") -> str:
    return f" {operator} ".join(["a"] * terms)


@pytest.mark.parametrize(
    "source",
    [
        "f(a, k=1)",
        "f(*items, **options)",
        "f(a, *b, c=1, **d)",
        "f(lambda: 0)",
        "f(lambda x, /, y=1, *rest, z, **extra: x)",
        "f('text', b'bytes', 7, 2.5, 3j, None, ...)",
        "f(u'legacy')",
        "f(f'{value!r:>{width}} tail')",
        "f([item for item in items if item], {key: value for key, value in pairs})",
        "f(values[1:2], values[::3], values[a:b, c])",
        "f(g(h(1), k=i()), j(*m))",
        "obj.method(-1, not a, a if b else c, (yield_value := 2))",
    ],
)
def test_python_call_fingerprint_matches_ast_dump_digest(source: str) -> None:
    node = _parse_python_call(source)

    assert python_call_fingerprint(node) == _stdlib_python_digest(node)


def test_python_call_fingerprint_distinguishes_unicode_prefix_and_literal_type() -> None:
    plain = python_call_fingerprint(_parse_python_call("f('legacy')"))
    prefixed = python_call_fingerprint(_parse_python_call("f(u'legacy')"))
    integer = python_call_fingerprint(_parse_python_call("f(1)"))
    floating = python_call_fingerprint(_parse_python_call("f(1.0)"))

    assert plain != prefixed
    assert integer != floating


class _Opaque:
    __slots__ = ()


def _circular_list() -> list[Any]:
    value: list[Any] = [1]
    value.append(value)
    return value


def _circular_dict() -> dict[str, Any]:
    value: dict[str, Any] = {"a": 1}
    value["self"] = value
    return value


_JSON_CORPUS: list[Any] = [
    {"z": {"b": [1, {"d": [], "c": {}}], "a": None}, "y": [[[]], [{}]], "a": "first"},
    {},
    [],
    {"b": 2, "a": 1, "c": {"z": 0, "m": [3, 2, 1]}},
    "caf\u00e9 \u2603 \U0001f600",
    'quote " backslash \\ newline \n tab \t control \x01 del \x7f',
    "\u2028\u2029",
    "\ud800",
    True,
    False,
    None,
    2**64 + 1,
    -(2**70),
    1.0,
    -0.0,
    1e21,
    1e-7,
    [True, False, None, 0, -1, 0.5, ""],
    ("tuple", ["inside"]),
    {"non-ascii-\u00fc": "value", "\ud800": "surrogate key"},
    {2: "b", 1.5: "a", True: "t"},
    {None: "null key"},
]


@pytest.mark.parametrize("value", _JSON_CORPUS)
def test_json_text_matches_compact_sorted_json_dumps(value: Any) -> None:
    assert _json_text(value) == _stdlib_json(value)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (float("nan"), ValueError),
        ([1, float("inf")], ValueError),
        ({"key": float("-inf")}, ValueError),
        ({(1, 2): "tuple key"}, TypeError),
        ({"a": 1, 2: "mixed key types"}, TypeError),
        ([_Opaque()], TypeError),
        (_circular_list(), ValueError),
        (_circular_dict(), ValueError),
    ],
)
def test_json_text_rejects_what_json_dumps_rejects(value: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        _stdlib_json(value)
    with pytest.raises(error):
        _json_text(value)


def test_json_text_matches_json_dumps_for_every_javascript_sample_call() -> None:
    sample_paths = sorted(_JAVASCRIPT_SAMPLE_ROOT.glob("*.js"))
    calls_by_file: dict[str, int] = {}

    for path in sample_paths:
        tree = _parse_javascript_module(path.read_text(encoding="utf-8"))
        for call in _javascript_calls(tree):
            structure = _normalize_javascript(call)
            assert structure["type"] == "CallExpression"
            assert _json_text(structure) == _stdlib_json(structure)
            assert javascript_call_fingerprint(call) == _stdlib_javascript_digest(structure)
            calls_by_file[path.name] = calls_by_file.get(path.name, 0) + 1

    assert [path.name for path in sample_paths] == [
        "accessors.js",
        "app.js",
        "expressions.js",
        "lib.js",
    ]
    assert calls_by_file == {"app.js": 1, "expressions.js": 8}


def test_javascript_call_normalizes_to_written_structure() -> None:
    call = _parse_javascript_call("f(a + a);")
    expected = {
        "arguments": [
            {
                "left": {"name": "a", "type": "Identifier"},
                "operator": "+",
                "right": {"name": "a", "type": "Identifier"},
                "type": "BinaryExpression",
            }
        ],
        "callee": {"name": "f", "type": "Identifier"},
        "optional": False,
        "type": "CallExpression",
    }

    assert _normalize_javascript(call) == expected
    assert javascript_call_fingerprint(call) == _stdlib_javascript_digest(expected)


@pytest.mark.parametrize(
    ("source", "expected_argument"),
    [
        (
            "f(class {});",
            {
                "body": {"body": [], "type": "ClassBody"},
                "id": None,
                "superClass": None,
                "type": "ClassExpression",
            },
        ),
        (
            "f(`x${a}`);",
            {
                "expressions": [{"name": "a", "type": "Identifier"}],
                "quasis": [
                    {
                        "tail": False,
                        "type": "TemplateElement",
                        "value": {"cooked": "x", "type": "None"},
                    },
                    {
                        "tail": True,
                        "type": "TemplateElement",
                        "value": {"cooked": "", "type": "None"},
                    },
                ],
                "type": "TemplateLiteral",
            },
        ),
        (
            "f(/ab+c/gi);",
            {"regex": {"flags": "gi", "pattern": "ab+c"}, "type": "Literal"},
        ),
        ("f(1e999);", {"type": "Literal", "value": "inf"}),
    ],
)
def test_javascript_call_argument_normalizes_to_written_structure(
    source: str, expected_argument: dict[str, Any]
) -> None:
    call = _parse_javascript_call(source)

    normalized = _normalize_javascript(call)

    assert normalized["arguments"] == [expected_argument]
    assert javascript_call_fingerprint(call) == _stdlib_javascript_digest(normalized)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(float("inf"), "inf"), (float("-inf"), "-inf"), (float("nan"), "nan"), (2.5, 2.5)],
)
def test_javascript_float_normalization_renders_non_finite_values_as_repr(
    value: float, expected: Any
) -> None:
    assert _normalize_javascript(value) == expected


def test_unsupported_javascript_regex_or_value_has_no_fingerprint() -> None:
    unsupported_regex = _parse_javascript_call("f(/ab+c/gi);")
    unsupported_regex.arguments[0].regex.pattern = None
    unsupported_value = _parse_javascript_call("f(a);")
    unsupported_value.arguments[0].name = b"bytes"
    nested_unsupported = _parse_javascript_call("f(g(h(a)));")
    nested_unsupported.arguments[0].arguments[0].arguments.append(_Opaque())

    assert javascript_call_fingerprint(unsupported_regex) is None
    assert javascript_call_fingerprint(unsupported_value) is None
    assert javascript_call_fingerprint(nested_unsupported) is None
    assert javascript_call_fingerprint(_parse_javascript_call("f(g(h(a)));")) is not None


def _expected_value_under_raised_limit(compute: Any) -> Any:
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(_frame_depth() + _EXPECTED_VALUE_LIMIT)
    try:
        return compute()
    finally:
        sys.setrecursionlimit(previous)


def _call_with_caller_budget(function: Any, argument: Any) -> Any:
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(_frame_depth() + _CALLER_BUDGET)
    try:
        return function(argument)
    finally:
        sys.setrecursionlimit(previous)


def test_python_call_fingerprint_handles_long_argument_chain_at_constant_depth() -> None:
    node = _parse_python_call(f"f({_chain(_CHAIN_TERMS)})")
    expected = _expected_value_under_raised_limit(lambda: _stdlib_python_digest(node))

    assert _call_with_caller_budget(python_call_fingerprint, node) == expected


def test_javascript_call_fingerprint_handles_long_argument_chain_at_constant_depth() -> None:
    call = _parse_javascript_call(f"f({_chain(_CHAIN_TERMS)});")
    operand: dict[str, Any] = {"name": "a", "type": "Identifier"}
    for _ in range(_CHAIN_TERMS - 1):
        operand = {
            "left": operand,
            "operator": "+",
            "right": {"name": "a", "type": "Identifier"},
            "type": "BinaryExpression",
        }
    structure = {
        "arguments": [operand],
        "callee": {"name": "f", "type": "Identifier"},
        "optional": False,
        "type": "CallExpression",
    }
    expected = _expected_value_under_raised_limit(lambda: _stdlib_javascript_digest(structure))

    assert _call_with_caller_budget(javascript_call_fingerprint, call) == expected
