"""Behavioral coverage for fresh activation entry and pointwise exit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from minotaur.language_interpreter.python.binding_flow import (
    ActivationShape,
    BindingEnvironment,
    BindingSlot,
    BindingState,
    CompletionKey,
    CompletionMap,
    EnterActivation,
    ExitActivation,
    enter_activation,
    exit_activation,
    join_activation_shapes,
)


def test_shape_is_typed_immutable_and_order_independent() -> None:
    local = BindingSlot("function", "local")
    second_local = BindingSlot("function", "second")
    delegated = BindingSlot("module", "outer")
    second_delegated = BindingSlot("module", "second_outer")
    shape = ActivationShape((local, second_local), (delegated, second_delegated))
    reordered = ActivationShape((second_local, local), (second_delegated, delegated))
    concise = ActivationShape(locals=(local, second_local), delegated=(delegated, second_delegated))

    assert shape == reordered
    assert shape == concise
    assert shape.locals == tuple(sorted((local, second_local), key=lambda slot: slot.slot_id))
    assert shape.delegated == tuple(
        sorted((delegated, second_delegated), key=lambda slot: slot.slot_id)
    )
    assert shape.slots == tuple(
        sorted((local, second_local, delegated, second_delegated), key=lambda slot: slot.slot_id)
    )
    with pytest.raises(AttributeError):
        shape.local_slots += (BindingSlot("function", "other"),)  # type: ignore[misc]

    with pytest.raises(ValueError):
        ActivationShape((local, local), ())
    with pytest.raises(ValueError):
        ActivationShape((local,), (local,))
    with pytest.raises(TypeError):
        ActivationShape(("not-a-slot",), ())  # type: ignore[arg-type]


def test_entry_resets_locals_and_retains_delegated_outer_writes() -> None:
    local = BindingSlot("function", "local")
    delegated = BindingSlot("module", "outer")
    shape = ActivationShape(local_slots=(local,), delegated_slots=(delegated,))
    stale = (
        BindingEnvironment()
        .transfer(local, BindingState.imported("old.local"))
        .transfer(delegated, BindingState.imported("module.outer"))
    )

    entered = EnterActivation(shape).apply(stale)
    assert isinstance(entered, BindingEnvironment)
    assert entered.get(local).is_unbound
    assert entered.get(delegated) == BindingState.imported("module.outer")
    assert stale.get(local).import_target == "old.local"

    rebound = entered.transfer(local, BindingState.imported("new.local"))
    reentered = enter_activation(rebound, shape)
    assert isinstance(reentered, BindingEnvironment)
    assert reentered.get(local).is_unbound
    assert reentered.get(delegated).import_target == "module.outer"


def test_reentry_resets_locals_pointwise_on_every_completion_channel() -> None:
    local = BindingSlot("function", "local")
    delegated = BindingSlot("module", "outer")
    shape = ActivationShape((local,), (delegated,))
    keys = (
        CompletionKey.normal(),
        CompletionKey.break_("loop"),
        CompletionKey.continue_("loop"),
        CompletionKey.return_(),
        CompletionKey.exception(),
        CompletionKey.invalid_control(),
        CompletionKey.unknown_semantics(),
    )
    stale = CompletionMap(
        {
            key: BindingEnvironment()
            .transfer(local, BindingState.imported(f"stale.{index}"))
            .transfer(delegated, BindingState.imported(f"outer.{index}"))
            for index, key in enumerate(keys)
        }
    )

    entered = EnterActivation(shape).apply(stale)
    assert isinstance(entered, CompletionMap)
    rebound = CompletionMap(
        {
            key: environment.transfer(local, BindingState.imported(f"rebound.{index}"))
            for index, (key, environment) in enumerate(entered.items())
        }
    )
    reentered = enter_activation(rebound, shape)
    assert isinstance(reentered, CompletionMap)
    assert set(reentered) == set(keys)
    for index, key in enumerate(keys):
        assert reentered[key].get(local).is_unbound
        assert reentered[key].get(delegated).import_target == f"outer.{index}"


def test_exit_projects_locals_from_every_completion_channel() -> None:
    local = BindingSlot("function", "local")
    delegated = BindingSlot("module", "outer")
    unrelated = BindingSlot("module", "other")
    shape = ActivationShape((local,), (delegated,))
    keys = (
        CompletionKey.normal(),
        CompletionKey.break_("loop"),
        CompletionKey.continue_("loop"),
        CompletionKey.return_(),
        CompletionKey.exception(),
        CompletionKey.invalid_control(),
        CompletionKey.unknown_semantics(),
    )
    pending = CompletionMap(
        {
            key: BindingEnvironment()
            .transfer(local, BindingState.imported(f"local.{index}"))
            .transfer(delegated, BindingState.imported(f"outer.{index}"))
            .transfer(unrelated, BindingState.non_import())
            for index, key in enumerate(keys)
        }
    )

    projected = ExitActivation(shape).apply(pending)
    assert isinstance(projected, CompletionMap)
    assert set(projected) == set(keys)
    assert isinstance(projected.entries, MappingProxyType)
    for index, key in enumerate(keys):
        environment = projected[key]
        assert local not in environment.bindings
        assert environment.get(local).is_unbound
        assert environment.get(delegated).import_target == f"outer.{index}"
        assert environment.get(unrelated).is_non_import

    # The function helper has the same pointwise contract as the operation.
    assert exit_activation(pending, shape) == projected


def test_activation_shapes_must_match_at_joins() -> None:
    local = BindingSlot("function", "local")
    other = BindingSlot("function", "other")
    shape = ActivationShape((local,), ())
    unequal = ActivationShape((other,), ())

    assert shape.join(shape) == shape
    assert join_activation_shapes(shape, shape) == shape
    with pytest.raises(ValueError, match="unequal shapes"):
        shape.join(unequal)
    with pytest.raises(ValueError, match="unequal shapes"):
        join_activation_shapes(shape, unequal)
    with pytest.raises(ValueError):
        join_activation_shapes()


def test_activation_operations_reject_untyped_values() -> None:
    shape = ActivationShape()
    with pytest.raises(TypeError):
        EnterActivation(shape).apply(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ExitActivation(shape).apply(object())  # type: ignore[arg-type]


def test_existing_interpreter_does_not_import_or_reference_dormant_kernel() -> None:
    from minotaur.language_interpreter.python import interpreter

    source = Path(interpreter.__file__).read_text(encoding="utf-8")
    assert "binding_flow" not in source
    assert not any("binding_flow" in name for name in interpreter.__dict__)


def test_public_python_import_and_analysis_leave_kernel_dormant(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    probe = """
import sys
from pathlib import Path
from minotaur.language_interpreter.python import analyze_python_workspace

result = analyze_python_workspace(Path(sys.argv[1]))
assert result.diagnostics == ()
assert not any(name.endswith(".binding_flow") for name in sys.modules)
print("dormant")
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "dormant"
