"""Byte-exact comparison and report oracles from a deterministic synthetic graph."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import CoordinateEncoding, IdentityBasis, NodeClass, Provenance
from minotaur.graph_model.relationship import Relationship
from minotaur.language_interpreter.call_expressions import CallExpressionObservation
from minotaur.query import diff as plain_diff
from minotaur.query import system_diff_view
from minotaur.query.render import dump_json
from minotaur.query.system import ReportingSnapshot
from minotaur.query.system_diff import compare_systems
from minotaur.system import load_systems_data

GOLDENS = Path(__file__).resolve().parents[1] / "fixtures" / "comparison_parity"


def _location(path: str, line: int) -> Location:
    return Location(path, Range(Position(line, 0), Position(line, 5)))


def _node(
    label: str,
    path: str | None,
    line: int = 0,
    *,
    basis: IdentityBasis = IdentityBasis.SOURCE_LOCATION,
    node_class: NodeClass = NodeClass.SYMBOL,
    origin: Node | None = None,
) -> Node:
    identity = NodeIdentity(
        basis,
        "parity",
        upstream_identifier="upstream" if basis is IdentityBasis.UPSTREAM_IDENTIFIER else None,
        resource_key="database" if basis is IdentityBasis.RESOURCE_KEY else None,
        originating_node=origin.id if origin else None,
    )
    location = _location(path, line) if path and node_class is not NodeClass.FILE else None
    fields = dict(
        node_class=node_class.value,
        symbol_kind="function" if node_class is NodeClass.SYMBOL else None,
        location=location,
        path=path if node_class is NodeClass.FILE else None,
        reference_text=label if node_class is NodeClass.UNRESOLVED_REFERENCE else None,
    )
    return Node(
        id=compute_node_id(identity, **fields),
        identity=identity,
        node_class=node_class,
        label=label,
        symbol_kind=fields["symbol_kind"],
        location=location,
        path=fields["path"],
        reference_text=fields["reference_text"],
    )


def build_side(new: bool) -> tuple[ReportingSnapshot, tuple[CallExpressionObservation, ...]]:
    """Build six strict-loaded systems and varied graph/call transitions without I/O."""
    rng = random.Random(81473)
    names = ["A", "B", "C", "D", "E", "Added" if new else "Removed"]
    definitions = {}
    for name in names:
        files = [f"{name.lower()}.py"]
        if name == ("B" if new else "A"):
            files.append("moved.py")
        if name == "E":
            files.append("absent.py")
        definitions[Path("systems") / name / "system.toml"] = {
            "schema_version": 1,
            "name": name,
            "files": files,
        }
    systems = load_systems_data(definitions)
    nodes = {name: _node(name, f"{name.lower()}.py", rng.randrange(1, 8)) for name in names}
    nodes["moved"] = _node("moved", "moved.py")
    nodes["loose"] = _node("loose", "unassigned.py")
    nodes["upstream"] = _node(
        "remote-new" if new else "remote-old", None, basis=IdentityBasis.UPSTREAM_IDENTIFIER
    )
    nodes["resource"] = _node(
        "database", "c.py", 15, basis=IdentityBasis.RESOURCE_KEY, node_class=NodeClass.RESOURCE
    )
    nodes["missing"] = _node(
        "unknown.target",
        "a.py",
        21 if new else 20,
        basis=IdentityBasis.UNRESOLVED_REFERENCE,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        origin=nodes["A"],
    )
    nodes["file"] = _node("a.py", "a.py", basis=IdentityBasis.FILE_PATH, node_class=NodeClass.FILE)
    # Adding a second incoming source and outgoing target changes existing aggregate rows.
    edges = [
        ("A", "B", "calls"),
        ("B", "C", "references"),
        ("C", "D", "imports"),
        ("A", "moved", "calls"),
        ("loose", "B", "references"),
        ("A", "upstream", "calls"),
        ("D", "resource", "references"),
        ("B", "missing", "references"),
        ("file", "A", "contains"),
    ]
    edges += (
        [("A", "C", "calls"), ("C", "B", "calls"), ("A", "D", "calls"), ("Added", "E", "imports")]
        if new
        else [("Removed", "E", "calls"), ("D", "E", "imports")]
    )
    relationships = []
    for index, (source, target, kind) in enumerate(edges):
        path = nodes[source].location.path if nodes[source].location else "a.py"
        relationships.append(
            Relationship(
                nodes[source].id,
                nodes[target].id,
                kind,
                (Evidence(Provenance.STATIC_ANALYSIS, locations=(_location(path, 40 + index),)),),
            )
        )
    ordered_nodes = list(nodes.values())
    rng.shuffle(ordered_nodes)
    rng.shuffle(relationships)
    snapshot = ReportingSnapshot.prepare(
        GraphDocument(
            coordinate_encoding=CoordinateEncoding.UTF_8,
            nodes=tuple(ordered_nodes),
            relationships=tuple(relationships),
        ),
        systems,
    )

    def observation(line: int, fingerprint: str | None) -> CallExpressionObservation:
        return CallExpressionObservation(
            "python", _location("a.py", line), _location("a.py", line), fingerprint
        )

    observations = [
        observation(40, "changed" if new else "original"),
        observation(43, "same"),
        observation(80, "fallback"),
        observation(81, "available" if new else None),
    ]
    if not new:
        observations.append(observation(40, "original"))
    return snapshot, tuple(observations)


def parity_outputs() -> dict[str, object]:
    old, old_calls = build_side(False)
    new, new_calls = build_side(True)
    result = compare_systems(
        old, new, old_call_observations=old_calls, new_call_observations=new_calls
    )
    reports = {}
    for side, snapshot in (("old", old), ("new", new)):
        reports[side] = {
            f"{query}:{system.name}": dump_json(
                snapshot.report(query, system.name, details=True).to_dict()
            )
            for query in ("surface", "consumers", "system-deps")
            for system in snapshot.systems
        }
        reports[side]["systems"] = dump_json(snapshot.all_systems_report(details=True).to_dict())
    difference = plain_diff.diff(old.document, new.document)
    return {
        "system_diff": {
            "json": dump_json(result.to_dict()),
            "text": system_diff_view.render_text(result),
            "details": system_diff_view.render_text(result, details=True),
            "exit_code": result.exit_code,
        },
        "reports": reports,
        "diff": {
            "text": plain_diff.render_text(difference),
            "json": plain_diff.render_json(difference),
        },
    }


@pytest.mark.parametrize("name", ["system_diff", "reports", "diff"])
def test_comparison_outputs_match_frozen_bytes(name: str) -> None:
    expected = json.loads((GOLDENS / f"{name}.golden").read_text(encoding="utf-8"))
    assert parity_outputs()[name] == expected


def test_fixture_exercises_changed_rows_boundaries_and_call_residuals() -> None:
    old, old_calls = build_side(False)
    new, new_calls = build_side(True)
    result = compare_systems(
        old, new, old_call_observations=old_calls, new_call_observations=new_calls
    )
    for changes in (result.surface_changes, result.consumer_changes, result.dependency_changes):
        assert {change.kind for change in changes} == {"added", "removed", "changed"}
    assert {change.kind for change in result.boundary_changes} == {
        "added",
        "removed",
        "membership",
        "endpoint",
    }
    assert result.added_systems == ("Added",)
    assert result.removed_systems == ("Removed",)
    assert {reason for change in result.call_changes for reason in change.reasons} >= {
        "expression_changed",
        "multiplicity_changed",
    }
    assert any(change.status == "unavailable" for change in result.call_changes)
    assert any(
        change.relationship_id.startswith("relationship:call-site:")
        for change in result.call_changes
    )
    assert any(
        not change.relationship_id.startswith("relationship:call-site:")
        for change in result.call_changes
    )
