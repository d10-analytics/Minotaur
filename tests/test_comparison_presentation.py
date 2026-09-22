"""Behavioral coverage for immutable comparison presentation payloads."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

from minotaur.graph_visualizer.html.render import render_html
from minotaur.graph_visualizer.presentation import (
    build_comparison_presentation,
    build_presentation,
)
from minotaur.graph_visualizer.source import (
    capture_source_bytes,
    prepare_comparison_excerpts,
)
from minotaur.query.call_diff import CallChange, CallLimitation
from minotaur.query.graph_comparison import (
    GraphNodeChange,
    GraphRelationshipChange,
    endpoint_eligibility,
)
from minotaur.query.system_diff import SystemDiffResult


def _location(path: str, line: int) -> dict[str, object]:
    return {
        "path": path,
        "range": {
            "start": {"line": line, "character": 4},
            "end": {"line": line, "character": 12},
        },
    }


def _comparison() -> SystemDiffResult:
    old_node = {
        "id": "node:old",
        "node_class": "symbol",
        "label": "old <label>",
        "symbol_kind": "function",
        "location": _location("app.py", 0),
    }
    new_node = {**old_node, "label": "new <label>"}
    relationship_id = "relationship:call"
    old_side = {"node": old_node, "system": "legacy"}
    new_side = {"node": new_node, "system": "current"}
    old_relationship = {
        "source": "node:old",
        "target": "node:old",
        "kind": "calls",
        "evidence": [{"provenance": "old-proof", "locations": [_location("app.py", 1)]}],
    }
    new_relationship = {
        **old_relationship,
        "evidence": [{"provenance": "new-proof", "locations": [_location("app.py", 2)]}],
    }
    old_observation = {
        "language": "python",
        "callee": _location("app.py", 1),
        "expression": _location("app.py", 1),
        "fingerprint": "old-expression",
    }
    new_observation = {
        **old_observation,
        "expression": _location("app.py", 2),
        "fingerprint": "new-expression",
    }
    return SystemDiffResult(
        old_system_names=("legacy",),
        new_system_names=("current",),
        nodes=(
            GraphNodeChange(
                "node:comparison:stable",
                "changed",
                ("changed",),
                ("legacy", "current"),
                old_side,
                new_side,
            ),
            GraphNodeChange("node:comparison:removed", "removed", before=old_side),
        ),
        relationships=(
            GraphRelationshipChange(
                relationship_id,
                "node:comparison:stable",
                "node:comparison:stable",
                "calls",
                "changed",
                before=old_relationship,
                after=new_relationship,
                involved_systems=("legacy", "current"),
                eligibility=endpoint_eligibility(
                    (("legacy", "legacy"),), (("current", "current"),)
                ),
            ),
            GraphRelationshipChange(
                "relationship:comparison:removed",
                "node:comparison:removed",
                "node:comparison:removed",
                "calls",
                "removed",
                before=old_relationship,
                eligibility=endpoint_eligibility((("legacy", "legacy"),), ()),
            ),
        ),
        call_changes=(
            CallChange(
                relationship_id,
                "changed",
                reasons=("expression_changed",),
                involved_systems=("legacy", "current"),
                before=(old_observation,),
                after=(new_observation,),
            ),
        ),
        old_revision="old-sha",
        new_revision="new-sha",
    )


def test_comparison_excerpts_use_captured_bytes_after_live_source_changes(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    source = root / "app.py"
    source.write_text("def caller():\n    old_call()\n", encoding="utf-8")
    captured = {"before": capture_source_bytes(root, ["app.py"]), "after": {}}

    source.write_text("def caller():\n    hostile_live_revision()\n", encoding="utf-8")
    shutil.rmtree(root)
    excerpts = prepare_comparison_excerpts(_comparison(), captured)

    before = excerpts["before"]
    assert isinstance(before, dict)
    assert before["paths"]["app.py"]["status"] == "available"
    assert "old_call()" in before["paths"]["app.py"]["spans"][0]["lines"][1]
    assert "hostile_live_revision" not in json.dumps(excerpts)
    assert excerpts["after"]["paths"]["app.py"]["status"] == "unavailable"


def test_comparison_payload_retains_stable_records_and_side_defaults() -> None:
    payload = build_comparison_presentation(_comparison())
    graph = payload["graph"]
    assert isinstance(graph, dict)
    changed_node = next(item for item in graph["nodes"] if item["id"] == "node:comparison:stable")
    assert changed_node["presence"] == "both"
    assert changed_node["status"] == "changed"
    assert changed_node["involved_systems"] == ["current", "legacy"]
    removed_edge = next(
        item for item in graph["relationships"] if item["id"] == "relationship:comparison:removed"
    )
    assert removed_edge["presence"] == "before"
    assert removed_edge["default_side"] == "before"
    changed_edge = next(
        item for item in graph["relationships"] if item["id"] == "relationship:call"
    )
    assert changed_edge["before"]["evidence"][0]["provenance"] == "old-proof"
    assert changed_edge["after"]["evidence"][0]["provenance"] == "new-proof"
    call = payload["comparison"]["calls"][0]
    assert call["id"] == "relationship:call"
    assert call["default_side"] == "after"


def test_production_comparison_payload_retains_per_side_membership_and_eligibility() -> None:
    """C-05/C-07: the serialized payload keeps side membership, never a union."""
    payload = build_comparison_presentation(_comparison())

    changed_node = next(
        item for item in payload["graph"]["nodes"] if item["id"] == "node:comparison:stable"
    )
    # The moved node belongs to legacy Before and current After; the flat union
    # would lose that distinction.
    assert changed_node["before"]["system"] == "legacy"
    assert changed_node["after"]["system"] == "current"
    assert changed_node["involved_systems"] == ["current", "legacy"]
    assert payload["node_systems"]["node:comparison:stable"] == {
        "before": "legacy",
        "after": "current",
    }

    changed_edge = next(
        item for item in payload["graph"]["relationships"] if item["id"] == "relationship:call"
    )
    eligibility = changed_edge["eligibility"]
    assert eligibility["before"] == {
        "source_systems": ["legacy"],
        "target_systems": ["legacy"],
    }
    assert eligibility["after"] == {
        "source_systems": ["current"],
        "target_systems": ["current"],
    }
    assert eligibility["internal_systems"] == ["current", "legacy"]
    assert eligibility["boundary_systems"] == []


def test_comparison_html_is_inert_and_ordinary_graph_presentation_stays_separate() -> None:
    result = _comparison()
    payload = build_comparison_presentation(result)
    payload["graph"]["nodes"][0]["before"]["node"]["label"] = "</script><script>alert(1)</script>"
    html = render_html(payload).decode("utf-8")
    assert "</script><script>alert(1)</script>" not in html
    assert "<\\/script>" in html

    ordinary = build_presentation({"nodes": [], "relationships": []})
    assert "comparison" not in ordinary
    assert ordinary["graph"] == {"nodes": [], "relationships": []}


def test_comparison_call_site_does_not_use_cross_file_caller_context() -> None:
    comparison = _comparison()
    nodes = list(comparison.nodes)
    caller = nodes[0]
    before = dict(caller.before)
    after = dict(caller.after)
    before["node"] = {**dict(before["node"]), "location": _location("caller.py", 40)}
    after["node"] = {**dict(after["node"]), "location": _location("caller.py", 40)}
    nodes[0] = GraphNodeChange(
        caller.id,
        caller.status,
        caller.reasons,
        caller.involved_systems,
        before,
        after,
    )
    comparison = replace(comparison, nodes=tuple(nodes))

    excerpts = prepare_comparison_excerpts(comparison, {"before": {}, "after": {}})

    before_site = excerpts["before"]["call_sites"]["relationship:call"][0]
    after_site = excerpts["after"]["call_sites"]["relationship:call"][0]
    assert "caller_start" not in before_site
    assert "caller_start" not in after_site


def test_comparison_excerpt_includes_same_file_caller_start_in_bounded_span() -> None:
    comparison = _comparison()
    caller = comparison.nodes[0]
    nodes = (
        GraphNodeChange(
            caller.id,
            caller.status,
            caller.reasons,
            caller.involved_systems,
            {
                **dict(caller.before),
                "node": {**dict(caller.before["node"]), "location": _location("app.py", 0)},
            },
            {
                **dict(caller.after),
                "node": {**dict(caller.after["node"]), "location": _location("app.py", 0)},
            },
        ),
        *comparison.nodes[1:],
    )
    call = comparison.call_changes[0]

    def relocate(value: object, line: int) -> dict[str, object]:
        assert isinstance(value, tuple)
        observation = dict(value[0])
        observation["callee"] = _location("app.py", line)
        observation["expression"] = _location("app.py", line)
        return observation

    relocated_call = CallChange(
        call.relationship_id,
        call.status,
        call.reasons,
        call.involved_systems,
        before=(relocate(call.before, 120),),
        after=(relocate(call.after, 121),),
    )
    comparison = replace(comparison, nodes=nodes, call_changes=(relocated_call,))
    content = "\n".join(f"line {line}" for line in range(200)).encode()

    excerpts = prepare_comparison_excerpts(
        comparison,
        {"before": {"app.py": content}, "after": {"app.py": content}},
    )

    assert excerpts["before"]["paths"]["app.py"]["spans"][0]["start"] == 0
    assert excerpts["after"]["paths"]["app.py"]["spans"][0]["start"] == 0


def _unchanged_comparison() -> SystemDiffResult:
    node = {
        "id": "node:same",
        "node_class": "symbol",
        "label": "same",
        "symbol_kind": "function",
        "location": _location("app.py", 0),
    }
    relationship = {
        "source": "node:same",
        "target": "node:same",
        "kind": "calls",
        "evidence": [],
    }
    side = {"node": node, "system": "A"}
    return SystemDiffResult(
        old_system_names=("A",),
        new_system_names=("A",),
        nodes=(GraphNodeChange("node:same", "unchanged", before=side, after=side),),
        relationships=(
            GraphRelationshipChange(
                "relationship:same",
                "node:same",
                "node:same",
                "calls",
                "unchanged",
                before=relationship,
                after=relationship,
            ),
        ),
        old_revision="before-sha",
        new_revision="after-sha",
    )


def test_comparison_no_change_and_call_only_states_are_data_driven() -> None:
    """The stored complete boolean drives the disabled control and message."""
    identical = build_comparison_presentation(_unchanged_comparison())
    assert identical["comparison"]["changed"] is False
    assert identical["comparison"]["limitations"] == []
    html = render_html(identical).decode("utf-8")
    assert 'id="emphasis-changes"' in html
    assert 'id="comparison-no-change"' in html

    call_only = replace(
        _unchanged_comparison(),
        call_changes=(
            CallChange(
                "relationship:same",
                "changed",
                reasons=("expression_changed",),
                before=({"language": "python", "fingerprint": "old"},),
                after=({"language": "python", "fingerprint": "new"},),
            ),
        ),
    )
    payload = build_comparison_presentation(call_only)
    assert payload["comparison"]["changed"] is True
    assert payload["comparison"]["calls"][0]["status"] == "changed"
    assert payload["comparison"]["calls"][0]["relationship_id"] == "relationship:same"


def test_comparison_unavailable_expression_evidence_is_reported_not_equality() -> None:
    """Unavailable call evidence is a retained limitation, never a change."""
    comparison = replace(
        _unchanged_comparison(),
        call_changes=(
            CallChange(
                "relationship:same",
                "unavailable",
                reasons=("unavailable",),
                before=(
                    {
                        "language": "python",
                        "callee": _location("app.py", 1),
                        "expression": _location("app.py", 1),
                        "fingerprint": None,
                    },
                ),
            ),
        ),
        limitations=(
            CallLimitation(
                "old",
                "relationship:same",
                "call-expression-unavailable",
                "structural call expression was unavailable for one or more observations",
            ),
        ),
    )

    payload = build_comparison_presentation(comparison)

    # Missing expression evidence is neither invented as a change nor presented
    # as expression equality; the limitation stays explicit for details and the
    # report-level notice.
    assert payload["comparison"]["changed"] is False
    assert payload["comparison"]["calls"][0]["status"] == "unavailable"
    limitation = payload["comparison"]["limitations"][0]
    assert limitation["side"] == "old"
    assert limitation["code"] == "call-expression-unavailable"
    html = render_html(payload).decode("utf-8")
    assert "call-expression-unavailable" in html
    assert "structural call expression was unavailable" in html
