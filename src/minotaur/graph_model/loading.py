"""One strict boundary for loading a v1 graph into its canonical form."""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import orjson

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

    ``validated`` is ``True`` when this load ran both the JSON-schema pass and
    node-ID verification. A matching sidecar authorizes the trusted fast path,
    which skips both checks while retaining the remaining semantic checks.
    ``digest`` is the SHA-256 hex digest of the exact bytes that were parsed.
    Both are load provenance, not graph content.
    """

    document: GraphDocument
    validated: bool
    digest: str

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


def stamp_path(graph: Path) -> Path:
    """Return the sidecar digest path for *graph*."""
    return graph.with_name(graph.name + ".sha256")


def graph_digest(data: bytes) -> str:
    """Return the SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


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


def _validate_wire_shape(raw: dict[str, object]) -> None:
    """Run the JSON-schema pass against the packaged v1 schema."""
    jsonschema.Draft202012Validator(schema(), format_checker=_FORMAT_CHECKER).validate(raw)


def load_graph_bytes(
    content: bytes,
    *,
    _skip_schema: bool = False,
    _digest: str | None = None,
) -> LoadedGraph:
    """Parse, schema-check, model-load, and semantically validate bytes.

    Canonicalization is deferred to ``LoadedGraph.canonical`` (a cached
    property) so callers that never read it — such as the query path —
    avoid the cost entirely.  Every validation stage still runs eagerly:
    malformed or dangling graph facts are rejected before any downstream
    consumer can access the loaded result.

    Private parameters are for ``load_graph_file``'s trusted-load path
    and must not be used by external callers.
    """
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GraphLoadError(f"graph input is not valid UTF-8: {error}") from None
    try:
        # ``orjson`` is the only decoder (R-05).  It rejects NaN/Infinity, lone
        # surrogate escapes and nesting beyond 1024 levels outright; an integer
        # literal wider than 64 bits it decodes as a float instead of raising.
        # That float is still rejected, one layer down and with that layer's
        # message: every v1 integer position is a model field guarded by
        # ``_require_int``/``Position.__post_init__``, and every other v1
        # number lives in an extension, where ``_freeze_json`` rejects any
        # float leaf.  No whole-document walk is needed here, and adding one
        # would cost more than the decoder itself.
        raw: Any = orjson.loads(decoded)
    except orjson.JSONDecodeError as error:
        raise GraphLoadError(f"graph input is not valid JSON: {error.msg}") from None
    if not isinstance(raw, dict):
        raise GraphLoadError("graph input must contain a JSON object")
    validated = not _skip_schema
    try:
        # Schema validation protects wire shape; model construction and the
        # semantic report then protect cross-record invariants such as endpoint
        # identity. Keeping both checks here gives every presentation path the
        # same admission policy.
        if validated:
            _validate_wire_shape(raw)
        document = GraphDocument.from_dict(raw)
    except (jsonschema.ValidationError, ValueError) as error:
        raise GraphLoadError(str(error)) from None
    except RecursionError:
        # Both the schema validator and the extension freeze recurse once per
        # nesting level and reach the interpreter limit before orjson's own
        # 1024-level guard would.  Surface that as the load boundary's error
        # rather than a raw traceback (see ``docs/formats/minotaur-graph-v1.md``).
        raise GraphLoadError(
            "graph input nests deeper than the loader supports (extension objects)"
        ) from None
    # A matching sidecar vouches for these exact bytes having already passed
    # schema and node-ID validation. Skip only those redundant checks on the
    # trusted path; endpoint, duplicate, identity-origin, location, and
    # evidence validation must still run for every load.
    report: ValidationReport = validate_document(document, verify_node_ids=not _skip_schema)
    if not report.is_valid:
        details = "; ".join(f"{issue.json_pointer}: {issue.message}" for issue in report)
        raise GraphLoadError(f"graph semantic validation failed: {details}")
    digest = _digest if _digest is not None else graph_digest(content)
    return LoadedGraph(document=document, validated=validated, digest=digest)


def load_graph_file(path: Path, *, validate: bool = False) -> LoadedGraph:
    """Read a graph file before passing it through the shared strict boundary.

    When *validate* is ``False`` (the default) and a sidecar at
    ``stamp_path(path)`` matches the SHA-256 of the graph bytes, the
    JSON-schema pass is skipped.  Every other sidecar state falls back to
    full validation.  The function never writes the sidecar.
    """
    try:
        data = path.read_bytes()
    except OSError as error:
        message = f"could not read graph input {path}: {error.strerror or error}"
        raise GraphLoadError(message) from None

    digest = graph_digest(data)

    skip_schema = False
    if not validate:
        try:
            with stamp_path(path).open("rb") as fh:
                stamp = fh.read(4096).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            stamp = ""
        skip_schema = stamp == digest

    return load_graph_bytes(data, _skip_schema=skip_schema, _digest=digest)


def load_graph_blob(
    content: bytes,
    sidecar: bytes | None = None,
    *,
    validate: bool = False,
) -> LoadedGraph:
    """Load graph and optional sidecar bytes without touching the filesystem.

    A sidecar is trusted only when its stripped UTF-8 text exactly equals the
    SHA-256 digest of *content*. Missing, malformed, or mismatched sidecars
    take the full validation path. This entry is intentionally read-only and
    never stamps either input.
    """
    digest = graph_digest(content)
    trusted = False
    if not validate and sidecar is not None:
        try:
            trusted = sidecar.decode("utf-8").strip() == digest
        except UnicodeDecodeError:
            trusted = False
    return load_graph_bytes(content, _skip_schema=trusted, _digest=digest)
