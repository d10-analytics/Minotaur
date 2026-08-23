"""Document envelope model for Minotaur graph documents.

A GraphDocument is a single current-snapshot of a software architecture.
It is NOT a history, a diff, or a revision-tracking structure. It captures
what a set of producers found at one point in time: which entities exist,
how they relate, and what evidence supports each claim.

The document envelope carries required format metadata (format name, version,
coordinate encoding) and optional snapshot context (who generated it, when,
from what source-control state). The optional fields are informational only
— they help a reader understand the snapshot's origin but do not participate
in node identity, relationship semantics, or validation logic.

Empty node and relationship arrays are valid. A document with no nodes and
no relationships is a legitimate current snapshot: "we analyzed this codebase
and found nothing." This is distinct from an invalid or incomplete document.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from minotaur.graph_model._parsing import (
    freeze_extensions,
    reject_unknown_fields,
    serialize_extensions,
)
from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.location import Location
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.graph_model.relationship import Relationship

# Module-level constants for reject_unknown_fields (F-13).
_SOURCE_CONTROL_FIELDS = frozenset({"system", "commit", "branch"})
_DOCUMENT_FIELDS = frozenset(
    {
        "format",
        "format_version",
        "coordinate_encoding",
        "generated_by",
        "generated_at",
        "source_control",
        "nodes",
        "relationships",
        "extensions",
    }
)

# Format constants. These are fixed for v1 and must match the JSON Schema's
# `const` values exactly. A document with a different format or version is
# not a v1 Minotaur graph — the reader rejects it rather than guessing.
FORMAT_NAME = "minotaur-graph"
FORMAT_VERSION = "0.1.0"

# Git commit IDs are full-length only: 40 hex chars (SHA-1) or 64 hex chars
# (SHA-256, for future Git object format). Abbreviated commits are invalid
# because they're ambiguous — the same 7-char prefix can resolve to different
# commits in different clones of the same repository.
_GIT_COMMIT_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")

# RFC 3339 timestamp ending in Z (UTC). The schema requires the Z suffix
# rather than a numeric offset to ensure timestamps are directly comparable
# without timezone arithmetic.
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


@dataclass(frozen=True, slots=True)
class SourceControl:
    """Optional source-control context for the snapshot.

    Identifies the Git state when the graph was generated. At least one of
    commit or branch must be present — a source_control object with neither
    is meaningless and the schema rejects it (via anyOf).

    These fields are snapshot context, not identity inputs. They tell a
    reader "this graph was generated from commit abc123 on branch main"
    but do not affect node IDs, relationship semantics, or validation.
    """

    system: str  # Always "git" in v1.
    commit: str | None = None
    branch: str | None = None

    def __post_init__(self) -> None:
        if self.system != "git":
            raise ValueError(f"v1 only supports 'git' source control, got {self.system!r}")

        if self.commit is None and self.branch is None:
            raise ValueError("source_control requires at least one of 'commit' or 'branch'")

        if self.commit is not None and not _GIT_COMMIT_RE.match(self.commit):
            raise ValueError(
                f"git commit must be a full 40 or 64 character lowercase hex "
                f"identifier, got {self.commit!r}"
            )

        if self.branch is not None and not self.branch:
            raise ValueError("branch must be non-empty when present")

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {"system": self.system}
        if self.commit is not None:
            result["commit"] = self.commit
        if self.branch is not None:
            result["branch"] = self.branch
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SourceControl:
        reject_unknown_fields(data, _SOURCE_CONTROL_FIELDS, "source_control")
        system = data.get("system")
        if not isinstance(system, str):
            raise ValueError("source_control requires a 'system' string")

        commit = data.get("commit")
        if commit is not None and not isinstance(commit, str):
            raise ValueError("'commit' must be a string when present")

        branch = data.get("branch")
        if branch is not None and not isinstance(branch, str):
            raise ValueError("'branch' must be a string when present")

        return cls(system=system, commit=commit, branch=branch)


@dataclass(frozen=True, slots=True)
class GraphDocument:
    """A complete Minotaur graph document.

    This is the top-level container that a consumer loads, validates, and
    renders. The document owns its nodes and relationships and provides
    the coordinate encoding that applies to every source range within it.

    Nodes and relationships are stored as tuples (not lists) because the
    document is frozen. The ordering within these tuples is the input order
    from deserialization; canonical ordering is applied by the normalizer,
    not by the model. This separation means the model faithfully represents
    what was in the JSON file, and the normalizer produces the canonical
    form for comparison and serialization.
    """

    coordinate_encoding: CoordinateEncoding

    # Tuples preserve order while maintaining frozen immutability.
    nodes: tuple[Node, ...] = field(default_factory=tuple)
    relationships: tuple[Relationship, ...] = field(default_factory=tuple)

    # Optional snapshot metadata.
    generated_by: Producer | None = None
    generated_at: str | None = None
    source_control: SourceControl | None = None
    extensions: Mapping[str, Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.generated_at is not None:
            if not _RFC3339_UTC_RE.match(self.generated_at):
                raise ValueError(
                    f"generated_at must be an RFC 3339 UTC timestamp ending in 'Z', "
                    f"got {self.generated_at!r}"
                )
            try:
                datetime.fromisoformat(f"{self.generated_at[:-1]}+00:00")
            except ValueError as error:
                raise ValueError(
                    "generated_at must be a valid RFC 3339 UTC timestamp, "
                    f"got {self.generated_at!r}"
                ) from error
        object.__setattr__(self, "extensions", freeze_extensions(self.extensions))

    def node_by_id(self, node_id: str) -> Node | None:
        """Look up a node by its ID, or None if not found.

        Linear scan is acceptable because graph documents are typically
        hundreds to low thousands of nodes. If this becomes a bottleneck,
        the validation module can build an index dict once rather than
        adding index maintenance complexity to the immutable model.
        """
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict.

        Produces the wire format that matches the v1 JSON Schema. The
        format and format_version fields are always included with their
        constant values — they identify the document as a v1 Minotaur graph.
        """
        result: dict[str, object] = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "coordinate_encoding": self.coordinate_encoding.value,
        }

        if self.generated_by is not None:
            result["generated_by"] = self.generated_by.to_dict()
        if self.generated_at is not None:
            result["generated_at"] = self.generated_at
        if self.source_control is not None:
            result["source_control"] = self.source_control.to_dict()

        result["nodes"] = [node.to_dict() for node in self.nodes]
        result["relationships"] = [rel.to_dict() for rel in self.relationships]

        if self.extensions is not None:
            result["extensions"] = serialize_extensions(self.extensions)

        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GraphDocument:
        """Deserialize from a parsed JSON dict.

        Checks the format envelope (format name, version) and constructs
        the full object graph. Does NOT run semantic validation — that
        happens in the validation module after the document is loaded,
        so all errors can be reported together.
        """
        reject_unknown_fields(data, _DOCUMENT_FIELDS, "graph document")

        # Parse-local location memo (D-08, AR-02): one dict per from_dict
        # call, threaded to Node/Relationship → Evidence → Location.
        location_memo: dict[tuple[str, int, int, int, int], Location] = {}
        # Verify the format envelope first. A document with the wrong
        # format name or version is not a v1 Minotaur graph, and we
        # should fail clearly rather than trying to parse it and getting
        # confusing errors from mismatched field expectations.
        fmt = data.get("format")
        if fmt != FORMAT_NAME:
            raise ValueError(f"expected format '{FORMAT_NAME}', got {fmt!r}")

        version = data.get("format_version")
        if version != FORMAT_VERSION:
            raise ValueError(f"expected format_version '{FORMAT_VERSION}', got {version!r}")

        encoding_str = data.get("coordinate_encoding")
        if not isinstance(encoding_str, str):
            raise ValueError("document requires a 'coordinate_encoding' string")
        try:
            coordinate_encoding = CoordinateEncoding(encoding_str)
        except ValueError:
            raise ValueError(
                f"unknown coordinate_encoding '{encoding_str}'; "
                f"v1 values are: {', '.join(e.value for e in CoordinateEncoding)}"
            ) from None

        # Optional metadata.
        generated_by = None
        gen_by_data = data.get("generated_by")
        if gen_by_data is not None:
            if not isinstance(gen_by_data, dict):
                raise ValueError("'generated_by' must be an object when present")
            generated_by = Producer.from_dict(gen_by_data)

        generated_at = data.get("generated_at")
        if generated_at is not None and not isinstance(generated_at, str):
            raise ValueError("'generated_at' must be a string when present")

        source_control = None
        sc_data = data.get("source_control")
        if sc_data is not None:
            if not isinstance(sc_data, dict):
                raise ValueError("'source_control' must be an object when present")
            source_control = SourceControl.from_dict(sc_data)

        # Nodes and relationships.
        nodes_data = data.get("nodes")
        if not isinstance(nodes_data, list):
            raise ValueError("document requires a 'nodes' array")
        nodes = tuple(Node.from_dict(n, memo=location_memo) for n in nodes_data)

        rels_data = data.get("relationships")
        if not isinstance(rels_data, list):
            raise ValueError("document requires a 'relationships' array")
        relationships = tuple(Relationship.from_dict(r, memo=location_memo) for r in rels_data)

        extensions = data.get("extensions")
        if extensions is not None and not isinstance(extensions, dict):
            raise ValueError("'extensions' must be an object when present")

        return cls(
            coordinate_encoding=coordinate_encoding,
            nodes=nodes,
            relationships=relationships,
            generated_by=generated_by,
            generated_at=generated_at,
            source_control=source_control,
            extensions=extensions,
        )
