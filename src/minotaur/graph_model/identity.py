"""Node identity construction and verification for Minotaur graph documents.

Node IDs in Minotaur are deterministic: given the same identity inputs, any
implementation must produce the same ID. This is achieved by:

  1. Constructing a canonical identity input (a Python dict with specific
     fields determined by the identity basis).
  2. Serializing it with RFC 8785 JSON Canonicalization Scheme (JCS).
  3. Computing the SHA-256 digest of that serialization.
  4. Formatting the result as "node:sha256:<hex-digest>".

JCS was chosen over ad-hoc string concatenation because it gives independent
language implementations (Python, JavaScript, C#) a published standard to
implement against, rather than a homegrown escaping/ordering convention that
each reimplementation would risk getting subtly wrong. Since our identity
inputs contain only strings, integers, and nested objects (no floats), JCS
reduces to "sort object keys, serialize strings with minimal escaping,
serialize integers without leading zeros."

The SHA-256 digest makes no security or signature claim — it exists solely
to produce compact, collision-resistant, opaque identifiers from potentially
long identity inputs (which include full file paths and source ranges).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from minotaur.graph_model._parsing import (
    _jcs_serialize,
    reject_unknown_fields,
    reject_unpaired_surrogates,
)
from minotaur.graph_model.location import Location
from minotaur.graph_model.provenance import IdentityBasis

# Module-level constant for reject_unknown_fields (F-13).
_IDENTITY_FIELDS = frozenset(
    {"basis", "namespace", "upstream_identifier", "originating_node", "resource_key"}
)

# Compiled once: validates the wire format of node IDs without checking
# the digest's correctness (that requires reconstructing the identity input).
NODE_ID_RE = re.compile(r"^node:sha256:[a-f0-9]{64}$")


def is_valid_node_id_format(node_id: str) -> bool:
    """Check whether a string has the correct node ID wire format."""
    return NODE_ID_RE.match(node_id) is not None


# The conditional identity fields each basis is permitted (and, where listed,
# required) to carry. Any other conditional field is rejected at construction.
_BASIS_FIELDS: dict[IdentityBasis, frozenset[str]] = {
    IdentityBasis.SOURCE_LOCATION: frozenset(),
    IdentityBasis.FILE_PATH: frozenset(),
    IdentityBasis.UPSTREAM_IDENTIFIER: frozenset({"upstream_identifier"}),
    IdentityBasis.UNRESOLVED_REFERENCE: frozenset({"originating_node"}),
    IdentityBasis.RESOURCE_KEY: frozenset({"resource_key"}),
}


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    """The auditable identity descriptor that accompanies every node.

    Every node carries an identity object that makes its opaque SHA-256
    digest reproducible and inspectable. A consumer or validator can
    reconstruct the canonical identity input from this descriptor plus
    the node's own fields, apply JCS + SHA-256, and verify that the
    declared node ID matches.

    The basis field selects which additional fields participate in the
    identity input. Each basis exists because different kinds of nodes
    have different natural identity anchors:

      - source-location nodes are identified by WHERE they are defined
        (path + range + kind), so moving a function changes its identity.
      - file-path nodes ARE their path — the file's identity is its location
        in the repository.
      - upstream-identifier nodes get their identity from an external system's
        ID — Minotaur cannot invent a better one.
      - unresolved-reference nodes are identified by what couldn't be resolved
        and where the reference appeared.
      - resource-key nodes have a producer-defined key for external resources
        that don't map to source locations.
    """

    basis: IdentityBasis
    namespace: str

    # Conditional fields — which are present depends on the basis.
    # Using None rather than separate subclasses because the wire format
    # is a single object with conditional fields, and the JSON Schema
    # uses if/then rather than oneOf. Subclasses would create a type
    # hierarchy that doesn't exist in the schema.
    upstream_identifier: str | None = None
    originating_node: str | None = None
    resource_key: str | None = None

    def __post_init__(self) -> None:
        if not self.namespace:
            raise ValueError("identity namespace must be non-empty")

        # Enforce the conditional requirements from the schema's allOf/if/then.
        # These are structural requirements, not semantic checks — a missing
        # upstream_identifier on an upstream-identifier basis is like a missing
        # required field, not a logical inconsistency to find later.
        #
        # _BASIS_FIELDS is the single source of truth: a basis REQUIRES every
        # conditional field it lists and FORBIDS every one it does not. An
        # identity that carries, say, a resource_key under a source-location
        # basis is not forward-compatible metadata to be ignored: it is a
        # contradiction about how the node ID was derived. This mirrors the
        # schema's per-basis `required` and `properties: {<field>: false}`.
        allowed = _BASIS_FIELDS[self.basis]
        for field_name in ("upstream_identifier", "originating_node", "resource_key"):
            value = getattr(self, field_name)
            if field_name in allowed:
                if not value:
                    raise ValueError(
                        f"{self.basis.value} basis requires a non-empty '{field_name}'"
                    )
                reject_unpaired_surrogates(value, f"identity '{field_name}'")
            elif value is not None:
                raise ValueError(f"{self.basis.value} basis does not permit '{field_name}'")
        # The schema types originating_node as a node ID; mirror that so a
        # malformed origin is a wire error, not a later semantic finding.
        if self.originating_node is not None and not is_valid_node_id_format(self.originating_node):
            raise ValueError(
                "'originating_node' must match 'node:sha256:<64 hex chars>', "
                f"got {self.originating_node!r}"
            )
        reject_unpaired_surrogates(self.namespace, "identity namespace")

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {
            "basis": self.basis.value,
            "namespace": self.namespace,
        }
        if self.upstream_identifier is not None:
            result["upstream_identifier"] = self.upstream_identifier
        if self.originating_node is not None:
            result["originating_node"] = self.originating_node
        if self.resource_key is not None:
            result["resource_key"] = self.resource_key
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> NodeIdentity:
        reject_unknown_fields(data, _IDENTITY_FIELDS, "identity")
        basis_str = data.get("basis")
        if not isinstance(basis_str, str):
            raise ValueError("identity requires a 'basis' string")
        try:
            basis = IdentityBasis(basis_str)
        except ValueError:
            raise ValueError(
                f"unknown identity basis '{basis_str}'; v1 bases are: "
                f"{', '.join(b.value for b in IdentityBasis)}"
            ) from None

        namespace = data.get("namespace")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("identity requires a non-empty 'namespace' string")

        upstream_identifier = data.get("upstream_identifier")
        if upstream_identifier is not None and not isinstance(upstream_identifier, str):
            raise ValueError("'upstream_identifier' must be a string when present")

        originating_node = data.get("originating_node")
        if originating_node is not None and not isinstance(originating_node, str):
            raise ValueError("'originating_node' must be a string when present")

        resource_key = data.get("resource_key")
        if resource_key is not None and not isinstance(resource_key, str):
            raise ValueError("'resource_key' must be a string when present")

        return cls(
            basis=basis,
            namespace=namespace,
            upstream_identifier=upstream_identifier,
            originating_node=originating_node,
            resource_key=resource_key,
        )


def compute_node_id(
    identity: NodeIdentity,
    *,
    node_class: str,
    symbol_kind: str | None = None,
    path: str | None = None,
    location: Location | None = None,
    reference_text: str | None = None,
) -> str:
    """Compute the deterministic node ID from identity inputs.

    The canonical identity input is a JSON object whose keys depend on
    the identity basis. This function constructs that object, serializes
    it with JCS, hashes with SHA-256, and returns the formatted node ID.

    Parameters come from both the identity descriptor and the node's own
    fields because the identity input includes node-level data (like
    node_class and symbol_kind) that isn't stored in the identity object
    itself. This avoids duplicating node fields into the identity descriptor
    just for hashing.
    """
    canonical_input = _build_canonical_input(
        identity,
        node_class=node_class,
        symbol_kind=symbol_kind,
        path=path,
        location=location,
        reference_text=reference_text,
    )
    jcs_bytes = _jcs_serialize(canonical_input)
    digest = hashlib.sha256(jcs_bytes).hexdigest()
    return f"node:sha256:{digest}"


def verify_node_id(
    declared_id: str,
    identity: NodeIdentity,
    *,
    node_class: str,
    symbol_kind: str | None = None,
    path: str | None = None,
    location: Location | None = None,
    reference_text: str | None = None,
) -> bool:
    """Verify that a declared node ID matches its recomputed value.

    Returns True if the declared ID exactly matches the recomputed ID.
    The semantic validator uses this to reject nodes whose IDs were
    tampered with, miscalculated, or copied from a different node.
    """
    expected = compute_node_id(
        identity,
        node_class=node_class,
        symbol_kind=symbol_kind,
        path=path,
        location=location,
        reference_text=reference_text,
    )
    return declared_id == expected


def _build_canonical_input(
    identity: NodeIdentity,
    *,
    node_class: str,
    symbol_kind: str | None,
    path: str | None,
    location: Location | None,
    reference_text: str | None,
) -> dict[str, object]:
    """Build the canonical identity input object for a given basis.

    Each basis selects a different set of fields. The field names in the
    canonical input match the schema field names exactly — no renaming
    or restructuring — because an independent implementation needs to
    build the exact same JSON object to get the same hash.
    """
    basis = identity.basis

    if basis == IdentityBasis.SOURCE_LOCATION:
        # Source-location identity includes the node's structural position
        # in the codebase: what kind of thing it is and exactly where it
        # sits. This means renaming a function (without moving it) keeps
        # the same ID, but moving it to a new file creates a new one.
        if location is None:
            raise ValueError("source-location basis requires a location")
        result: dict[str, object] = {
            "basis": basis.value,
            "namespace": identity.namespace,
            "node_class": node_class,
            "path": location.path,
            "range": location.range.to_dict(),
        }
        if symbol_kind is not None:
            result["symbol_kind"] = symbol_kind
        return result

    if basis == IdentityBasis.FILE_PATH:
        # File nodes are identified solely by their repository-relative path.
        # No range is included because the file IS the entity — it doesn't
        # have a position within itself.
        if path is None:
            raise ValueError("file-path basis requires a path")
        return {
            "basis": basis.value,
            "namespace": identity.namespace,
            "path": path,
        }

    if basis == IdentityBasis.UPSTREAM_IDENTIFIER:
        # Imported nodes carry their upstream system's ID. Minotaur cannot
        # invent a better identity for something defined in an external tool.
        if identity.upstream_identifier is None:
            raise ValueError("upstream-identifier basis requires upstream_identifier")
        return {
            "basis": basis.value,
            "namespace": identity.namespace,
            "upstream_identifier": identity.upstream_identifier,
        }

    if basis == IdentityBasis.UNRESOLVED_REFERENCE:
        # Unresolved references are identified by what was referenced and
        # where the reference appeared. The originating_node ties the
        # unresolved reference back to the code that tried to use it.
        if identity.originating_node is None:
            raise ValueError("unresolved-reference basis requires originating_node")
        if reference_text is None:
            raise ValueError("unresolved-reference basis requires reference_text")
        result = {
            "basis": basis.value,
            "namespace": identity.namespace,
            "originating_node": identity.originating_node,
            "reference_text": reference_text,
        }
        # Location is optional for unresolved references — the reference
        # might come from an import that couldn't be resolved, where the
        # location is known, or from a dynamic context where it isn't.
        if location is not None:
            result["location"] = location.to_dict()
        return result

    if basis == IdentityBasis.RESOURCE_KEY:
        # Resource nodes have a producer-defined key for things that aren't
        # source code (databases, APIs, config files, external services).
        if identity.resource_key is None:
            raise ValueError("resource-key basis requires resource_key")
        return {
            "basis": basis.value,
            "namespace": identity.namespace,
            "resource_key": identity.resource_key,
        }

    raise ValueError(f"unhandled identity basis: {basis}")
