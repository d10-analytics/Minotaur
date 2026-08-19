"""Python AST parsing with source-preserving diagnostics."""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedPython:
    """A successfully parsed source module and its original text."""

    tree: ast.Module
    source: str


def parse_python(source: str, path: str) -> ParsedPython:
    """Parse a source file without executing or importing it."""
    return ParsedPython(tree=ast.parse(source, filename=path, type_comments=True), source=source)
