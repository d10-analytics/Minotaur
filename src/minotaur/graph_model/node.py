"""Node model for Minotaur graph documents.

Nodes represent the entities in a software architecture graph: functions,
classes, files, external resources, and unresolved references. Each node
has a deterministic, opaque ID computed from its identity descriptor and
structural properties.

The node model uses conditional shapes rather than a class hierarchy.
A symbol node requires symbol_kind; a file node requires path; an
unresolved-reference node requires reference_text. This mirrors the
JSON Schema's if/then conditional validation rather than introducing
a Python type hierarchy that doesn't exist in the wire format. The
tradeoff is that some fields are Optional when they're conceptually
required for certain node classes — but construction and from_dict
enforce the conditional requirements, so a well-formed Node always
has the right fields for its class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from minotaur.graph_model._parsing import (
    freeze_extensions,
    reject_unknown_fields,
    reject_unpaired_surrogates,
    serialize_extensions,
    type_error,
)
from minotaur.graph_model.identity import NodeIdentity, is_valid_node_id_format
from minotaur.graph_model.location import Location, is_safe_path
from minotaur.graph_model.provenance import (
    IdentityBasis,
    NodeClass,
    is_valid_language,
    resolve_symbol_kind,
)

# Module-level constant for reject_unknown_fields (F-13).
_NODE_FIELDS = frozenset(
    {
        "id",
        "identity",
        "node_class",
        "label",
        "symbol_kind",
        "language",
        "location",
        "path",
        "reference_text",
        "expected_symbol_kind",
        "extensions",
    }
)

# Which identity bases each node class may use. Resource nodes may carry a
# source location, so they may also be identified by one.
_PERMITTED_BASES: dict[NodeClass, tuple[IdentityBasis, ...]] = {
    NodeClass.SYMBOL: (IdentityBasis.SOURCE_LOCATION, IdentityBasis.UPSTREAM_IDENTIFIER),
    NodeClass.FILE: (IdentityBasis.FILE_PATH,),
    NodeClass.RESOURCE: (
        IdentityBasis.RESOURCE_KEY,
        IdentityBasis.UPSTREAM_IDENTIFIER,
        IdentityBasis.SOURCE_LOCATION,
    ),
    NodeClass.UNRESOLVED_REFERENCE: (IdentityBasis.UNRESOLVED_REFERENCE,),
}


@dataclass(frozen=True, slots=True)
class Node:
    """A single entity in the architecture graph.

    The node's `id` is a precomputed "node:sha256:<digest>" string. It is
    NOT recomputed at construction time — construction trusts the caller's
    ID. Verification that the ID matches the identity descriptor is the
    semantic validator's job, not the model's, because:

      1. Verification requires the full identity reconstruction logic,
         which depends on the node class and basis. Putting that in the
         constructor would duplicate the validator.
      2. During deserialization we want to load a node even if its ID is
         wrong, so the validator can report the mismatch with full context
         (expected vs. actual) rather than failing silently at load time.
    """

    id: str
    identity: NodeIdentity
    node_class: NodeClass
    label: str

    # Conditional fields — presence depends on node_class.
    # symbol_kind stores the raw string (core or namespaced extension)
    # rather than the SymbolKind enum so that extension values pass
    # through without conversion. The resolve_symbol_kind function
    # validates the value at construction.
    symbol_kind: str | None = None
    language: str | None = None
    location: Location | None = None
    path: str | None = None
    reference_text: str | None = None
    expected_symbol_kind: str | None = None
    extensions: Mapping[str, Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        # Field types are checked before anything reads them (R-02): from_dict
        # type-checks the wire, but in-process construction and
        # dataclasses.replace() reach __post_init__ directly, and an untyped
        # value here reaches the serializer and compute_node_id unchallenged.
        if not isinstance(self.id, str):
            raise type_error("node id", self.id, "a string")
        if not isinstance(self.identity, NodeIdentity):
            raise type_error("node identity", self.identity, "a NodeIdentity")
        if not isinstance(self.node_class, NodeClass):
            raise type_error("node_class", self.node_class, "a NodeClass")
        if not isinstance(self.label, str):
            raise type_error("node label", self.label, "a string")
        if self.symbol_kind is not None and not isinstance(self.symbol_kind, str):
            raise type_error("'symbol_kind'", self.symbol_kind, "a string when present")
        if self.language is not None and not isinstance(self.language, str):
            raise type_error("language", self.language, "a string when present")
        if self.location is not None and not isinstance(self.location, Location):
            raise type_error("'location'", self.location, "a Location when present")
        if self.path is not None and not isinstance(self.path, str):
            raise type_error("node path", self.path, "a string when present")
        if self.reference_text is not None and not isinstance(self.reference_text, str):
            raise type_error("'reference_text'", self.reference_text, "a string when present")
        if self.expected_symbol_kind is not None and not isinstance(self.expected_symbol_kind, str):
            raise type_error(
                "'expected_symbol_kind'", self.expected_symbol_kind, "a string when present"
            )

        # Wire-format check only — not digest verification.
        if not is_valid_node_id_format(self.id):
            raise ValueError(f"node id must match 'node:sha256:<64 hex chars>', got {self.id!r}")

        if not self.label:
            raise ValueError("node label must be non-empty")

        # Enforce conditional field requirements per node_class.
        # These match the JSON Schema's allOf/if/then rules exactly.
        if self.node_class == NodeClass.SYMBOL and self.symbol_kind is None:
            raise ValueError("symbol nodes require 'symbol_kind'")
        # Validate that symbol_kind, whenever present, is either a core value
        # or a valid namespaced extension — on every node class, as the schema
        # does. Catch invalid values early rather than letting them through
        # to filtering/display logic.
        if self.symbol_kind is not None:
            resolve_symbol_kind(self.symbol_kind)

        if self.node_class == NodeClass.FILE and self.path is None:
            raise ValueError("file nodes require 'path'")
        # Node paths use the same safe-path contract as location paths, on
        # every node class. An unsafe path is just as dangerous on a resource
        # or symbol node as in a Location — it could reference files outside
        # the repository, and downstream consumers rely on never re-checking.
        if self.path is not None and not is_safe_path(self.path):
            raise ValueError(
                f"node path must be a safe repository-relative path, got {self.path!r}"
            )
        # symbol_kind and path feed the identity input; keep them JCS-encodable.
        reject_unpaired_surrogates(self.label, "node label")
        if self.symbol_kind is not None:
            reject_unpaired_surrogates(self.symbol_kind, "'symbol_kind'")
        if self.path is not None:
            reject_unpaired_surrogates(self.path, "node path")

        if self.node_class == NodeClass.UNRESOLVED_REFERENCE and self.reference_text is None:
            raise ValueError("unresolved-reference nodes require a non-empty 'reference_text'")
        # Like symbol_kind and path, a present reference_text is checked on
        # every node class, as the schema's unconditional nonEmptyString does.
        if self.reference_text is not None:
            if not self.reference_text:
                raise ValueError("'reference_text' must be non-empty when present")
            reject_unpaired_surrogates(self.reference_text, "'reference_text'")

        # The identity basis must be one that makes sense for this node class.
        # A file node whose ID claims to be derived from an upstream identifier,
        # or a symbol identified by a resource key, is a contradiction about
        # how the ID was formed. This mirrors the schema's node_class → basis
        # conditionals; the digest itself is verified by the semantic validator.
        permitted = _PERMITTED_BASES[self.node_class]
        if self.identity.basis not in permitted:
            raise ValueError(
                f"{self.node_class.value} nodes do not permit identity basis "
                f"'{self.identity.basis.value}'; permitted: "
                f"{', '.join(b.value for b in permitted)}"
            )
        # A source-location identity is derived from the node's location, so
        # the location must be present for the ID to be reconstructible.
        if self.identity.basis == IdentityBasis.SOURCE_LOCATION and self.location is None:
            raise ValueError("source-location identity basis requires a node 'location'")

        # Language validation when present.
        if self.language is not None and not is_valid_language(self.language):
            raise ValueError(f"language must match '^[a-z][a-z0-9-]*$', got {self.language!r}")

        # Validate expected_symbol_kind the same way as symbol_kind.
        if self.expected_symbol_kind is not None:
            resolve_symbol_kind(self.expected_symbol_kind)
        object.__setattr__(self, "extensions", freeze_extensions(self.extensions))

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict.

        Only includes fields that are present — absent optional fields
        are omitted entirely rather than serialized as null. This matches
        the schema contract where optional fields are simply not present
        in the JSON object, and avoids ambiguity between "field is null"
        and "field is absent."
        """
        result: dict[str, object] = {
            "id": self.id,
            "identity": self.identity.to_dict(),
            "node_class": self.node_class.value,
            "label": self.label,
        }

        if self.symbol_kind is not None:
            result["symbol_kind"] = self.symbol_kind
        if self.language is not None:
            result["language"] = self.language
        if self.location is not None:
            result["location"] = self.location.to_dict()
        if self.path is not None:
            result["path"] = self.path
        if self.reference_text is not None:
            result["reference_text"] = self.reference_text
        if self.expected_symbol_kind is not None:
            result["expected_symbol_kind"] = self.expected_symbol_kind
        if self.extensions is not None:
            result["extensions"] = serialize_extensions(self.extensions)

        return result

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        *,
        memo: dict[tuple[str, int, int, int, int], Location] | None = None,
    ) -> Node:
        """Deserialize from a parsed JSON dict.

        Performs structural validation (types, required fields) but not
        semantic validation (ID digest verification, endpoint integrity).
        A Node returned from from_dict is structurally well-formed but
        not necessarily semantically valid — the validator checks that.
        """
        reject_unknown_fields(data, _NODE_FIELDS, "node")
        node_id = data.get("id")
        if not isinstance(node_id, str):
            raise ValueError("node requires an 'id' string")

        identity_data = data.get("identity")
        if not isinstance(identity_data, dict):
            raise ValueError("node requires an 'identity' object")
        identity = NodeIdentity.from_dict(identity_data)

        node_class_str = data.get("node_class")
        if not isinstance(node_class_str, str):
            raise ValueError("node requires a 'node_class' string")
        try:
            node_class = NodeClass(node_class_str)
        except ValueError:
            raise ValueError(
                f"unknown node_class '{node_class_str}'; v1 classes are: "
                f"{', '.join(c.value for c in NodeClass)}"
            ) from None

        label = data.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError("node requires a non-empty 'label' string")

        # Optional fields — extract with type checking but allow absence.
        symbol_kind = _optional_str(data, "symbol_kind")
        language = _optional_str(data, "language")
        path = _optional_str(data, "path")
        reference_text = _optional_str(data, "reference_text")
        expected_symbol_kind = _optional_str(data, "expected_symbol_kind")

        location = None
        location_data = data.get("location")
        if location_data is not None:
            if not isinstance(location_data, dict):
                raise ValueError("'location' must be an object when present")
            location = Location.from_dict(location_data, memo=memo)

        extensions = data.get("extensions")
        if extensions is not None and not isinstance(extensions, dict):
            raise ValueError("'extensions' must be an object when present")

        return cls(
            id=node_id,
            identity=identity,
            node_class=node_class,
            label=label,
            symbol_kind=symbol_kind,
            language=language,
            location=location,
            path=path,
            reference_text=reference_text,
            expected_symbol_kind=expected_symbol_kind,
            extensions=extensions,
        )


def _optional_str(data: dict[str, object], key: str) -> str | None:
    """Extract an optional string field, raising on wrong type."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string when present, got {type(value).__name__}")
    return value
