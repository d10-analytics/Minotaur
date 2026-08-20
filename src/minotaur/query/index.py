"""Indexes used by the fixed graph query commands."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import NodeClass
from minotaur.graph_model.relationship import Relationship


@dataclass(frozen=True, slots=True)
class GraphIndex:
    """A single immutable lookup index for one loaded graph snapshot.

    Query handlers receive this object rather than repeatedly scanning the
    document.  Relationship maps retain the complete edge objects because
    call-site locations live on their evidence records.
    """

    nodes: Mapping[str, Node]
    symbols_by_label: Mapping[str, tuple[Node, ...]]
    relationships_by_target: Mapping[tuple[str, str], tuple[Relationship, ...]]
    relationships_by_source: Mapping[tuple[str, str], tuple[Relationship, ...]]
    unresolved_nodes: tuple[Node, ...]

    @classmethod
    def build(cls, document: GraphDocument) -> GraphIndex:
        nodes = {node.id: node for node in document.nodes}
        labels: dict[str, list[Node]] = defaultdict(list)
        unresolved: list[Node] = []
        for node in document.nodes:
            if node.node_class == NodeClass.SYMBOL:
                labels[node.label].append(node)
            elif node.node_class == NodeClass.UNRESOLVED_REFERENCE:
                unresolved.append(node)

        incoming: dict[tuple[str, str], list[Relationship]] = defaultdict(list)
        outgoing: dict[tuple[str, str], list[Relationship]] = defaultdict(list)
        for relationship in document.relationships:
            incoming[(relationship.kind, relationship.target)].append(relationship)
            outgoing[(relationship.kind, relationship.source)].append(relationship)

        return cls(
            nodes=nodes,
            symbols_by_label={label: tuple(items) for label, items in labels.items()},
            relationships_by_target={key: tuple(items) for key, items in incoming.items()},
            relationships_by_source={key: tuple(items) for key, items in outgoing.items()},
            unresolved_nodes=tuple(unresolved),
        )

    def symbols(self) -> tuple[Node, ...]:
        return tuple(node for node in self.nodes.values() if node.node_class == NodeClass.SYMBOL)

    def symbol(self, label: str) -> Node | None:
        matches = self.symbols_by_label.get(label, ())
        return matches[0] if len(matches) == 1 else None

    def labels(self) -> tuple[str, ...]:
        return tuple(sorted(self.symbols_by_label))

    def incoming(self, kind: str, node_id: str) -> tuple[Relationship, ...]:
        return self.relationships_by_target.get((kind, node_id), ())

    def outgoing(self, kind: str, node_id: str) -> tuple[Relationship, ...]:
        return self.relationships_by_source.get((kind, node_id), ())

    def relationships(self, kind: str) -> Iterable[Relationship]:
        return (
            relationship
            for (relationship_kind, _), values in self.relationships_by_target.items()
            if relationship_kind == kind
            for relationship in values
        )
