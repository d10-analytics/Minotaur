"""Fixtures shared by the CLI and query test suites."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

from minotaur.language_interpreter.javascript import interpreter as javascript_interpreter
from minotaur.language_interpreter.python import interpreter as python_interpreter
from minotaur.language_interpreter.sql import interpreter as sql_interpreter


@dataclass(frozen=True)
class _Seam:
    """One real per-file pass and how to read the file it is working on."""

    module: ModuleType
    name: str
    path_of: Callable[[Mapping[str, Any]], str]
    paths_in_attempt: Callable[[Mapping[str, Any]], frozenset[str]] | None = None


_SEAMS = {
    "python": _Seam(
        python_interpreter,
        "_analyze_module",
        lambda arguments: arguments["module"].path,
        lambda arguments: frozenset(module.path for module in arguments["modules"].values()),
    ),
    "javascript": _Seam(
        javascript_interpreter,
        "_expressions",
        lambda arguments: arguments["module"].path,
    ),
    "sql": _Seam(sql_interpreter, "_scan_batches", lambda arguments: arguments["path"]),
}


@dataclass(frozen=True)
class _Fault:
    error: BaseException
    alongside: str | None


class InterpreterFault:
    """Make a real interpreter pass raise while it works on one named file.

    The wrapper calls the original pass for every other file and never counts
    calls, so a file fails identically however many times it is analyzed and
    whichever other files share the attempt.
    """

    def __init__(self) -> None:
        self._patch = pytest.MonkeyPatch()
        self._faults: dict[str, dict[str, _Fault]] = {}

    def install(
        self,
        language: str,
        path: str,
        error: BaseException,
        *,
        alongside: str | None = None,
    ) -> None:
        """Raise ``error`` whenever ``language``'s pass reaches root-relative ``path``.

        With ``alongside``, the fault fires only when that file is part of the
        same analysis attempt, so ``path`` still analyzes cleanly on its own.
        """
        seam = _SEAMS[language]
        if alongside is not None and seam.paths_in_attempt is None:
            raise ValueError(f"the {language} seam cannot see the other files in an attempt")
        faults = self._faults.get(language)
        if faults is None:
            faults = self._faults[language] = {}
            self._wrap(seam, faults)
        faults[path] = _Fault(error, alongside)

    def undo(self) -> None:
        """Restore every original pass and forget the installed faults."""
        self._patch.undo()
        self._faults.clear()

    def _wrap(self, seam: _Seam, faults: dict[str, _Fault]) -> None:
        original = getattr(seam.module, seam.name)
        signature = inspect.signature(original)

        def faulty(*args: Any, **kwargs: Any) -> Any:
            arguments = signature.bind(*args, **kwargs).arguments
            fault = faults.get(seam.path_of(arguments))
            if fault is not None and (
                fault.alongside is None
                or (
                    seam.paths_in_attempt is not None
                    and fault.alongside in seam.paths_in_attempt(arguments)
                )
            ):
                raise fault.error
            return original(*args, **kwargs)

        self._patch.setattr(seam.module, seam.name, faulty)


@pytest.fixture
def interpreter_fault() -> Iterator[InterpreterFault]:
    """Inject a durable per-file fault into one real interpreter pass."""
    fault = InterpreterFault()
    yield fault
    fault.undo()
