"""Shared synthetic comparison fixtures for the visualizer test suites.

The builders produce payloads through the production side-record shapes so a
fixture cannot inject a field production never writes. They import no browser
dependency, so the browser-free comparison tests in ``test_visualizer.py`` can
reuse exactly the fixtures the Chromium tests in ``test_visualizer_browser.py``
build, without pulling Playwright into either module.
"""

from __future__ import annotations

from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.location import Location
from minotaur.graph_model.node import Node
from minotaur.graph_model.relationship import Relationship
from minotaur.language_interpreter.emission import symbol_node
from minotaur.query.graph_comparison import (
    GraphNodeChange,
    GraphRelationshipChange,
    endpoint_eligibility,
)


def _comparison_location(path: str, line: int, length: int = 4) -> dict[str, object]:
    return {
        "path": path,
        "range": {
            "start": {"line": line, "character": 0},
            "end": {"line": line, "character": length},
        },
    }


def _comparison_source_node(node_id: str) -> Node:
    return symbol_node(
        node_id,
        "function",
        Location.from_dict(_comparison_location(f"{node_id}.py", 0)),
        "minotaur-browser-test",
        "python",
    )


def _comparison_node(
    node_id: str,
    *,
    status: str = "unchanged",
    label: str | None = None,
    before: bool = True,
    after: bool = True,
    path: str | None = None,
    line: int = 0,
    before_system: str | None = "A",
    after_system: str | None = "A",
) -> GraphNodeChange:
    """Build one comparison node through the production side-record shape.

    Each side is ``{node, system}`` exactly as the comparator emits it, so a
    fixture cannot inject a per-side ``systems`` field that production never
    writes.
    """

    def side(system: str | None) -> dict[str, object]:
        node = _comparison_source_node(node_id).to_dict()
        node["label"] = label or node_id
        node["location"] = _comparison_location(path or f"{node_id}.py", line)
        return {
            "node": node,
            "system": system,
        }

    reasons = [status] if status != "unchanged" else []
    involved = sorted({name for name in (before_system, after_system) if name})
    return GraphNodeChange(
        node_id,
        status,
        tuple(reasons),
        tuple(involved),
        side(before_system) if before else None,
        side(after_system) if after else None,
    )


def _comparison_edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    status: str = "unchanged",
    kind: str = "calls",
    before: bool = True,
    after: bool = True,
    before_systems: tuple[str | None, str | None] = ("A", "A"),
    after_systems: tuple[str | None, str | None] = ("A", "A"),
    before_evidence: list[list[dict[str, object]]] | None = None,
    after_evidence: list[list[dict[str, object]]] | None = None,
) -> GraphRelationshipChange:
    def side(occurrences: list[list[dict[str, object]]] | None) -> object:
        if occurrences is None:
            occurrences = [
                [
                    {
                        "provenance": "static-analysis",
                        "locations": [_comparison_location(f"{source}.py", 0)],
                    }
                ]
            ]
        payloads = []
        for evidence in occurrences:
            payload = Relationship(
                _comparison_source_node(source).id,
                _comparison_source_node(target).id,
                kind,
                tuple(Evidence.from_dict(record) for record in evidence),
            ).to_dict()
            payload["evidence"] = sorted(payload["evidence"], key=repr)
            payloads.append(payload)
        return payloads[0] if len(payloads) == 1 else tuple(sorted(payloads, key=repr))

    involved = sorted({name for pair in (before_systems, after_systems) for name in pair if name})
    return GraphRelationshipChange(
        edge_id,
        source,
        target,
        kind,
        status,
        tuple([status] if status != "unchanged" else []),
        tuple(involved),
        side(before_evidence) if before else None,
        side(after_evidence) if after else None,
        endpoint_eligibility(
            (before_systems,) if before else (),
            (after_systems,) if after else (),
        ),
    )
