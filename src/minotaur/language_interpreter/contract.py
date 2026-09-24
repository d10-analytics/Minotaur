"""Public result types for native source-language analysis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location
from minotaur.language_interpreter.call_expressions import CallExpressionObservation

IMPORTS_RESOLVED = "imports_resolved"
IMPORTS_UNRESOLVED = "imports_unresolved"
IMPORTS_ROOT_MISMATCHED = "imports_root_mismatched"
IMPORT_ROOT_HINT = "import_root_hint"


class DiagnosticCode(str, Enum):
    """Conditions that prevent a source file from being fully interpreted."""

    PARSE_ERROR = "parse-error"
    SOURCE_READ_ERROR = "source-read-error"
    UNSUPPORTED_SYNTAX = "unsupported-syntax"
    DUPLICATE_DECLARATION = "duplicate-declaration"
    AMBIGUOUS_REFERENCE = "ambiguous-reference"
    CIRCULAR_DEPENDENCY = "circular-dependency"
    VIEW_DEPTH_WARNING = "view-depth-warning"


class DiagnosticSeverity(str, Enum):
    """The outcome severity carried by one source diagnostic."""

    ERROR = "error"
    WARNING = "warning"


# Keep the shorter spelling available to callers that treat severity as the
# primary part of the contract while retaining the explicit public enum name.
Severity = DiagnosticSeverity


def _freeze_metadata(value: object) -> object:
    """Freeze nested diagnostic metadata without changing its JSON shape."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A source-local diagnostic; analysis continues after one is reported."""

    code: DiagnosticCode
    path: str
    message: str
    location: Location | None = None
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    extensions: Mapping[str, Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, DiagnosticSeverity):
            try:
                object.__setattr__(self, "severity", DiagnosticSeverity(self.severity))
            except (TypeError, ValueError) as error:
                raise ValueError(f"unknown diagnostic severity: {self.severity!r}") from error
        if self.extensions is not None:
            frozen = _freeze_metadata(self.extensions)
            if not isinstance(frozen, Mapping):
                raise ValueError("diagnostic extensions must be a mapping")
            object.__setattr__(self, "extensions", frozen)

    @property
    def is_error(self) -> bool:
        return self.severity is DiagnosticSeverity.ERROR

    @property
    def is_warning(self) -> bool:
        return self.severity is DiagnosticSeverity.WARNING

    @property
    def metadata(self) -> Mapping[str, Mapping[str, object]] | None:
        """Alias for namespaced structured diagnostic extensions."""
        return self.extensions


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """The complete graph and all non-fatal analysis diagnostics."""

    document: GraphDocument
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)
    call_expressions: tuple[CallExpressionObservation, ...] = field(default_factory=tuple)

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.is_error)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.is_warning)
