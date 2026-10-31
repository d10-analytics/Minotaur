"""Natural-trigger and edge-case proof for the pure system comparison view."""

from __future__ import annotations

import difflib
import json
from dataclasses import replace

import pytest
from test_comparison_parity import build_side
from test_system_diff import _call, _reference, _snapshot, _symbol, _systems, _upstream

from minotaur.graph_model.evidence import Evidence, Producer
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.provenance import Provenance
from minotaur.graph_model.relationship import Relationship
from minotaur.language_interpreter.call_expressions import CallExpressionObservation
from minotaur.query import system_diff as system_diff_module
from minotaur.query.call_diff import CallLimitation
from minotaur.query.render import dump_json
from minotaur.query.system import ReportingSnapshot
from minotaur.query.system_diff import SystemChange, SystemDiffResult, compare_systems
from minotaur.query.system_diff_view import (
    filter_system_diff,
    render_context_text,
    render_text,
)
from minotaur.system import UnknownSystem


def _checkout_notifications_result() -> SystemDiffResult:
    checkout = _symbol("checkout", "checkout.py")
    sender = _symbol("send_receipt", "payments.py")
    email = _symbol("email_receipt", "notifications.py")
    old_systems = _systems(
        ("checkout.toml", "Checkout", ("checkout.py",)),
        ("payments.toml", "Payments", ("payments.py",)),
    )
    new_systems = _systems(
        ("checkout.toml", "Checkout", ("checkout.py",)),
        ("notifications.toml", "Notifications", ("notifications.py",)),
        ("payments.toml", "Payments", ("payments.py",)),
    )
    old = _snapshot((checkout, sender), (_call(checkout, sender),), old_systems)
    new = _snapshot(
        (checkout, sender, email),
        (_call(checkout, sender), _call(sender, email)),
        new_systems,
    )
    return compare_systems(old, new)


def _ab_addition(kind: str = "calls") -> SystemDiffResult:
    source = _symbol("send", "a.py")
    target = _symbol("receive", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    old = _snapshot((source, target), (), systems)
    relationship = _call(source, target) if kind == "calls" else _reference(source, target)
    if kind == "imports":
        relationship = replace(relationship, kind="imports")
    new = _snapshot((source, target), (relationship,), systems)
    return compare_systems(old, new)


def _ab_removal() -> SystemDiffResult:
    source = _symbol("send", "a.py")
    target = _symbol("receive", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    old = _snapshot((source, target), (_call(source, target),), systems)
    new = _snapshot((source, target), (), systems)
    return compare_systems(old, new)


def _ab_changed_rows() -> SystemDiffResult:
    source = _symbol("send", "a.py")
    target = _symbol("receive", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    old = _snapshot((source, target), (_call(source, target),), systems)
    new = _snapshot((source, target), (_reference(source, target),), systems)
    return compare_systems(old, new)


def _membership_endpoint_result() -> SystemDiffResult:
    source = _symbol("send", "caller.py")
    old_target = _upstream("old_receive", identifier="target", path="b.py")
    new_target = _upstream("new_receive", identifier="target", path="b.py")
    old_systems = _systems(
        ("a.toml", "A", ("a.py",)),
        ("b.toml", "B", ("b.py",)),
    )
    new_systems = _systems(
        ("a.toml", "A", ("a.py", "b.py")),
    )
    old = _snapshot((source, old_target), (_call(source, old_target),), old_systems)
    new = _snapshot((source, new_target), (_call(source, new_target),), new_systems)
    return compare_systems(old, new)


def _context_lines(result: SystemDiffResult) -> list[str]:
    payload = result.to_dict()
    coverage = payload["coverage"]
    selection = payload["selection"]
    assert isinstance(coverage, dict)
    assert isinstance(selection, dict)
    return [
        f"old coverage: {dump_json(coverage['old']).rstrip(chr(10))}",
        f"new coverage: {dump_json(coverage['new']).rstrip(chr(10))}",
        f"old selection: {dump_json(selection['old']).rstrip(chr(10))}",
        f"new selection: {dump_json(selection['new']).rstrip(chr(10))}",
    ]


def _assert_exact_compact(result: SystemDiffResult, changed_lines: list[str]) -> None:
    expected = "".join(f"{line}\n" for line in changed_lines + _context_lines(result))
    assert render_text(result) == expected


def _detail_block(text: str, changed_line: str) -> dict[str, object]:
    lines = text.splitlines()
    index = lines.index(changed_line)
    values: dict[str, object] = {}
    for offset, label in enumerate(("old", "new", "old evidence", "new evidence"), 1):
        prefix = f"{label}: "
        assert lines[index + offset].startswith(prefix)
        value = lines[index + offset][len(prefix) :]
        values[label] = "unavailable" if value == "unavailable" else json.loads(value)
    return values


def _assert_relationship_evidence(
    evidence: object, *, kind: str, source_label: str, target_label: str
) -> None:
    assert isinstance(evidence, list)
    assert len(evidence) == 1
    relationship = evidence[0]
    assert isinstance(relationship, dict)
    assert relationship["kind"] == kind
    assert relationship["relationship_extensions"] == {"status": "unavailable"}
    assert relationship["source"]["label"] == source_label
    assert relationship["target"]["label"] == target_label
    assert relationship["evidence"] == [
        {
            "evidence_extensions": {"status": "unavailable"},
            "producer": {"status": "unavailable"},
            "provenance": "static-analysis",
            "rule": {"status": "unavailable"},
            "sites": [],
        }
    ]


def test_replacement_selection_uses_complete_result_and_copies_none() -> None:
    complete = _checkout_notifications_result()
    original = complete.to_dict()

    all_view = filter_system_diff(complete, None)
    checkout = filter_system_diff(complete, "Checkout")
    notifications = filter_system_diff(complete, "Notifications")
    repeated = filter_system_diff(complete, "Notifications")

    assert all_view is not complete
    assert all_view == complete
    assert all_view.to_dict() == original
    assert all_view.changed == complete.changed
    assert all_view.exit_code == complete.exit_code
    assert all_view.old_coverage == complete.old_coverage
    assert all_view.new_coverage == complete.new_coverage
    assert all_view.old_selection == complete.old_selection
    assert all_view.new_selection == complete.new_selection
    assert all("Notifications" not in change.involved_systems for change in checkout.differences)
    assert notifications == repeated
    assert any(
        change.domain == "boundary" and "Payments" in change.involved_systems
        for change in notifications.differences
    )
    assert any(
        change.domain == "membership" and "Notifications" in change.involved_systems
        for change in notifications.differences
    )
    unrelated = filter_system_diff(complete, "Checkout")
    assert unrelated.differences == ()
    assert unrelated.changed is False
    assert unrelated.exit_code == 0
    assert complete.to_dict() == original


def test_view_retains_complete_graph_call_and_limitation_facts() -> None:
    complete = SystemDiffResult(
        old_system_names=("Checkout",),
        new_system_names=("Checkout",),
        nodes=(
            system_diff_module.GraphNodeChange(
                "node:comparison:one",
                "changed",
                ("membership_changed",),
                ("Checkout",),
                {"label": "old"},
                {"label": "new"},
            ),
        ),
        relationships=(),
        call_changes=(),
    )

    selected = filter_system_diff(complete, "Checkout")
    assert selected.nodes == complete.nodes
    assert selected.to_dict()["nodes"] == [item.to_dict() for item in complete.nodes]


def test_call_change_retains_endpoint_membership_in_selected_view() -> None:
    source = _symbol("caller", "a.py")
    target = _symbol("callee", "b.py")
    site = Location("a.py", Range(Position(2, 0), Position(2, 4)))
    edge = Relationship(
        source.id,
        target.id,
        "calls",
        (Evidence(Provenance.STATIC_ANALYSIS, locations=(site,)),),
    )
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    observation = CallExpressionObservation(
        "python", site, Location("a.py", Range(Position(2, 0), Position(2, 8))), "fingerprint"
    )

    complete = compare_systems(
        _snapshot((source, target), (edge,), systems),
        _snapshot((source, target), (edge,), systems),
        old_call_observations=(observation,),
        new_call_observations=(),
    )

    assert complete.call_changes[0].involved_systems == ("A", "B")
    assert filter_system_diff(complete, "A").call_changes == complete.call_changes
    assert filter_system_diff(complete, "B").call_changes == complete.call_changes


def test_old_only_deleted_name_resolves_and_unrelated_name_is_neutral() -> None:
    old_only = _symbol("legacy", "legacy.py")
    old_systems = _systems(("legacy.toml", "Legacy", ("legacy.py",)))
    new = _snapshot((), (), ())
    result = compare_systems(_snapshot((old_only,), (), old_systems), new)

    deleted = filter_system_diff(result, "Legacy")
    assert deleted.removed_systems == ("Legacy",)
    assert deleted.old_system_names == ("Legacy",)
    assert deleted.new_system_names == ()
    assert deleted.changed is True
    assert deleted.exit_code == 1

    neutral = filter_system_diff(result, "Legacy")
    assert neutral.to_dict() == deleted.to_dict()


def test_unknown_suggestions_use_sorted_complete_union_and_exact_message() -> None:
    result = SystemDiffResult(
        old_system_names=("billing", "orders"),
        new_system_names=("billing", "orders", "shipping"),
    )
    with pytest.raises(UnknownSystem) as error:
        filter_system_diff(result, "oder")
    assert error.value.name == "oder"
    assert error.value.nearest == tuple(
        difflib.get_close_matches("oder", sorted({"billing", "orders", "shipping"}), n=5)
    )
    assert str(error.value) == "unknown system: oder; nearest systems: orders"

    with pytest.raises(UnknownSystem) as unmatched:
        filter_system_diff(SystemDiffResult(), "missing")
    assert unmatched.value.nearest == ()
    assert str(unmatched.value) == "unknown system: missing"

    names = tuple(f"billing-{index:02d}" for index in range(8))
    with pytest.raises(UnknownSystem) as capped:
        filter_system_diff(SystemDiffResult(old_system_names=names), "billing-0")
    assert capped.value.nearest == tuple(difflib.get_close_matches("billing-0", sorted(names), n=5))
    assert len(capped.value.nearest) == 5


def test_each_involved_system_retains_every_natural_row_category_exactly() -> None:
    complete = _ab_addition()
    selected_a = filter_system_diff(complete, "A")
    selected_b = filter_system_diff(complete, "B")

    for field_name in (
        "surface_changes",
        "consumer_changes",
        "dependency_changes",
        "boundary_changes",
    ):
        expected = getattr(complete, field_name)
        assert expected
        assert getattr(selected_a, field_name) == expected
        assert getattr(selected_b, field_name) == expected
        assert all(change.involved_systems == ("A", "B") for change in expected)


def test_added_categories_and_system_use_exact_compact_grammar_and_order() -> None:
    _assert_exact_compact(
        _checkout_notifications_result(),
        [
            "system added: Notifications",
            "membership changed: notifications.py — unassigned -> Notifications",
            "surface added: Notifications notifications.py.email_receipt",
            "consumer added: Notifications <- payments.py",
            "dependency added: Payments -> Notifications",
            "boundary added: Payments.send_receipt -> Notifications.email_receipt (calls)",
            "node added: email_receipt (notifications.py, function)",
            "relationship added: send_receipt -> email_receipt (calls)",
        ],
    )
    _assert_exact_compact(
        _ab_addition(),
        [
            "surface added: B b.py.receive",
            "consumer added: B <- a.py",
            "dependency added: A -> B",
            "boundary added: A.send -> B.receive (calls)",
            "relationship added: send -> receive (calls)",
        ],
    )


def test_removed_and_changed_rows_and_system_use_exact_compact_grammar() -> None:
    removed = _ab_removal()
    _assert_exact_compact(
        removed,
        [
            "surface removed: B b.py.receive",
            "consumer removed: B <- a.py",
            "dependency removed: A -> B",
            "boundary removed: A.send -> B.receive (calls)",
            "relationship removed: send -> receive (calls)",
        ],
    )

    old_only = _symbol("legacy", "legacy.py")
    old_systems = _systems(("legacy.toml", "Legacy", ("legacy.py",)))
    removed_system = compare_systems(_snapshot((old_only,), (), old_systems), _snapshot((), (), ()))
    _assert_exact_compact(
        removed_system,
        [
            "system removed: Legacy",
            "membership changed: legacy.py — Legacy -> unassigned",
            "node removed: legacy (legacy.py, function)",
        ],
    )

    _assert_exact_compact(
        _ab_changed_rows(),
        [
            "surface changed: B b.py.receive",
            "consumer changed: B <- a.py",
            "dependency changed: A -> B",
            "boundary added: A.send -> B.receive (references)",
            "boundary removed: A.send -> B.receive (calls)",
            "relationship added: send -> receive (references)",
            "relationship removed: send -> receive (calls)",
        ],
    )


def test_details_preserve_exact_records_endpoints_memberships_and_evidence() -> None:
    added = render_text(filter_system_diff(_ab_addition(), "B"), details=True)
    added_expectations = {
        "surface added: B b.py.receive": {
            "category": "system: B",
            "kinds": ["calls"],
            "path": "b.py",
            "symbol": "receive",
        },
        "consumer added: B <- a.py": {
            "category": "system: A",
            "file": "a.py",
            "kinds": ["calls"],
            "targets": [{"kind": "calls", "label": "receive", "path": "b.py"}],
        },
        "dependency added: A -> B": {
            "category": "system: B",
            "targets": [{"kind": "calls", "label": "receive", "path": "b.py"}],
        },
    }
    for line, expected_record in added_expectations.items():
        details = _detail_block(added, line)
        assert details["old"] == "unavailable"
        assert details["new"]["record"] == expected_record  # type: ignore[index]
        assert details["new"]["involved_systems"] == ["A", "B"]  # type: ignore[index]
        _assert_relationship_evidence(
            details["new"]["relationships"],  # type: ignore[index]
            kind="calls",
            source_label="send",
            target_label="receive",
        )
        assert details["old evidence"] == "unavailable"
        _assert_relationship_evidence(
            details["new evidence"],
            kind="calls",
            source_label="send",
            target_label="receive",
        )

    boundary = _detail_block(
        added,
        "boundary added: A.send -> B.receive (calls)",
    )
    assert boundary["old"] == "unavailable"
    new_boundary = boundary["new"]
    assert new_boundary["categories"] == ["system: A", "system: B"]  # type: ignore[index]
    assert new_boundary["source_category"] == "system: A"  # type: ignore[index]
    assert new_boundary["target_category"] == "system: B"  # type: ignore[index]
    assert new_boundary["source_membership"] == "system: A"  # type: ignore[index]
    assert new_boundary["target_membership"] == "system: B"  # type: ignore[index]
    assert new_boundary["source_endpoint"]["label"] == "send"  # type: ignore[index]
    assert new_boundary["target_endpoint"]["label"] == "receive"  # type: ignore[index]
    _assert_relationship_evidence(
        new_boundary["relationships"],  # type: ignore[index]
        kind="calls",
        source_label="send",
        target_label="receive",
    )
    assert boundary["old evidence"] == "unavailable"
    _assert_relationship_evidence(
        boundary["new evidence"],
        kind="calls",
        source_label="send",
        target_label="receive",
    )

    removed = render_text(filter_system_diff(_ab_removal(), "B"), details=True)
    for line in (
        "surface removed: B b.py.receive",
        "consumer removed: B <- a.py",
        "dependency removed: A -> B",
        "boundary removed: A.send -> B.receive (calls)",
    ):
        details = _detail_block(removed, line)
        assert details["new"] == "unavailable"
        assert details["new evidence"] == "unavailable"
        assert isinstance(details["old"], dict)
        _assert_relationship_evidence(
            details["old evidence"],
            kind="calls",
            source_label="send",
            target_label="receive",
        )

    changed_aspects = render_text(_membership_endpoint_result(), details=True)
    for line in (
        "boundary endpoint: no_system.send -> A.new_receive (calls)",
        "boundary membership: no_system.send -> A.new_receive (calls)",
    ):
        details = _detail_block(changed_aspects, line)
        old_side = details["old"]
        new_side = details["new"]
        assert old_side["categories"] == ["no_system", "system: B"]  # type: ignore[index]
        assert new_side["categories"] == ["no_system", "system: A"]  # type: ignore[index]
        assert old_side["source_membership"] == "no_system"  # type: ignore[index]
        assert new_side["target_membership"] == "system: A"  # type: ignore[index]
        assert old_side["target_endpoint"]["label"] == "old_receive"  # type: ignore[index]
        assert new_side["target_endpoint"]["label"] == "new_receive"  # type: ignore[index]
        _assert_relationship_evidence(
            details["old evidence"],
            kind="calls",
            source_label="send",
            target_label="old_receive",
        )
        _assert_relationship_evidence(
            details["new evidence"],
            kind="calls",
            source_label="send",
            target_label="new_receive",
        )


def test_boundary_membership_and_endpoint_lines_and_details_are_exact_and_adjacent() -> None:
    result = _membership_endpoint_result()
    expected_lines = [
        "system removed: B",
        "membership changed: b.py — B -> A",
        "surface added: A b.py.new_receive",
        "surface removed: B b.py.old_receive",
        "consumer added: A <- caller.py",
        "consumer removed: B <- caller.py",
        "boundary endpoint: no_system.send -> A.new_receive (calls)",
        "boundary membership: no_system.send -> A.new_receive (calls)",
    ]
    internal_lines = [
        "node changed: new_receive (b.py, function) [changed, membership_changed]",
        "relationship changed: send -> new_receive (calls) [membership_changed]",
    ]
    _assert_exact_compact(result, expected_lines + internal_lines)

    lines = render_text(result, details=True).splitlines()
    for changed_line in expected_lines:
        index = lines.index(changed_line)
        assert lines[index + 1].startswith("old: ")
        assert lines[index + 2].startswith("new: ")
        assert lines[index + 3].startswith("old evidence: ")
        assert lines[index + 4].startswith("new evidence: ")
    for changed_line in expected_lines[-2:]:
        index = lines.index(changed_line)
        assert lines[index + 1].startswith("old: {")
        assert lines[index + 2].startswith("new: {")
        assert lines[index + 3].startswith("old evidence: [")
        assert lines[index + 4].startswith("new evidence: [")
    assert lines[-4:] == _context_lines(result)


def test_membership_and_endpoint_aspects_remain_separate_for_both_systems() -> None:
    source = _symbol("send", "caller.py")
    old_target = _upstream("old_receive", identifier="target", path="b.py")
    new_target = _upstream("new_receive", identifier="target", path="b.py")
    old_systems = _systems(
        ("a.toml", "A", ("a.py",)),
        ("b.toml", "B", ("b.py",)),
    )
    new_systems = _systems(
        ("a.toml", "A", ("a.py", "b.py")),
    )
    old = _snapshot((source, old_target), (_call(source, old_target),), old_systems)
    new = _snapshot((source, new_target), (_call(source, new_target),), new_systems)
    complete = compare_systems(old, new)

    assert [change.kind for change in complete.boundary_changes] == ["membership", "endpoint"]
    for name in ("A", "B"):
        selected = filter_system_diff(complete, name)
        assert [change.kind for change in selected.boundary_changes] == ["membership", "endpoint"]
        membership, endpoint = selected.boundary_changes
        assert membership.old["categories"] == ("no_system", "system: B")  # type: ignore[index]
        assert membership.new["categories"] == ("no_system", "system: A")  # type: ignore[index]
        assert endpoint.old["target_endpoint"]["label"] == "old_receive"  # type: ignore[index]
        assert endpoint.new["target_endpoint"]["label"] == "new_receive"  # type: ignore[index]


@pytest.mark.parametrize("kind", ("calls", "references", "imports"))
def test_all_supported_relationship_kinds_render_and_imports_stay_out_of_surface(kind: str) -> None:
    result = _ab_addition(kind)
    text = render_text(filter_system_diff(result, "B"))
    assert f"({kind})" in text
    assert result.consumer_changes
    assert result.dependency_changes
    if kind == "imports":
        assert not result.surface_changes


def test_rendering_is_pure_after_all_acquisition_seams_fail(monkeypatch) -> None:
    complete = _checkout_notifications_result()

    def fail(*_args, **_kwargs):
        raise AssertionError("a completed-result view must not reacquire inputs")

    monkeypatch.setattr(ReportingSnapshot, "report", fail)
    monkeypatch.setattr(system_diff_module, "compare_systems", fail)

    selected = filter_system_diff(complete, "Notifications")
    changed, exit_code = selected.changed, selected.exit_code
    assert render_context_text(selected)
    assert render_text(selected, details=True)
    assert selected.changed is changed
    assert selected.exit_code == exit_code


def test_text_grammar_context_order_details_and_escaping_are_exact() -> None:
    result = _checkout_notifications_result()
    text = render_text(filter_system_diff(result, "Notifications"), details=True)
    lines = text.splitlines()
    assert any(line.startswith("boundary added: Payments.send_receipt") for line in lines)
    boundary_index = next(
        index for index, line in enumerate(lines) if line.startswith("boundary added:")
    )
    assert lines[boundary_index + 1].startswith("old: unavailable")
    assert lines[boundary_index + 2].startswith("new: {")
    assert lines[boundary_index + 3].startswith("old evidence: unavailable")
    assert lines[boundary_index + 4].startswith("new evidence: [")
    assert lines[-4].startswith("old coverage: ")
    assert lines[-3].startswith("new coverage: ")
    assert lines[-2].startswith("old selection: ")
    assert lines[-1].startswith("new selection: ")

    exceptional = SystemDiffResult(
        surface_changes=(
            SystemChange(
                "surface",
                "added",
                ("A", "odd\npath", "symbol\tname"),
                None,
                {"record": {"path": "odd\npath", "symbol": "symbol\tname"}},
                ("A",),
            ),
        )
    )
    escaped = render_text(exceptional)
    assert "odd\\npath" in escaped
    assert "symbol\\tname" in escaped
    assert len(escaped.splitlines()) == 5
    assert render_text(SystemDiffResult()).startswith("no system differences\nold coverage: ")


def test_permuted_source_and_evidence_order_has_identical_selected_views() -> None:
    source = _symbol("send", "a.py")
    target = _symbol("receive", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    first = Evidence(Provenance.STATIC_ANALYSIS, producer=Producer("one"))
    second = Evidence(Provenance.STATIC_ANALYSIS, producer=Producer("two"))
    relationship = Relationship(source.id, target.id, "calls", (first, second))
    reverse = Relationship(source.id, target.id, "calls", (second, first))
    old_first = _snapshot((source, target), (), systems)
    new_first = _snapshot((source, target), (relationship,), systems)
    old_second = _snapshot((target, source), (), systems)
    new_second = _snapshot((target, source), (reverse,), systems)
    first_result = filter_system_diff(compare_systems(old_first, new_first), "B")
    second_result = filter_system_diff(compare_systems(old_second, new_second), "B")
    assert dump_json(first_result.to_dict()) == dump_json(second_result.to_dict())
    assert render_text(first_result, details=True) == render_text(second_result, details=True)


_SENTINELS = ("no_system", "external", "unassigned")


@pytest.mark.parametrize(
    "name",
    (
        "system: A",
        "system: system: A",
        "external",
        "no_system",
        "unassigned",
        '"no_system"',
        '"a b',
        "Ordinary",
    ),
)
def test_literal_names_render_once_by_field_semantics(name):
    def atom(value):
        return json.dumps(value) if " " in value else value

    shown = json.dumps(name, ensure_ascii=True) if name in _SENTINELS or name[0] == '"' else name

    category = f"system: {name}"
    result = SystemDiffResult(
        old_system_names=(name,),
        new_system_names=(name,),
        membership_changes=(
            SystemChange(
                "membership",
                "changed",
                ("absent.py",),
                {"file": "absent.py", "system": None},
                {"file": "absent.py", "system": name},
            ),
        ),
        surface_changes=(SystemChange("surface", "added", (name, "a.py", "entry")),),
        consumer_changes=(SystemChange("consumers", "added", (name, "a.py")),),
        dependency_changes=(
            SystemChange(
                "system-deps",
                "added",
                (name, category),
                new={"category": category},
            ),
        ),
        boundary_changes=(
            SystemChange(
                "boundary",
                "added",
                ("opaque",),
                new={
                    "source_category": category,
                    "target_category": category,
                    "kind": "calls",
                    "source_endpoint": {"label": "source"},
                    "target_endpoint": {"label": "target"},
                },
            ),
        ),
    )
    expected = (
        f"membership changed: absent.py — unassigned -> {atom(shown)}\n"
        f"surface added: {atom(name)} a.py.entry\n"
        f"consumer added: {atom(name)} <- a.py\n"
        f"dependency added: {atom(name)} -> {atom(shown)}\n"
        f"boundary added: {atom(shown + '.source')} -> {atom(shown + '.target')} (calls)\n"
    )
    assert render_text(result) == expected + render_context_text(result)
    assert json.loads(dump_json(result.to_dict()))["membership_changes"][0]["new"]["system"] == name
    assert result.exit_code == 1
    detailed = render_text(result, details=True)
    assert all(line in detailed.splitlines() for line in expected.splitlines())


def test_sentinel_named_systems_differ_from_the_sentinels_they_collide_with():
    """A declared name prints quoted; the real sentinel and ordinary names stay plain."""

    def rows(source, target, old_owner, new_owner):
        result = SystemDiffResult(
            membership_changes=(
                SystemChange(
                    "membership",
                    "changed",
                    ("f.py",),
                    {"file": "f.py", "system": old_owner},
                    {"file": "f.py", "system": new_owner},
                ),
            ),
            dependency_changes=(
                SystemChange("system-deps", "added", ("A", target), new={"category": target}),
            ),
            boundary_changes=(
                SystemChange(
                    "boundary",
                    "added",
                    ("opaque",),
                    new={
                        "source_category": source,
                        "target_category": target,
                        "kind": "calls",
                        "source_endpoint": {"label": "s"},
                        "target_endpoint": {"label": "t"},
                    },
                ),
            ),
        )
        return render_text(result).splitlines()[:3]

    declared = rows("system: external", "system: no_system", None, "unassigned")
    assert declared == [
        'membership changed: f.py — unassigned -> "unassigned"',
        'dependency added: A -> "no_system"',
        'boundary added: "external".s -> "no_system".t (calls)',
    ]
    sentinel = rows("external", "no_system", "Owner", None)
    assert sentinel == [
        "membership changed: f.py — Owner -> unassigned",
        "dependency added: A -> no_system",
        "boundary added: external.s -> no_system.t (calls)",
    ]
    quoted = rows('system: "no_system"', 'system: "no_system"', None, '"no_system"')
    assert quoted[1] == 'dependency added: A -> "\\"no_system\\""'
    assert quoted[1] != declared[1]
    ordinary = rows("system: A", "system: B", "A", "B")
    assert ordinary == [
        "membership changed: f.py — A -> B",
        "dependency added: A -> B",
        "boundary added: A.s -> B.t (calls)",
    ]


_ROW_PREFIXES = (
    "system ",
    "membership ",
    "surface ",
    "consumer ",
    "dependency ",
    "boundary ",
    "node ",
    "relationship ",
    "call ",
)
_LIMITATION_LINE = (
    "limitation old: call-expression-unavailable: "
    "structural call expression was unavailable for one or more observations"
)


def _rows(text: str, prefixes: tuple[str, ...] = _ROW_PREFIXES) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(prefixes)]


def _assert_rows_iff_exit_one(result: SystemDiffResult, text: str) -> None:
    """A difference row is printed exactly when the exit status is 1."""
    assert bool(_rows(text)) is (result.exit_code == 1)
    assert ("no system differences" in text.splitlines()) is (result.exit_code == 0)


def _site(line: int, path: str = "a.py") -> Location:
    return Location(path, Range(Position(line, 0), Position(line, 4)))


def _observation(
    line: int, fingerprint: str | None, path: str = "a.py"
) -> CallExpressionObservation:
    return CallExpressionObservation(
        "python",
        _site(line, path),
        Location(path, Range(Position(line, 0), Position(line, 9))),
        fingerprint,
    )


def _site_call(source, target, line: int, path: str = "a.py") -> Relationship:
    return Relationship(
        source.id,
        target.id,
        "calls",
        (Evidence(Provenance.STATIC_ANALYSIS, locations=(_site(line, path),)),),
    )


def _mixed_result() -> SystemDiffResult:
    """A new node, a new relationship, an added call and a changed call on an
    unchanged relationship."""
    caller = _symbol("caller", "a.py")
    callee = _symbol("callee", "b.py")
    fresh = _symbol("fresh", "a.py", line=5)
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    kept = _site_call(caller, callee, 2)
    old = _snapshot((caller, callee), (kept,), systems)
    new = _snapshot((caller, callee, fresh), (kept, _site_call(caller, fresh, 3)), systems)
    return compare_systems(
        old,
        new,
        old_call_observations=(_observation(2, "one"),),
        new_call_observations=(_observation(2, "two"), _observation(3, "x")),
    )


def _unavailable_only_result() -> SystemDiffResult:
    caller = _symbol("caller", "a.py")
    callee = _symbol("callee", "b.py")
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    snapshot = _snapshot((caller, callee), (_site_call(caller, callee, 2),), systems)
    return compare_systems(
        snapshot,
        snapshot,
        old_call_observations=(_observation(2, None),),
        new_call_observations=(_observation(2, "x"),),
    )


def test_unavailable_call_is_not_a_difference_and_keeps_its_limitation() -> None:
    result = _unavailable_only_result()
    assert [item.status for item in result.call_changes] == ["unavailable"]

    assert result.differences == ()
    assert result.changed is False
    assert result.exit_code == 0
    text = render_text(result)
    assert text.splitlines()[:2] == ["no system differences", _LIMITATION_LINE]
    assert _rows(text) == []
    assert "call unavailable" not in render_text(result, details=True)
    _assert_rows_iff_exit_one(result, text)


def test_limitation_line_is_rendered_exactly_before_the_context() -> None:
    result = SystemDiffResult(
        limitations=(
            CallLimitation(
                "old",
                "relationship:x",
                "call-expression-unavailable",
                "structural call expression was unavailable for one or more observations",
            ),
        )
    )
    text = render_text(result)
    assert text.splitlines()[:2] == ["no system differences", _LIMITATION_LINE]
    assert text == f"no system differences\n{_LIMITATION_LINE}\n" + render_context_text(result)


def test_changed_status_agrees_with_the_difference_rule_for_every_call_status() -> None:
    for status, expected in (
        ("added", True),
        ("removed", True),
        ("changed", True),
        ("unchanged", False),
        ("unavailable", False),
    ):
        result = SystemDiffResult(
            call_changes=(system_diff_module.CallChange("relationship:x", status, ("r",)),)
        )
        assert result.changed is expected
        assert bool(result.differences) is expected
        assert result.exit_code == int(expected)


def test_printed_internal_rows_correspond_one_to_one_to_internal_differences() -> None:
    result = _mixed_result()
    internal = [
        change
        for change in result.differences
        if change.domain in {"graph-node", "graph-relationship", "call"}
    ]
    assert [change.domain for change in internal] == [
        "graph-node",
        "graph-relationship",
        "call",
        "call",
    ]
    for details in (False, True):
        text = render_text(result, details=details)
        printed = _rows(text, ("node ", "relationship ", "call "))
        group_of = {"node": 0, "relationship": 1, "call": 2}
        domain_prefix = {"graph-node": "node", "graph-relationship": "relationship", "call": "call"}
        printed_pairs = [(line.split(" ")[0], line.split(" ")[1].rstrip(":")) for line in printed]
        entry_pairs = [(domain_prefix[change.domain], change.kind) for change in internal]
        # Same group order; within a group the same entries (rows sort by
        # status and text while `differences` keeps comparison order).
        assert [group_of[prefix] for prefix, _ in printed_pairs] == [
            group_of[prefix] for prefix, _ in entry_pairs
        ]
        assert sorted(printed_pairs) == sorted(entry_pairs)
        assert printed == [
            "node added: fresh (a.py, function)",
            "relationship added: caller -> fresh (calls)",
            "call added: caller -> fresh",
            "call changed: caller -> callee [expression_changed]",
        ]
        assert "no system differences" not in text
        _assert_rows_iff_exit_one(result, text)


def test_changed_call_on_unchanged_relationship_prints_the_call_reasons() -> None:
    result = _mixed_result()
    kept = next(item for item in result.call_changes if item.status == "changed")
    assert [item.status for item in result.relationships if item.id == kept.relationship_id] == [
        "unchanged"
    ]
    text = render_text(result)
    assert "call changed: caller -> callee [expression_changed]\n" in text
    assert not any(
        line.startswith("relationship") and "callee" in line for line in text.splitlines()
    )


def test_changed_relationship_reasons_come_from_its_own_group_not_the_call_entry() -> None:
    old, old_calls = build_side(False)
    new, new_calls = build_side(True)
    result = compare_systems(
        old, new, old_call_observations=old_calls, new_call_observations=new_calls
    )
    relationship = next(
        item for item in result.relationships if item.reasons == ("membership_changed",)
    )
    assert relationship.status == "changed"
    shadow = [item for item in result.call_changes if item.relationship_id == relationship.id]
    assert [(item.status, item.reasons) for item in shadow] == [("unchanged", ())]

    row = "relationship changed: A -> moved (calls) [membership_changed]"
    assert row in render_text(result).splitlines()
    assert row in render_text(result, details=True).splitlines()


def test_call_without_a_graph_relationship_prints_its_one_based_callee_location() -> None:
    caller = _symbol("caller", "a.py")
    systems = _systems(("a.toml", "A", ("a.py",)))
    snapshot = _snapshot((caller,), (), systems)
    added = compare_systems(snapshot, snapshot, new_call_observations=(_observation(2, "x"),))
    removed = compare_systems(snapshot, snapshot, old_call_observations=(_observation(6, "x"),))

    assert added.call_changes[0].relationship_id.startswith("relationship:call-site:")
    assert _rows(render_text(added)) == ["call added: a.py:3"]
    assert _rows(render_text(removed)) == ["call removed: a.py:7"]
    _assert_rows_iff_exit_one(added, render_text(added))
    _assert_rows_iff_exit_one(removed, render_text(removed))


def test_selected_view_resolves_names_from_the_complete_result_only() -> None:
    complete = _ab_addition()
    selected = filter_system_diff(complete, "A")
    assert all(item.id for item in complete.nodes)
    assert len(selected.nodes) < len(complete.nodes)

    row = "relationship added: send -> receive (calls)"
    assert _rows(render_text(complete), ("relationship ",)) == [row]
    assert _rows(render_text(selected, complete=complete), ("relationship ",)) == [row]
    unresolved = _rows(render_text(selected), ("relationship ",))
    assert unresolved != [row]
    assert "node:comparison:" in unresolved[0]


def test_complete_result_never_changes_rows_status_or_empty_state() -> None:
    systems = _systems(
        ("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)), ("c.toml", "C", ("c.py",))
    )
    source = _symbol("send", "a.py")
    target = _symbol("receive", "b.py")
    complete = compare_systems(
        _snapshot((source, target), (), systems),
        _snapshot((source, target), (_call(source, target),), systems),
    )
    selected = filter_system_diff(complete, "C")

    assert complete.exit_code == 1
    assert _rows(render_text(complete))
    text = render_text(selected, complete=complete)
    assert text.startswith("no system differences\n")
    assert _rows(text) == []
    assert selected.exit_code == 0
    _assert_rows_iff_exit_one(selected, text)
    _assert_rows_iff_exit_one(complete, render_text(complete))


def test_internal_rows_sort_by_status_then_text_while_existing_groups_keep_order() -> None:
    old, old_calls = build_side(False)
    new, new_calls = build_side(True)
    result = compare_systems(
        old, new, old_call_observations=old_calls, new_call_observations=new_calls
    )
    rows = _rows(render_text(result), ("relationship ",))
    assert rows == sorted(rows, key=lambda line: (line.split(" ", 2)[1], line))
    assert rows[0].startswith("relationship added:")
    assert rows[-1].startswith("relationship removed:")
    dependency = [
        line for line in render_text(result).splitlines() if line.startswith("dependency changed:")
    ]
    assert dependency == ["dependency changed: A -> external", "dependency changed: A -> B"]


def test_details_project_stored_evidence_for_each_row_class() -> None:
    text = render_text(_mixed_result(), details=True)

    node = _detail_block(text, "node added: fresh (a.py, function)")
    assert node["old evidence"] == "unavailable"
    assert node["new evidence"] == "unavailable"
    assert node["new"]["node"]["label"] == "fresh"  # type: ignore[index]

    relationship = _detail_block(text, "relationship added: caller -> fresh (calls)")
    assert relationship["old evidence"] == "unavailable"
    evidence = relationship["new evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert evidence[0]["provenance"] == "static-analysis"
    assert evidence[0]["locations"] == [_site(3).to_dict()]

    added_call = _detail_block(text, "call added: caller -> fresh")
    assert added_call["old evidence"] == "unavailable"
    assert added_call["new evidence"] == [_site(3).to_dict()]
    changed_call = _detail_block(text, "call changed: caller -> callee [expression_changed]")
    assert changed_call["old evidence"] == [_site(2).to_dict()]
    assert changed_call["new evidence"] == [_site(2).to_dict()]


def test_details_collect_each_record_of_a_several_record_group() -> None:
    # A several-record group is stored as a tuple of relationship payloads.
    def record(name: str) -> dict[str, object]:
        return {
            "kind": "calls",
            "source": "node:s",
            "target": "node:t",
            "evidence": [{"provenance": "static-analysis", "producer": {"name": name}}],
        }

    def node(identifier: str, label: str) -> system_diff_module.GraphNodeChange:
        return system_diff_module.GraphNodeChange(
            identifier,
            "unchanged",
            after={"node": {"label": label, "node_class": "symbol"}, "system": None},
        )

    result = SystemDiffResult(
        nodes=(node("node:s", "send"), node("node:t", "receive")),
        relationships=(
            system_diff_module.GraphRelationshipChange(
                "relationship:r",
                "node:s",
                "node:t",
                "calls",
                "added",
                ("added",),
                after=(record("first"), record("second")),
            ),
        ),
    )
    block = _detail_block(
        render_text(result, details=True), "relationship added: send -> receive (calls)"
    )
    evidence = block["new evidence"]
    assert isinstance(evidence, list)
    assert [item["producer"]["name"] for item in evidence] == ["first", "second"]


def test_exact_grammar_fixtures_print_rows_exactly_when_they_exit_one() -> None:
    legacy = _symbol("legacy", "legacy.py")
    removed_system = compare_systems(
        _snapshot((legacy,), (), _systems(("legacy.toml", "Legacy", ("legacy.py",)))),
        _snapshot((), (), ()),
    )
    complete = _ab_addition()
    for result, names in (
        (_checkout_notifications_result(), None),
        (complete, None),
        (filter_system_diff(complete, "A"), complete),
        (_ab_removal(), None),
        (removed_system, None),
        (_ab_changed_rows(), None),
        (_membership_endpoint_result(), None),
    ):
        rendered = render_text(result, complete=names)
        assert result.exit_code == 1
        _assert_rows_iff_exit_one(result, rendered)


def _two_system_unavailable_result() -> SystemDiffResult:
    """Two matched calls with a missing fingerprint, one inside A and one inside B."""

    a_caller, a_callee = _symbol("a_caller", "a.py"), _symbol("a_callee", "a.py", line=5)
    b_caller, b_callee = _symbol("b_caller", "b.py"), _symbol("b_callee", "b.py", line=5)
    systems = _systems(("a.toml", "A", ("a.py",)), ("b.toml", "B", ("b.py",)))
    snapshot = _snapshot(
        (a_caller, a_callee, b_caller, b_callee),
        (_site_call(a_caller, a_callee, 2, "a.py"), _site_call(b_caller, b_callee, 2, "b.py")),
        systems,
    )
    return compare_systems(
        snapshot,
        snapshot,
        old_call_observations=(_observation(2, None, "a.py"), _observation(2, None, "b.py")),
        new_call_observations=(_observation(2, "x", "a.py"), _observation(2, "x", "b.py")),
    )


def test_selected_view_keeps_only_its_own_limitations_in_text_and_json() -> None:
    complete = _two_system_unavailable_result()
    by_system = {
        name: {
            item.relationship_id for item in complete.call_changes if name in item.involved_systems
        }
        for name in ("A", "B")
    }
    assert [item.status for item in complete.call_changes] == ["unavailable", "unavailable"]
    assert len(by_system["A"]) == len(by_system["B"]) == 1
    assert len(complete.limitations) == 2
    assert render_text(complete).count("limitation old:") == 2

    selected = filter_system_diff(complete, "A")

    assert {item.relationship_id for item in selected.limitations} == by_system["A"]
    assert render_text(selected).count("limitation old:") == 1
    assert [item["relationship_id"] for item in selected.to_dict()["limitations"]] == list(
        by_system["A"]
    )
    assert len(complete.to_dict()["limitations"]) == 2


def test_no_selection_copies_every_limitation_and_equals_the_complete_result() -> None:
    complete = _two_system_unavailable_result()

    selected = filter_system_diff(complete, None)

    assert selected == complete
    assert selected.limitations == complete.limitations
    assert dump_json(selected.to_dict()) == dump_json(complete.to_dict())
    assert render_text(selected) == render_text(complete)
    assert render_text(selected).count("limitation old:") == 2
