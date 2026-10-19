"""Immutable source observations for resolved call expressions.

Call-expression observations deliberately live beside, rather than inside,
the graph document.  The graph remains the stable interchange format while
comparisons can use language-aware syntax without treating source formatting,
comments, or positions as semantic changes.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable, Generator
from dataclasses import dataclass
from functools import partial
from math import isfinite
from typing import Any

from minotaur.graph_model.location import Location


@dataclass(frozen=True, slots=True)
class CallExpressionObservation:
    """One resolved call-site expression observed by a language interpreter.

    ``callee_location`` identifies the exact location used by the graph's
    ``calls`` evidence.  ``expression_location`` spans the complete call,
    including its arguments.  A missing fingerprint is explicit evidence that
    the parser could not provide comparable syntax for this occurrence.
    """

    language: str
    callee_location: Location
    expression_location: Location
    fingerprint: str | None


def python_call_fingerprint(node: ast.Call) -> str:
    """Return a deterministic fingerprint for a Python call AST.

    The structure is the text of
    ``ast.dump(node, annotate_fields=True, include_attributes=False)`` with
    empty fields shown: literal values and structural ordering are retained
    while line/column metadata, type comments, and other formatting artifacts
    are excluded.  It is rendered without a Python frame per syntax level, so
    a long operand chain cannot exhaust the interpreter's recursion limit.
    """
    parts: list[str] = []
    _evaluate(node, partial(_python_dump_steps, parts=parts))
    return _digest("python", "".join(parts))


def javascript_call_fingerprint(node: Any) -> str | None:
    """Return a deterministic fingerprint for an ESTree call node.

    ESTree location, range, token, and comment metadata are observational
    rather than semantic and are omitted.  Literal values and regular-
    expression attributes remain in the canonical structure, which is
    serialized as compact, key-sorted JSON.
    """
    try:
        payload = _json_text(_normalize_javascript(node))
    except (TypeError, ValueError, UnicodeError):
        return None
    return _digest("javascript", payload)


def _digest(language: str, structure: str) -> str:
    payload = f"{language}\0{structure}".encode()
    return hashlib.sha256(payload).hexdigest()


_Steps = Generator[Any, Any, Any]


def _evaluate(root: Any, steps: Callable[[Any], _Steps]) -> Any:
    """Run a depth-first structural evaluation with a constant Python frame depth.

    ``steps(value)`` is a generator that yields each child value it needs, in
    the order a recursive implementation would visit them, receives that
    child's result in return, and finally returns its own result.  Suspended
    generators live on an explicit stack, so evaluation order, results, and
    exceptions match the equivalent recursion while nesting depth is bounded
    only by memory.
    """
    stack = [steps(root)]
    result: Any = None
    while True:
        try:
            child = stack[-1].send(result)
        except StopIteration as finished:
            stack.pop()
            result = finished.value
            if not stack:
                return result
        else:
            stack.append(steps(child))
            result = None


def _python_dump_steps(value: Any, parts: list[str]) -> _Steps:
    """Write one value as ``ast.dump`` does without indentation.

    Nodes render as ``Class(field=value, ...)``, skipping fields that are
    missing and ``None`` fields whose class default is ``None``; lists render
    as ``[a, b]`` or ``[]``; every other value renders with ``repr``.
    """
    if isinstance(value, ast.AST):
        cls = type(value)
        parts.append(f"{value.__class__.__name__}(")
        separator = ""
        for name in value._fields:
            try:
                child = getattr(value, name)
            except AttributeError:
                continue
            if child is None and getattr(cls, name, ...) is None:
                continue
            parts.append(f"{separator}{name}=")
            separator = ", "
            yield child
        parts.append(")")
    elif isinstance(value, list):
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(", ")
            yield item
        parts.append("]")
    else:
        parts.append(repr(value))


_NODE_IGNORED_FIELDS = frozenset({"type", "loc", "range", "raw", "tokens", "comments", "errors"})
_OBJECT_IGNORED_FIELDS = frozenset({"loc", "range", "raw", "tokens", "comments", "errors"})


def _normalize_javascript(value: Any) -> Any:
    """Return the JSON-compatible canonical structure of an ESTree value."""
    return _evaluate(value, _javascript_steps)


def _javascript_steps(value: Any) -> _Steps:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            return repr(value)
        return value
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for item in value:
            items.append((yield item))
        return items
    if hasattr(value, "type"):
        if value.type == "Literal" and getattr(value, "regex", None) is not None:
            regex = value.regex
            pattern = getattr(regex, "pattern", None)
            flags = getattr(regex, "flags", None)
            if not isinstance(pattern, str) or not isinstance(flags, str):
                raise TypeError("unsupported JavaScript regex literal")
            return {"type": "Literal", "regex": {"flags": flags, "pattern": pattern}}
        fields: dict[str, Any] = {"type": str(value.type)}
        for name, child in sorted(vars(value).items()):
            if name in _NODE_IGNORED_FIELDS:
                continue
            fields[name] = yield child
        return fields
    if hasattr(value, "__dict__"):
        attributes: dict[str, Any] = {}
        for name, child in sorted(vars(value).items()):
            if name not in _OBJECT_IGNORED_FIELDS:
                attributes[name] = yield child
        return attributes
    raise TypeError(f"unsupported JavaScript AST value: {type(value).__name__}")


def _json_text(value: Any) -> str:
    """Serialize ``value`` exactly as compact, key-sorted ``json.dumps`` would.

    The output, and the ``TypeError``/``ValueError`` raised for values JSON
    cannot represent, match ``json.dumps(value, ensure_ascii=False,
    sort_keys=True, separators=(",", ":"), allow_nan=False)``.  Containers are
    written iteratively; every scalar and key is written through
    ``json.dumps`` itself.
    """
    parts: list[str] = []
    _evaluate(value, partial(_json_steps, parts=parts, open_containers=set()))
    return "".join(parts)


def _json_steps(value: Any, parts: list[str], open_containers: set[int]) -> _Steps:
    if isinstance(value, (list, tuple)):
        if not value:
            parts.append("[]")
            return
        _enter_json_container(value, open_containers)
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            yield item
        parts.append("]")
        open_containers.discard(id(value))
    elif isinstance(value, dict):
        if not value:
            parts.append("{}")
            return
        _enter_json_container(value, open_containers)
        parts.append("{")
        for index, (key, child) in enumerate(sorted(value.items())):
            parts.append(f"{',' if index else ''}{_json_key(key)}:")
            yield child
        parts.append("}")
        open_containers.discard(id(value))
    else:
        parts.append(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _enter_json_container(value: Any, open_containers: set[int]) -> None:
    marker = id(value)
    if marker in open_containers:
        raise ValueError("Circular reference detected")
    open_containers.add(marker)


def _json_key(key: Any) -> str:
    if not isinstance(key, str):
        if key is not None and not isinstance(key, (int, float)):
            raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")
        key = json.dumps(key, allow_nan=False)
    return json.dumps(key, ensure_ascii=False)
