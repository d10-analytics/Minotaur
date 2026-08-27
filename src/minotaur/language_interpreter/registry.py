"""Language-neutral registry used by source selection and the CLI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from minotaur.language_interpreter.contract import AnalysisResult
from minotaur.language_interpreter.javascript import NAMESPACE as JAVASCRIPT_NAMESPACE
from minotaur.language_interpreter.javascript import analyze_javascript_files
from minotaur.language_interpreter.python import NAMESPACE as PYTHON_NAMESPACE
from minotaur.language_interpreter.python import analyze_python_files
from minotaur.language_interpreter.workspace import Workspace

# Interpreters receive files after shared selection has established containment
# and ordering. This avoids every language implementation repeating the same
# security-sensitive filesystem policy.
AnalyzeFiles = Callable[[Workspace, tuple[Path, ...]], AnalysisResult]


@dataclass(frozen=True, slots=True)
class InterpreterRegistration:
    """A source extension and the interpreter that owns it.

    The registry uses extensions as a deliberately small language-neutral
    boundary. The CLI should not need to know whether a file is Python, or
    later another language, in order to select it safely.
    """

    extension: str
    analyze_files: AnalyzeFiles
    namespace: str = field(kw_only=True)


class InterpreterRegistry:
    """Map normalized source-file extensions to native interpreters.

    Registrations are copied into an immutable-by-convention lookup table at
    construction time. Rejecting duplicate extensions prevents registration
    order from silently deciding which language interprets a file.
    """

    def __init__(self, registrations: tuple[InterpreterRegistration, ...]) -> None:
        by_extension: dict[str, InterpreterRegistration] = {}
        for registration in registrations:
            extension = _normalize_extension(registration.extension)
            if extension in by_extension:
                raise ValueError(f"duplicate interpreter registration for {extension}")
            by_extension[extension] = replace(registration, extension=extension)
        self._by_extension = by_extension

    def registration_for(self, path: Path) -> InterpreterRegistration | None:
        """Return the owner of ``path`` without making unsupported files errors.

        Recursive directory walks need this distinction: unsupported files are
        normal noise during discovery, while an explicitly selected one is a
        user mistake handled by the selection layer.
        """
        return self._by_extension.get(path.suffix.lower())

    def supports(self, path: Path) -> bool:
        return self.registration_for(path) is not None


def default_registry() -> InterpreterRegistry:
    """Return interpreters supported by this Minotaur distribution.

    This is the sole product-level registration point. Adding a language here
    makes it available to the existing ``analyze`` command without adding a
    language switch or a parallel command-line workflow.
    """
    return InterpreterRegistry(
        (
            InterpreterRegistration(
                ".py", analyze_python_files, namespace=PYTHON_NAMESPACE
            ),
            InterpreterRegistration(
                ".js", analyze_javascript_files, namespace=JAVASCRIPT_NAMESPACE
            ),
        )
    )


def _normalize_extension(extension: str) -> str:
    """Make extension matching case-insensitive and reject ambiguous entries."""
    normalized = extension.lower()
    if not normalized.startswith(".") or normalized == ".":
        raise ValueError(f"interpreter extension must start with a non-empty dot: {extension}")
    return normalized
