"""Controlled vocabularies for Minotaur graph version 1.

This module defines the closed core vocabularies that the v1 schema requires.
These are deliberately small, fixed sets — not extensible enumerations. When
a value does not have a faithful core equivalent, the producer uses a
namespaced extension value (e.g. "rust:trait") rather than adding to these
enums. This keeps filtering and interchange reliable across tools without
requiring a schema revision for every specialized case.

The vocabularies are grouped here rather than scattered across the model
modules they relate to because a single producer or validator needs to check
all of them, and they share the same extension-value policy.
"""

from __future__ import annotations

import re
from enum import Enum

from minotaur.graph_model._parsing import reject_unpaired_surrogates

# Namespaced extension values use the pattern `namespace:local-name`, where
# the namespace is a lowercase dotted identifier (like a reversed domain)
# and the local name is a lowercase kebab identifier. This pattern matches
# the JSON Schema `symbolKind` and `relationshipKind` extension patterns
# exactly, so Python-side validation stays consistent with schema validation.
_NAMESPACED_EXTENSION_RE = re.compile(r"^[a-z][a-z0-9.-]*:[a-z][a-z0-9-]*$")

# Language identifiers are normalized lowercase ASCII. The pattern is the
# same as the JSON Schema `language` definition: starts with a letter,
# then lowercase alphanumerics and hyphens. Examples: "python", "csharp",
# "type-script". This format avoids case-sensitivity issues and maps
# cleanly to file extensions and language-server identifiers.
_LANGUAGE_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def is_namespaced_extension(value: str) -> bool:
    """Check whether a string is a valid namespaced extension value.

    Extension values allow producers to express language-specific or
    tool-specific concepts without polluting the core vocabulary. The
    namespace prefix makes the origin attributable and prevents
    collisions between independent producers.
    """
    return _NAMESPACED_EXTENSION_RE.match(value) is not None


def is_valid_language(value: str) -> bool:
    """Check whether a string is a valid v1 language identifier."""
    return _LANGUAGE_RE.match(value) is not None


class CoordinateEncoding(Enum):
    """The character-offset encoding used by every source range in the document.

    This is a document-level declaration, not per-location, because mixing
    encodings within a single graph would make position comparison undefined.
    A consumer that receives a utf-16 graph and has utf-8 source text must
    convert positions before range validation; it cannot assume the encoding
    matches its local text.

    utf-16 is the LSP default. utf-8 is the natural encoding for Python
    and most modern tools. utf-32 gives codepoint-level positions. The
    choice belongs to the producer; Minotaur preserves it unchanged.
    """

    UTF_8 = "utf-8"
    UTF_16 = "utf-16"
    UTF_32 = "utf-32"


class NodeClass(Enum):
    """Broad classification of what a node represents.

    Node class determines which additional fields are required:
      - symbol: requires symbol_kind (the fine-grained code entity type)
      - file: requires path (the canonical repository-relative file path)
      - resource: no additional required fields (uses extensions for subtypes)
      - unresolved_reference: requires reference_text (the raw unresolved name)

    These four classes cover the space of things that appear in an architecture
    graph: named code entities, files, external resources, and references that
    could not be resolved. The split between class and kind avoids forcing
    non-code nodes (files, resources) through a symbol_kind taxonomy that
    doesn't describe them.
    """

    SYMBOL = "symbol"
    FILE = "file"
    RESOURCE = "resource"
    UNRESOLVED_REFERENCE = "unresolved-reference"


class SymbolKind(Enum):
    """Fine-grained code entity type for symbol nodes.

    These values follow established language-server and code-index conventions
    (LSP SymbolKind, ctags, tree-sitter node types) rather than inventing a
    Minotaur-specific taxonomy. This means consumers can map Minotaur symbols
    to editor features (go-to-definition, outline views) without a translation
    layer.

    The list includes entity types that appear across multiple languages.
    Language-specific concepts without a faithful core equivalent (e.g.
    Rust traits, Kotlin objects, SQL tables) use namespaced extensions.
    """

    PACKAGE = "package"
    MODULE = "module"
    NAMESPACE = "namespace"
    CLASS = "class"
    INTERFACE = "interface"
    PROTOCOL = "protocol"
    STRUCT = "struct"
    ENUM = "enum"
    ENUM_MEMBER = "enum-member"
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    PROPERTY = "property"
    FIELD = "field"
    VARIABLE = "variable"
    CONSTANT = "constant"
    PARAMETER = "parameter"
    TYPE_PARAMETER = "type-parameter"
    MACRO = "macro"


class RelationshipKind(Enum):
    """Language-neutral structural relationship type.

    These are deliberately semantic and directional:
      - contains: container → member (a module contains a function)
      - imports: importer → imported target
      - references: referring node → referenced target
      - calls: caller → established callable target
      - inherits: subtype → supertype
      - implements: implementation → interface/protocol/contract

    The list omits vague relationships like "depends-on" because they don't
    tell a consumer what actually happens between two nodes. Every core kind
    has a clear direction and a verifiable meaning at the source level.

    Language-qualified relationships (e.g. "python:decorates") use the
    namespaced extension path and are reserved for meanings that cannot
    faithfully use a core kind.
    """

    CONTAINS = "contains"
    IMPORTS = "imports"
    REFERENCES = "references"
    CALLS = "calls"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"


class Provenance(Enum):
    """How an evidence record was established.

    Each value identifies the category of process that produced the evidence,
    not the specific tool (that's the producer field). The categories are:

      - static_analysis: established by a Minotaur language interpreter
        analyzing source code without executing it.
      - imported_graph: normalized from an external graph or index while
        retaining the upstream producer's attribution.
      - curated_rule: added by a declared, inspectable rule rather than
        direct source resolution. Requires a rule identifier so the basis
        is auditable.

    "runtime-observation" and "human-assertion" are deliberately excluded
    from v1 because they each require their own evidence contracts (how do
    you verify a runtime observation? what makes a human assertion
    authoritative?) that v1 does not define.
    """

    STATIC_ANALYSIS = "static-analysis"
    IMPORTED_GRAPH = "imported-graph"
    CURATED_RULE = "curated-rule"


class IdentityBasis(Enum):
    """How a node's canonical identity is determined.

    The basis selects which fields go into the canonical identity input
    that gets JCS-serialized and SHA-256-hashed to produce the node ID.
    Each basis uses different fields because different node types have
    different stable identity anchors:

      - source_location: for nodes defined at a specific code position.
        Identity = (namespace, node_class, symbol_kind, path, range).
      - file_path: for file nodes whose identity IS their path.
        Identity = (namespace, path).
      - upstream_identifier: for nodes imported from an external system
        whose identity comes from that system's ID.
        Identity = (namespace, upstream_identifier).
      - unresolved_reference: for references that couldn't be resolved.
        Identity = (namespace, originating_node, reference_text, location?).
      - resource_key: for external resources identified by a producer key.
        Identity = (namespace, resource_key).
    """

    SOURCE_LOCATION = "source-location"
    FILE_PATH = "file-path"
    UPSTREAM_IDENTIFIER = "upstream-identifier"
    UNRESOLVED_REFERENCE = "unresolved-reference"
    RESOURCE_KEY = "resource-key"


# Reserved language identifiers for v1. These are not an enum because
# the language field accepts any value matching the language pattern —
# new languages don't require a code change. The reserved set exists
# so that documentation and tests can reference canonical spellings
# without hardcoding strings.
RESERVED_LANGUAGES: frozenset[str] = frozenset(
    {
        "python",
        "csharp",
        "javascript",
        "typescript",
        "sql",
    }
)


def resolve_symbol_kind(value: str) -> SymbolKind | str:
    """Resolve a symbol_kind string to its core enum or return it as an extension.

    Returns the SymbolKind enum member for core values, or the raw string
    for valid namespaced extensions. Raises ValueError for values that are
    neither core nor valid extensions — those are schema violations, not
    extension values.
    """
    reject_unpaired_surrogates(value, "symbol_kind")
    try:
        return SymbolKind(value)
    except ValueError:
        pass
    if is_namespaced_extension(value):
        return value
    raise ValueError(
        f"'{value}' is not a core symbol_kind or a valid namespaced extension "
        f"(expected 'namespace:local-name' pattern)"
    )


def resolve_relationship_kind(value: str) -> RelationshipKind | str:
    """Resolve a relationship kind string to its core enum or return it as an extension."""
    reject_unpaired_surrogates(value, "relationship kind")
    try:
        return RelationshipKind(value)
    except ValueError:
        pass
    if is_namespaced_extension(value):
        return value
    raise ValueError(
        f"'{value}' is not a core relationship kind or a valid namespaced extension "
        f"(expected 'namespace:local-name' pattern)"
    )
