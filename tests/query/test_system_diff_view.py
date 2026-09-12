"""Natural-trigger and edge-case proof for the pure system comparison view."""

from __future__ import annotations

import difflib
from dataclasses import replace

import pytest
from test_system_diff import _call, _reference, _snapshot, _symbol, _systems, _upstream

from minotaur.graph_model.evidence import Evidence, Producer
from minotaur.graph_model.provenance import Provenance
from minotaur.graph_model.relationship import Relationship
from minotaur.query import system_diff as system_diff_module
from minotaur.query.render import dump_json
from minotaur.query.system import ReportingSnapshot
from minotaur.query.system_diff import SystemChange, SystemDiffResult, compare_systems
from minotaur.query.system_diff_view import (
    filter_system_diff,
    render_context_text,
    render_json,
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
        ],
    )
    _assert_exact_compact(
        _ab_addition(),
        [
            "surface added: B b.py.receive",
            "consumer added: B <- a.py",
            "dependency added: A -> B",
            "boundary added: A.send -> B.receive (calls)",
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
        ],
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
    _assert_exact_compact(result, expected_lines)

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
    assert render_json(selected) == dump_json(selected.to_dict())
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
    assert render_json(first_result) == render_json(second_result)
    assert render_text(first_result, details=True) == render_text(second_result, details=True)


def test_canonical_dumper_receives_exact_to_dict_and_status_stays_typed(monkeypatch) -> None:
    result = filter_system_diff(_ab_addition(), "B")
    expected_bytes = dump_json(result.to_dict())
    assert render_json(result) == expected_bytes
    changed, exit_code = result.changed, result.exit_code
    sentinel = {"sentinel": True}
    observed: list[object] = []

    monkeypatch.setattr(SystemDiffResult, "to_dict", lambda _self: sentinel)

    def observe(payload: object) -> str:
        observed.append(payload)
        return "sentinel-json\n"

    monkeypatch.setattr("minotaur.query.system_diff_view.dump_json", observe)
    assert render_json(result) == "sentinel-json\n"
    assert observed == [sentinel]
    assert result.changed is changed
    assert result.exit_code == exit_code
