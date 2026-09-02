"""Behavioral coverage for the ordered Python binding-flow solver."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from minotaur.language_interpreter.python.binding_flow import (
    BasicBlock,
    BindingEnvironment,
    BindingSlot,
    BindingSolver,
    BindingState,
    NormalEdge,
    ResolutionSnapshot,
    StateOperation,
    UseOperation,
)


def test_ordered_blocks_produce_exact_inputs_outputs_and_use_snapshots() -> None:
    slot = BindingSlot("module", "helper")
    blocks = (
        BasicBlock(
            "entry",
            (StateOperation("bind", slot, BindingState.imported("pkg.helper")),),
            (NormalEdge("entry", "use"),),
        ),
        BasicBlock("use", (UseOperation("read", slot),), ("exit",)),
        BasicBlock(
            "exit",
            (StateOperation("shadow", slot, BindingState.non_import()),),
        ),
    )

    result = BindingSolver(blocks, entry="entry").solve()

    assert result.inputs["entry"] == BindingEnvironment()
    assert result.outputs["entry"].get(slot) == BindingState.imported("pkg.helper")
    assert result.inputs["use"].get(slot) == BindingState.imported("pkg.helper")
    assert result.uses["read"].get(slot) == BindingState.imported("pkg.helper")
    assert result.outputs["exit"].get(slot).is_non_import


def test_backedge_is_revisited_until_fixed_point_instead_of_first_predecessor() -> None:
    slot = BindingSlot("module", "value")
    blocks = (
        BasicBlock("entry", (), ("loop",)),
        BasicBlock(
            "loop",
            (
                UseOperation("loop-read", slot),
                StateOperation("loop-import", slot, BindingState.imported("pkg.value")),
            ),
            ("loop", "exit"),
        ),
        BasicBlock("exit", (UseOperation("exit-read", slot),)),
    )

    result = BindingSolver(blocks, entry="entry").solve()

    # The loop's initial unbound input and its backedge import meet to
    # uncertainty.  A one-predecessor pass would incorrectly retain unbound.
    assert result.inputs["loop"].get(slot) == BindingState.uncertain()
    assert result.uses["loop-read"].get(slot) == BindingState.uncertain()
    assert result.uses["exit-read"].get(slot) == BindingState.imported("pkg.value")


def _bounded_reference(
    blocks: tuple[BasicBlock, ...], entry: str, rounds: int = 32
) -> ResolutionSnapshot:
    """A small independent round-based reference for ordinary flow."""

    by_id = {block.block_id: block for block in blocks}
    inputs: dict[str, BindingEnvironment] = {}
    outputs: dict[str, BindingEnvironment] = {}
    uses: dict[str, BindingEnvironment] = {}
    for _ in range(rounds):
        changed = False
        for block_id in sorted(by_id):
            predecessors = sorted(
                predecessor.block_id
                for predecessor in blocks
                if any(edge.target == block_id for edge in predecessor.normal_edges)
            )
            incoming = [outputs[pred] for pred in predecessors if pred in outputs]
            if block_id == entry:
                incoming.insert(0, BindingEnvironment())
            if not incoming:
                continue
            block_input = incoming[0]
            for state in incoming[1:]:
                block_input = block_input.meet(state)
            environment = block_input
            for operation in by_id[block_id].operations:
                if isinstance(operation, UseOperation):
                    uses[operation.operation_id] = environment
                else:
                    environment = environment.transfer(
                        operation.slot, operation.state, operation.target
                    )
            if inputs.get(block_id) != block_input or outputs.get(block_id) != environment:
                changed = True
            inputs[block_id] = block_input
            outputs[block_id] = environment
        if not changed:
            break
    else:
        raise AssertionError("reference model exceeded its bounded rounds")
    return ResolutionSnapshot(inputs, outputs, uses)


def test_repeated_solves_and_insertion_order_permutation_are_equal() -> None:
    slot = BindingSlot("module", "name")
    original = (
        BasicBlock("z", (UseOperation("z-read", slot),)),
        BasicBlock(
            "entry", (StateOperation("set", slot, BindingState.imported("pkg.name")),), ("z",)
        ),
    )
    permuted = tuple(reversed(original))

    first = BindingSolver(original, entry="entry").solve()
    second = BindingSolver(original, entry="entry").solve()
    third = BindingSolver(permuted, entry="entry").solve()

    assert first == second == third
    assert first == _bounded_reference(original, "entry")


def test_joined_backedge_matches_reference_and_is_order_independent() -> None:
    slot = BindingSlot("module", "value")
    blocks = (
        BasicBlock("entry", (), ("left", "right")),
        BasicBlock(
            "left",
            (StateOperation("left-import", slot, BindingState.imported("pkg.left")),),
            ("join",),
        ),
        BasicBlock(
            "right",
            (StateOperation("right-import", slot, BindingState.imported("pkg.right")),),
            ("join",),
        ),
        BasicBlock(
            "join",
            (UseOperation("join-read", slot),),
            ("loop", "exit"),
        ),
        BasicBlock(
            "loop",
            (
                UseOperation("loop-read", slot),
                StateOperation("loop-import", slot, BindingState.imported("pkg.loop")),
            ),
            ("loop", "exit"),
        ),
        BasicBlock("exit", (UseOperation("exit-read", slot),)),
    )

    expected = _bounded_reference(blocks, "entry")
    result = BindingSolver(tuple(reversed(blocks)), entry="entry").solve()

    assert result == expected
    assert result.uses["join-read"].get(slot) == BindingState.uncertain(("pkg.left", "pkg.right"))
    assert result.uses["loop-read"].get(slot) == BindingState.uncertain(
        ("pkg.left", "pkg.loop", "pkg.right")
    )
    assert result.uses["exit-read"].get(slot) == BindingState.uncertain(
        ("pkg.left", "pkg.loop", "pkg.right")
    )


def test_initial_environment_alias_is_used_without_aliasing_the_caller() -> None:
    slot = BindingSlot("module", "seed")
    initial = BindingEnvironment().transfer(slot, BindingState.imported("pkg.seed"))
    block = BasicBlock("entry", (UseOperation("seed-read", slot),))

    result = BindingSolver((block,), initial_environment=initial).solve()

    assert result.inputs["entry"] == initial
    assert result.uses["seed-read"] == initial


def test_snapshots_and_builders_are_immutable_and_do_not_hold_solver_state() -> None:
    slot = BindingSlot("module", "name")
    operation = StateOperation("set", slot, BindingState.non_import())
    block = BasicBlock("entry", (operation,))
    result = BindingSolver((block,), entry="entry").solve()

    assert isinstance(result.inputs, MappingProxyType)
    assert isinstance(result.outputs, MappingProxyType)
    assert isinstance(result.uses, MappingProxyType)
    with pytest.raises(TypeError):
        result.outputs["entry"] = BindingEnvironment()  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        operation.operation_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        block.operations = ()  # type: ignore[misc]
    assert not hasattr(operation, "environment")
    assert not hasattr(operation, "apply")
