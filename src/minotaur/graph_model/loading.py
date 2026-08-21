"""One strict boundary for loading a v1 graph into its canonical form."""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.serialization import canonicalize
from minotaur.graph_model.validation import ValidationReport, validate_document


class GraphLoadError(ValueError):
    """Raised when an input is not a structurally and semantically valid graph."""


@dataclass(frozen=True)
class LoadedGraph:
    """The model and deterministic wire representation produced by the boundary.

    ``canonical`` is a cached property so the query path never pays the
    canonicalization cost.  Only ``visualize`` accesses it, triggering the
    computation on first read.  Dropping ``slots=True`` lets
    ``functools.cached_property`` write through ``__dict__``, which
    ``frozen=True`` does not block.
    """

    document: GraphDocument

    @functools.cached_property
    def canonical(self) -> dict[str, object]:
        return canonicalize(self.document)


# Keep this checker local to the input boundary. The model intentionally does
# not validate wire-format spellings while it is being constructed; accepting
# unchecked input anywhere else would make different consumers disagree about
# what constitutes one canonical graph.
_FORMAT_CHECKER = jsonschema.FormatChecker()


@_FORMAT_CHECKER.checks("date-time")  # type: ignore[untyped-decorator]
def _is_utc_rfc3339(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    return True


def schema() -> dict[str, object]:
    """Load the sole packaged v1 schema resource.

    The public ``schemas/`` path is a symlink to this resource. Reading the
    installed package here keeps the CLI, library callers, and wheel contents
    tied to one schema rather than allowing a checkout-only copy to drift.
    """
    text = files("minotaur.graph_model").joinpath("schemas/v1.json").read_text(encoding="utf-8")
    loaded = json.loads(text)
    if not isinstance(loaded, dict):  # pragma: no cover - a checked-in resource
        raise RuntimeError("the packaged v1 schema must be an object")
    return loaded


def load_graph_bytes(content: bytes) -> LoadedGraph:
    """Parse, schema-check, model-load, and semantically validate bytes.

    Canonicalization is deferred to ``LoadedGraph.canonical`` (a cached
    property) so callers that never read it — such as the query path —
    avoid the cost entirely.  Every validation stage still runs eagerly:
    malformed or dangling graph facts are rejected before any downstream
    consumer can access the loaded result.
    """
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GraphLoadError(f"graph input is not valid UTF-8: {error}") from None
    try:
        raw: Any = json.loads(decoded)
    except json.JSONDecodeError as error:
        raise GraphLoadError(f"graph input is not valid JSON: {error.msg}") from None
    if not isinstance(raw, dict):
        raise GraphLoadError("graph input must contain a JSON object")
    try:
        # Schema validation protects wire shape; model construction and the
        # semantic report then protect cross-record invariants such as endpoint
        # identity. Keeping both checks here gives every presentation path the
        # same admission policy.
        jsonschema.Draft202012Validator(schema(), format_checker=_FORMAT_CHECKER).validate(raw)
        document = GraphDocument.from_dict(raw)
    except (jsonschema.ValidationError, ValueError) as error:
        raise GraphLoadError(str(error)) from None
    report: ValidationReport = validate_document(document)
    if not report.is_valid:
        details = "; ".join(f"{issue.json_pointer}: {issue.message}" for issue in report)
        raise GraphLoadError(f"graph semantic validation failed: {details}")
    return LoadedGraph(document=document)


def load_graph_file(path: Path) -> LoadedGraph:
    """Read a graph file before passing it through the shared strict boundary."""
    try:
        return load_graph_bytes(path.read_bytes())
    except OSError as error:
        message = f"could not read graph input {path}: {error.strerror or error}"
        raise GraphLoadError(message) from None
