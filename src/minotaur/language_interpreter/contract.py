"""Public result types for native source-language analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location

IMPORTS_RESOLVED = "imports_resolved"
IMPORTS_UNRESOLVED = "imports_unresolved"
IMPORTS_ROOT_MISMATCHED = "imports_root_mismatched"
IMPORT_ROOT_HINT = "import_root_hint"


class DiagnosticCode(str, Enum):
    """Conditions that prevent a source file from being fully interpreted."""

    PARSE_ERROR = "parse-error"
    SOURCE_READ_ERROR = "source-read-error"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A source-local diagnostic; analysis continues after one is reported."""

    code: DiagnosticCode
    path: str
    message: str
    location: Location | None = None


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """The complete graph and all non-fatal analysis diagnostics."""

    document: GraphDocument
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)
