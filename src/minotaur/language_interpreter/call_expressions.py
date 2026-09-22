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
from dataclasses import dataclass
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

    @property
    def callee(self) -> Location:
        """Compatibility alias for the callee source location."""
        return self.callee_location

    @property
    def expression(self) -> Location:
        """Compatibility alias for the full expression source location."""
        return self.expression_location

    @property
    def structural_fingerprint(self) -> str | None:
        """Compatibility alias for the normalized syntax fingerprint."""
        return self.fingerprint

    @property
    def available(self) -> bool:
        """Whether comparable syntax was available for this occurrence."""
        return self.fingerprint is not None


def python_call_fingerprint(node: ast.Call) -> str:
    """Return a deterministic fingerprint for a Python call AST.

    ``ast.dump`` without attributes retains literal values and structural
    ordering while excluding line/column metadata, type comments, and other
    formatting artifacts.
    """
    return _digest("python", ast.dump(node, annotate_fields=True, include_attributes=False))


def javascript_call_fingerprint(node: Any) -> str | None:
    """Return a deterministic fingerprint for an ESTree call node.

    ESTree location, range, token, and comment metadata are observational
    rather than semantic and are omitted.  Literal values and regular-
    expression attributes remain in the canonical structure.
    """
    try:
        normalized = _normalize_javascript(node)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        return None
    return _digest("javascript", payload)


def _digest(language: str, structure: str) -> str:
    payload = f"{language}\0{structure}".encode()
    return hashlib.sha256(payload).hexdigest()


def _normalize_javascript(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            return repr(value)
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_javascript(item) for item in value]
    if hasattr(value, "type"):
        fields: dict[str, Any] = {"type": str(value.type)}
        for name, child in sorted(vars(value).items()):
            if name in {"type", "loc", "range", "raw", "tokens", "comments", "errors"}:
                continue
            fields[name] = _normalize_javascript(child)
        return fields
    if hasattr(value, "__dict__"):
        return {
            name: _normalize_javascript(child)
            for name, child in sorted(vars(value).items())
            if name not in {"loc", "range", "raw", "tokens", "comments", "errors"}
        }
    raise TypeError(f"unsupported JavaScript AST value: {type(value).__name__}")
