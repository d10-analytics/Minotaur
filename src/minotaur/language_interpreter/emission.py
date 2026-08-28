"""Shared node construction for language interpreters."""

from __future__ import annotations

from collections.abc import Mapping

from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    IdentityBasis,
    NodeClass,
    RelationshipKind,
    SymbolKind,
)
from minotaur.language_interpreter.accumulation import RelationshipAccumulator


def symbol_node(
    label: str,
    kind: SymbolKind,
    location: Location,
    namespace: str,
    language: str,
    extensions: Mapping[str, Mapping[str, object]] | None = None,
) -> Node:
    """Construct a source-location-backed symbol node."""
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, namespace)
    return Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind=kind.value,
            location=location,
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=kind.value,
        language=language,
        location=location,
        extensions=extensions,
    )


class NodeEmitter:
    """Construct language-specific unresolved-reference nodes and edges."""

    def __init__(self, namespace: str, language: str) -> None:
        self.namespace = namespace
        self.language = language
        self._seen: set[str] = set()

    def unresolved(
        self,
        origin: str,
        text: str,
        location: Location,
        nodes: list[Node],
        accumulator: RelationshipAccumulator,
    ) -> str:
        identity = NodeIdentity(
            IdentityBasis.UNRESOLVED_REFERENCE,
            self.namespace,
            originating_node=origin,
        )
        node_id = compute_node_id(
            identity,
            node_class=NodeClass.UNRESOLVED_REFERENCE.value,
            location=location,
            reference_text=text,
        )
        if node_id not in self._seen:
            self._seen.add(node_id)
            nodes.append(
                Node(
                    id=node_id,
                    identity=identity,
                    node_class=NodeClass.UNRESOLVED_REFERENCE,
                    label=text,
                    reference_text=text,
                    language=self.language,
                    location=location,
                )
            )
        accumulator.add(origin, node_id, RelationshipKind.REFERENCES.value, location)
        return node_id
