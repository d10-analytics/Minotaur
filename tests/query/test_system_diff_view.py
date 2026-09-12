"""Behavioral proof for pure views over a complete system comparison."""

from __future__ import annotations

import difflib

import pytest
from test_system_diff import _call, _snapshot, _symbol, _systems

from minotaur.query import system_diff as system_diff_module
from minotaur.query.system import ReportingSnapshot
from minotaur.query.system_diff import SystemDiffResult, compare_systems
from minotaur.query.system_diff_view import (
    render_context_text,
    render_details,
    render_json,
    render_text,
    select_system,
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


def test_selection_replaces_from_one_complete_result_and_never_reacquires(monkeypatch) -> None:
    complete = _checkout_notifications_result()
    original = complete.to_dict()

    def fail(*_args, **_kwargs):
        raise AssertionError("a pure view must not reacquire comparison inputs")

    monkeypatch.setattr(ReportingSnapshot, "report", fail)
    monkeypatch.setattr(system_diff_module, "compare_systems", fail)

    checkout = select_system(complete, "Checkout")
    notifications = select_system(complete, "Notifications")
    direct_notifications = select_system(complete, "Notifications")
    repeated = select_system(complete, "Notifications")

    assert checkout is not complete
    assert all("Notifications" not in change.involved_systems for change in checkout.differences)
    assert notifications == direct_notifications == repeated
    assert any(
        change.domain == "boundary" and "Payments" in change.involved_systems
        for change in notifications.differences
    )
    assert any(
        change.domain == "membership" and "Notifications" in change.involved_systems
        for change in notifications.differences
    )
    assert select_system(complete) is complete
    assert complete.to_dict() == original
    assert notifications.old_coverage == complete.old_coverage
    assert notifications.new_coverage == complete.new_coverage
    assert notifications.old_selection == complete.old_selection
    assert notifications.new_selection == complete.new_selection


def test_selection_resolves_complete_union_and_exact_unknown_suggestions() -> None:
    result = SystemDiffResult(
        old_system_names=("billing", "orders"),
        new_system_names=("billing", "orders", "shipping"),
    )
    with pytest.raises(UnknownSystem) as error:
        select_system(result, "oder")
    assert error.value.name == "oder"
    assert error.value.nearest == tuple(
        difflib.get_close_matches("oder", sorted({"billing", "orders", "shipping"}), n=5)
    )
    assert str(error.value) == "unknown system: oder; nearest systems: orders"

    with pytest.raises(UnknownSystem) as unmatched:
        select_system(SystemDiffResult(), "missing")
    assert unmatched.value.nearest == ()
    assert str(unmatched.value) == "unknown system: missing"

    names = tuple(f"billing-{index:02d}" for index in range(8))
    capped = SystemDiffResult(old_system_names=names)
    with pytest.raises(UnknownSystem) as capped_error:
        select_system(capped, "billing-0")
    assert capped_error.value.nearest == tuple(
        difflib.get_close_matches("billing-0", sorted(names), n=5)
    )
    assert len(capped_error.value.nearest) == 5


def test_text_has_fixed_categories_context_and_details() -> None:
    result = _checkout_notifications_result()
    text = render_text(result)
    assert render_context_text(result) == "".join(
        line for line in text.splitlines(keepends=True) if line.startswith(("old ", "new "))
    )
    lines = text.splitlines()
    assert lines[-4:] == [
        "old coverage " + text.split("old coverage ", 1)[1].splitlines()[0],
        "new coverage " + text.split("new coverage ", 1)[1].splitlines()[0],
        "old selection " + text.split("old selection ", 1)[1].splitlines()[0],
        "new selection " + text.split("new selection ", 1)[1].splitlines()[0],
    ]
    assert any(line.startswith("boundary added: Payments.send_receipt") for line in lines)
    assert any(line.startswith("membership changed: notifications.py") for line in lines)

    details = render_details(select_system(result, "Notifications"))
    assert '"old":null' in details
    assert '"new":' in details
    assert "involved_systems" in details

    no_change = render_text(SystemDiffResult())
    assert no_change.startswith("no changes\nold coverage ")
    assert no_change.index("old coverage") < no_change.index("new coverage")
    assert no_change.index("new coverage") < no_change.index("old selection")
    assert no_change.index("old selection") < no_change.index("new selection")


def test_json_uses_canonical_serializer_and_does_not_own_status(monkeypatch) -> None:
    result = select_system(_checkout_notifications_result(), "Notifications")
    expected = result.to_dict()
    observed: list[object] = []

    def sentinel(payload: object) -> str:
        observed.append(payload)
        return "sentinel-json\n"

    monkeypatch.setattr("minotaur.query.system_diff_view.dump_json", sentinel)
    assert render_json(result) == "sentinel-json\n"
    assert observed == [expected]
    assert result.changed is True
    assert result.exit_code == 1
