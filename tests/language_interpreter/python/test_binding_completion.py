"""Behavioral coverage for keyed completion flow and cleanup."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from minotaur.language_interpreter.python.binding_flow import (
    BasicBlock,
    BindingEnvironment,
    BindingSlot,
    BindingState,
    CleanupRegion,
    CompletionKey,
    CompletionKind,
    CompletionMap,
    CompletionSolver,
    NormalEdge,
    StateOperation,
    UseOperation,
    cross_key_snapshot,
    resume_cleanup,
)


def test_completion_keys_keep_targets_separate_from_slot_state() -> None:
    assert CompletionKey.normal() != CompletionKey.return_()
    assert CompletionKey.break_("outer") != CompletionKey.break_("inner")
    assert CompletionKey.break_("outer").kind is CompletionKind.BREAK
    assert CompletionKey.continue_("loop").region == "loop"
    assert CompletionKey.invalid_control().is_terminal
    assert CompletionKey.unknown_semantics().is_terminal
    with pytest.raises(ValueError):
        CompletionKey(CompletionKind.BREAK)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        CompletionKey(CompletionKind.RETURN, "not-a-region")  # type: ignore[call-arg]


def test_keyed_join_is_pointwise_and_immutable() -> None:
    slot = BindingSlot("module", "value")
    normal = BindingEnvironment().transfer(slot, BindingState.imported("pkg.normal"))
    returned = BindingEnvironment().transfer(slot, BindingState.imported("pkg.returned"))
    left = CompletionMap(
        {
            CompletionKey.normal(): normal,
            CompletionKey.return_(): returned,
        }
    )
    right = CompletionMap(
        {
            CompletionKey.normal(): BindingEnvironment(),
            CompletionKey.return_(): returned,
            CompletionKey.exception(): normal,
        }
    )

    joined = left.join(right)

    assert isinstance(joined.entries, MappingProxyType)
    assert set(joined) == {
        CompletionKey.normal(),
        CompletionKey.return_(),
        CompletionKey.exception(),
    }
    assert joined[CompletionKey.normal()].get(slot).is_uncertain
    assert joined[CompletionKey.return_()] == returned
    assert joined[CompletionKey.exception()] == normal
    with pytest.raises(TypeError):
        joined.entries[CompletionKey.normal()] = normal  # type: ignore[index]


def test_keyed_join_keeps_all_channel_and_target_identities_separate() -> None:
    slot = BindingSlot("module", "channel_value")
    keys = (
        CompletionKey.normal(),
        CompletionKey.break_("outer"),
        CompletionKey.break_("inner"),
        CompletionKey.continue_("outer"),
        CompletionKey.continue_("inner"),
        CompletionKey.return_(),
        CompletionKey.exception(),
        CompletionKey.invalid_control(),
        CompletionKey.unknown_semantics(),
    )
    left = CompletionMap(
        {
            key: BindingEnvironment().transfer(slot, BindingState.imported(f"left.{index}"))
            for index, key in enumerate(keys)
        }
    )
    right = CompletionMap(
        {
            CompletionKey.break_("outer"): BindingEnvironment().transfer(
                slot, BindingState.imported("right.outer")
            ),
            CompletionKey.break_("inner"): BindingEnvironment().transfer(
                slot, BindingState.imported("right.inner")
            ),
        }
    )

    joined = left.join(right)

    assert set(joined) == set(keys)
    assert joined[CompletionKey.break_("outer")].get(slot) == BindingState.uncertain(
        ("left.1", "right.outer")
    )
    assert joined[CompletionKey.break_("inner")].get(slot) == BindingState.uncertain(
        ("left.2", "right.inner")
    )
    for key in keys:
        if key not in {CompletionKey.break_("outer"), CompletionKey.break_("inner")}:
            assert joined[key] == left[key]


def test_cleanup_resumes_all_channels_and_changes_each_environment_pointwise() -> None:
    slot = BindingSlot("module", "cleanup_marker")
    pending = CompletionMap(
        {
            CompletionKey.normal(): BindingEnvironment(),
            CompletionKey.break_("loop"): BindingEnvironment(),
            CompletionKey.continue_("loop"): BindingEnvironment(),
            CompletionKey.return_(): BindingEnvironment(),
            CompletionKey.exception(): BindingEnvironment(),
            CompletionKey.invalid_control(): BindingEnvironment(),
            CompletionKey.unknown_semantics(): BindingEnvironment(),
        }
    )

    result = CleanupRegion((StateOperation("mark", slot, BindingState.non_import()),)).apply(
        pending
    )

    assert set(result) == set(pending)
    for key in pending:
        assert result[key].get(slot).is_non_import


def test_cleanup_pointwise_retains_each_channel_environment() -> None:
    source_slot = BindingSlot("module", "source")
    marker = BindingSlot("module", "cleanup_marker")
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
            key: BindingEnvironment().transfer(source_slot, BindingState.imported(f"pkg.{index}"))
            for index, key in enumerate(keys)
        }
    )

    result = CleanupRegion((StateOperation("mark", marker, BindingState.non_import()),)).execute(
        pending
    )

    assert set(result) == set(keys)
    for index, key in enumerate(keys):
        assert result[key].get(source_slot).import_target == f"pkg.{index}"
        assert result[key].get(marker).is_non_import


def test_cleanup_terminal_override_and_exception_only_suppression() -> None:
    pending = CompletionMap(
        {
            CompletionKey.return_(): BindingEnvironment(),
            CompletionKey.exception(): BindingEnvironment(),
        }
    )
    marker = BindingSlot("module", "marker")
    normal_cleanup = CleanupRegion(
        (StateOperation("cleanup", marker, BindingState.non_import()),)
    ).execute(CompletionMap.normal())

    resumed = resume_cleanup(pending, normal_cleanup)
    assert set(resumed) == set(pending)
    assert resumed[CompletionKey.return_()].get(marker).is_non_import
    assert resumed[CompletionKey.exception()].get(marker).is_non_import

    suppressed = resume_cleanup(pending, normal_cleanup, suppress_exceptions=True)
    assert set(suppressed) == {CompletionKey.normal(), CompletionKey.return_()}
    with pytest.raises(ValueError):
        CleanupRegion().apply(pending, override=CompletionKey.normal())

    overridden = CleanupRegion().apply(pending, override=CompletionKey.return_())
    assert set(overridden) == {CompletionKey.return_()}


def test_cleanup_terminal_result_supersedes_pending_channels() -> None:
    slot = BindingSlot("module", "cleanup_result")
    pending = CompletionMap(
        {
            CompletionKey.normal(): BindingEnvironment().transfer(
                slot, BindingState.imported("body.normal")
            ),
            CompletionKey.break_("loop"): BindingEnvironment().transfer(
                slot, BindingState.imported("body.break")
            ),
            CompletionKey.return_(): BindingEnvironment().transfer(
                slot, BindingState.imported("body.return")
            ),
        }
    )
    cleanup_return = BindingEnvironment().transfer(slot, BindingState.imported("cleanup.return"))

    result = resume_cleanup(pending, CompletionMap({CompletionKey.return_(): cleanup_return}))

    assert set(result) == {CompletionKey.return_()}
    assert result[CompletionKey.return_()].get(slot) == cleanup_return.get(slot)


def test_completion_solver_preserves_targeted_channels_and_shared_use_meet() -> None:
    slot = BindingSlot("module", "value")
    blocks = (
        BasicBlock("entry", (), ("left", "right")),
        BasicBlock(
            "left",
            (StateOperation("left-bind", slot, BindingState.imported("pkg.left")),),
            (
                NormalEdge(
                    "left",
                    "join",
                    CompletionKey.break_("loop"),
                    source_completions=(CompletionKey.normal(),),
                ),
            ),
        ),
        BasicBlock(
            "right",
            (StateOperation("right-bind", slot, BindingState.imported("pkg.right")),),
            (
                NormalEdge(
                    "right",
                    "join",
                    CompletionKey.return_(),
                    source_completions=(CompletionKey.normal(),),
                ),
            ),
        ),
        BasicBlock("join", (UseOperation("read", slot),)),
    )

    result = CompletionSolver(blocks, entry="entry").solve()
    keyed = result.keyed_uses["read"]

    assert set(keyed) == {CompletionKey.break_("loop"), CompletionKey.return_()}
    assert keyed[CompletionKey.break_("loop")].get(slot).import_target == "pkg.left"
    assert keyed[CompletionKey.return_()].get(slot).import_target == "pkg.right"
    assert result.uses["read"].get(slot) == BindingState.uncertain(("pkg.left", "pkg.right"))
    assert cross_key_snapshot(keyed) == result.uses["read"]
