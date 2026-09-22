"""Behavioral proof for explicit whole-graph correspondence and classification."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_system_diff import _call, _snapshot, _symbol, _systems

from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.provenance import Provenance
from minotaur.graph_model.relationship import Relationship
from minotaur.query.correspondence import CorrespondenceAmbiguityError
from minotaur.query.graph_comparison import compare_graphs


def test_internal_and_edgeless_nodes_are_retained_in_whole_graph_result() -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "a.py", 1)
    added = _symbol("added", "a.py", 2)
    systems = _systems(("a.toml", "A", ("a.py",)))
    contains = Relationship(
        source.id,
        target.id,
        "contains",
        (Evidence(Provenance.STATIC_ANALYSIS),),
    )

    result = compare_graphs(
        _snapshot((source, target), (), systems),
        _snapshot((source, target, added), (contains,), systems),
    )

    assert {item.status for item in result.nodes} == {"unchanged", "added"}
    assert any(item.status == "added" and item.before is None for item in result.nodes)
    assert [(item.kind, item.status, item.reasons) for item in result.relationships] == [
        ("contains", "added", ("added",))
    ]
    assert result.changed


def test_membership_move_is_a_node_reason_without_replacing_semantic_id() -> None:
    node = _symbol("entry", "b.py")
    old_systems = _systems(("b.toml", "Before", ("b.py",)))
    new_systems = _systems(("b.toml", "After", ("b.py",)))

    result = compare_graphs(
        _snapshot((node,), (), old_systems),
        _snapshot((node,), (), new_systems),
    )

    assert len(result.nodes) == 1
    assert result.nodes[0].status == "changed"
    assert result.nodes[0].reasons == ("membership_changed",)
    assert result.nodes[0].involved_systems == ("After", "Before")


def test_ambiguous_ordinary_candidates_fail_deterministically() -> None:
    first = _symbol("same", "a.py", 0)
    second = _symbol("same", "a.py", 1)
    systems = _systems(("a.toml", "A", ("a.py",)))

    with pytest.raises(CorrespondenceAmbiguityError) as raised:
        compare_graphs(_snapshot((first, second), (), systems), _snapshot((first,), (), systems))

    assert raised.value.endpoint == "node"
    assert raised.value.candidate_ids == tuple(sorted((first.id, second.id)))


def test_graph_projection_is_input_order_independent() -> None:
    source = _symbol("source", "a.py")
    target = _symbol("target", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    relation = _call(source, target)

    first = compare_graphs(
        _snapshot((source, target), (), systems),
        _snapshot((source, target), (relation,), systems),
    )
    second = compare_graphs(
        _snapshot((target, source), (), tuple(reversed(systems))),
        _snapshot(
            (target, source),
            (replace(relation, evidence=tuple(reversed(relation.evidence))),),
            tuple(reversed(systems)),
        ),
    )

    assert first.to_dict() == second.to_dict()


def test_per_side_membership_and_eligibility_do_not_collapse_to_the_union() -> None:
    """C-05 stores each side's own membership; V-21 never combines revisions."""
    shared = _symbol("shared", "a.py")
    moved = _symbol("moved", "b.py")
    relation = _call(shared, moved)
    old_systems = _systems(("a.toml", "A", ("a.py", "b.py")))
    new_systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))

    result = compare_graphs(
        _snapshot((shared, moved), (relation,), old_systems),
        _snapshot((shared, moved), (relation,), new_systems),
    )

    moved_node = next(
        item for item in result.nodes if item.to_dict()["before"]["node"]["label"] == "moved"
    )
    assert moved_node.before["system"] == "A"
    assert moved_node.after["system"] == "B"
    assert moved_node.involved_systems == ("A", "B")

    [edge] = result.relationships
    eligibility = edge.to_dict()["eligibility"]
    assert eligibility["before"] == {"source_systems": ["A"], "target_systems": ["A"]}
    assert eligibility["after"] == {"source_systems": ["A"], "target_systems": ["B"]}
    # A was internal only on the Before side; B is a boundary system, never an
    # invented A-internal relationship on the After side.
    assert eligibility["internal_systems"] == ["A"]
    assert eligibility["boundary_systems"] == ["B"]
