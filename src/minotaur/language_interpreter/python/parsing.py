"""Python AST parsing."""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedPython:
    """A successfully parsed source module."""

    tree: ast.Module


def parse_python(source: str, path: str) -> ParsedPython:
    """Parse a source file without executing or importing it."""
    return ParsedPython(tree=ast.parse(source, filename=path))
