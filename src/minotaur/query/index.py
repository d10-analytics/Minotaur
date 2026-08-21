"""Indexes used by the fixed graph query commands."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import NodeClass
from minotaur.graph_model.relationship import Relationship


class SymbolResolutionError(ValueError):
    """A queried name does not identify exactly one symbol node.

    Subclasses ``ValueError`` on purpose: the CLI already maps that to exit
    status 2 for every other query input error, so a failed resolution can
    never be rendered as an empty-but-successful result.
    """


class UnknownSymbol(SymbolResolutionError):
    """No symbol in the graph carries the queried label."""

    def __init__(self, label: str, suggestions: Sequence[str] = ()) -> None:
        self.label = label
        self.suggestions = tuple(suggestions)
        message = f"unknown symbol: {label}"
        if self.suggestions:
            message = f"{message}; nearest labels: {', '.join(self.suggestions)}"
        super().__init__(message)


class AmbiguousSymbol(SymbolResolutionError):
    """Several symbols share the queried label, so no single answer exists.

    A function defined twice in one module (conditional definitions, a
    ``TYPE_CHECKING`` branch, an accidental redefinition) produces two symbol
    nodes with the same label.  Answering for an arbitrary one of them would
    hand an agent a confidently wrong caller or impact set, so the candidate
    definition sites are reported instead and the caller must disambiguate.
    """

    def __init__(self, label: str, candidates: Sequence[str]) -> None:
        self.label = label
        self.candidates = tuple(candidates)
        super().__init__(f"ambiguous symbol: {label}; candidates: {', '.join(self.candidates)}")


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

    def resolve(self, label: str) -> Node:
        """Return the one symbol node labelled ``label`` or raise.

        This is the single resolution point for every symbol-taking query, so
        the "unknown" and "ambiguous" outcomes are decided once instead of
        being re-derived (and disagreed about) by each caller.
        """
        matches = self.symbols_by_label.get(label, ())
        if not matches:
            # The default cutoff (0.6) is intentional: a lower cutoff, or 0.0,
            # always returns ``n`` labels regardless of similarity, so an
            # unrelated name like ``nope`` would suggest an unrelated label
            # like ``pkg`` as if it were a plausible near-miss. Suggestions
            # should only appear when they are plausible; ``UnknownSymbol``
            # already omits the "nearest labels" clause when none qualify.
            raise UnknownSymbol(label, get_close_matches(label, self.labels(), n=5))
        if len(matches) > 1:
            ordered = sorted(matches, key=_candidate_sort_key)
            raise AmbiguousSymbol(label, tuple(_candidate(node) for node in ordered))
        return matches[0]

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


def _candidate_sort_key(node: Node) -> tuple[str, int]:
    """Order candidates by file then line -- numerically, not by text.

    Sorting the formatted ``path:line`` strings would put line 10 before
    line 2, so the ordering is computed from the location itself.
    """
    location = node.location
    if location is None:
        return (node.path or node.label, -1)
    return (location.path, location.range.start.line)


def _candidate(node: Node) -> str:
    """Format one ambiguous definition site as ``path:line`` (1-based).

    Line numbers match every other query renderer.  A symbol node without a
    location still has to appear in the candidate list, so it degrades to its
    file path and finally to its label rather than being silently dropped.
    """
    location = node.location
    if location is not None:
        return f"{location.path}:{location.range.start.line + 1}"
    return node.path or node.label
