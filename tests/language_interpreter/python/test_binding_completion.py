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


def test_preserving_edge_has_no_destination_or_selector() -> None:
    edge = NormalEdge("entry", "use")

    assert edge.completion is None
    assert edge.source_completions is None
    assert edge.edge_id == ("entry", "use", None)


def test_selector_is_canonical_copied_and_immutable() -> None:
    selectors = [CompletionKey.return_(), CompletionKey.break_("loop"), CompletionKey.normal()]
    edge = NormalEdge(
        "a",
        "b",
        CompletionKey.break_("sink"),
        source_completions=selectors,
    )

    assert edge.source_completions == (
        CompletionKey.break_("loop"),
        CompletionKey.normal(),
        CompletionKey.return_(),
    )
    selectors.append(CompletionKey.exception())
    selectors[0] = CompletionKey.exception()
    assert edge.source_completions == (
        CompletionKey.break_("loop"),
        CompletionKey.normal(),
        CompletionKey.return_(),
    )
    with pytest.raises(AttributeError):
        edge.completion = CompletionKey.return_()
    with pytest.raises(AttributeError):
        edge.source_completions = None
    with pytest.raises(TypeError):
        edge.source_completions[0] = CompletionKey.normal()


def test_edge_construction_rejects_invalid_selector_combinations() -> None:
    with pytest.raises(ValueError):
        NormalEdge("a", "b", CompletionKey.normal())
    with pytest.raises(ValueError):
        NormalEdge("a", "b", source_completions=(CompletionKey.normal(),))
    with pytest.raises(ValueError):
        NormalEdge("a", "b", CompletionKey.normal(), source_completions=())
    with pytest.raises(ValueError):
        NormalEdge("a", "b", source_completions=())
    with pytest.raises(ValueError):
        NormalEdge(
            "a",
            "b",
            CompletionKey.normal(),
            source_completions=(CompletionKey.normal(), CompletionKey.normal()),
        )
    with pytest.raises(TypeError):
        NormalEdge(
            "a",
            "b",
            CompletionKey.normal(),
            source_completions=("normal",),  # type: ignore[arg-type]
        )


def test_edge_id_reports_the_complete_route_identity() -> None:
    explicit = NormalEdge(
        "source",
        "target",
        CompletionKey.break_("loop"),
        source_completions=(CompletionKey.normal(),),
    )
    preserving = NormalEdge("source", "target")

    assert explicit.edge_id == ("source", "target", CompletionKey.break_("loop"))
    assert preserving.edge_id == ("source", "target", None)


def test_block_rejects_fragmented_route_identity_but_accepts_distinct_routes() -> None:
    with pytest.raises(ValueError):
        BasicBlock(
            "block",
            (),
            (
                NormalEdge(
                    "block",
                    "join",
                    CompletionKey.break_("loop"),
                    source_completions=(CompletionKey.normal(),),
                ),
                NormalEdge(
                    "block",
                    "join",
                    CompletionKey.break_("loop"),
                    source_completions=(CompletionKey.return_(),),
                ),
            ),
        )
    combined = BasicBlock(
        "block",
        (),
        (
            NormalEdge(
                "block",
                "join",
                CompletionKey.break_("loop"),
                source_completions=(CompletionKey.return_(), CompletionKey.normal()),
            ),
        ),
    )
    assert combined.edges[0].source_completions == (
        CompletionKey.normal(),
        CompletionKey.return_(),
    )
    distinct = BasicBlock(
        "block",
        (),
        (
            NormalEdge(
                "block",
                "join",
                CompletionKey.break_("loop"),
                source_completions=(CompletionKey.normal(),),
            ),
            NormalEdge(
                "block",
                "join",
                CompletionKey.return_(),
                source_completions=(CompletionKey.normal(),),
            ),
        ),
    )
    assert len(distinct.edges) == 2


def test_completion_map_entries_follow_shared_canonical_order() -> None:
    keyed = CompletionMap(
        {
            CompletionKey.return_(): BindingEnvironment(),
            CompletionKey.break_("other"): BindingEnvironment(),
            CompletionKey.normal(): BindingEnvironment(),
            CompletionKey.break_("loop"): BindingEnvironment(),
        }
    )

    assert tuple(keyed) == (
        CompletionKey.break_("loop"),
        CompletionKey.break_("other"),
        CompletionKey.normal(),
        CompletionKey.return_(),
    )


def test_block_edge_iteration_follows_shared_canonical_order() -> None:
    block = BasicBlock(
        "block",
        (),
        (
            NormalEdge("block", "zzz"),
            NormalEdge(
                "block",
                "aaa",
                CompletionKey.return_(),
                source_completions=(CompletionKey.normal(),),
            ),
            NormalEdge(
                "block",
                "aaa",
                CompletionKey.break_("loop"),
                source_completions=(CompletionKey.normal(),),
            ),
            NormalEdge("block", "mid"),
        ),
    )

    assert tuple(edge.edge_id for edge in block.edges) == (
        ("block", "aaa", CompletionKey.break_("loop")),
        ("block", "aaa", CompletionKey.return_()),
        ("block", "mid", None),
        ("block", "zzz", None),
    )


def test_preserving_edge_forwards_every_completion_channel() -> None:
    slot = BindingSlot("module", "forwarded")
    keys = (
        CompletionKey.normal(),
        CompletionKey.break_("loop"),
        CompletionKey.continue_("loop"),
        CompletionKey.return_(),
        CompletionKey.exception(),
        CompletionKey.invalid_control(),
        CompletionKey.unknown_semantics(),
    )
    tags = (
        "pkg.normal",
        "pkg.break",
        "pkg.continue",
        "pkg.return",
        "pkg.exception",
        "pkg.invalid",
        "pkg.unknown",
    )
    channels = CompletionMap(
        {
            key: BindingEnvironment().transfer(slot, BindingState.imported(tag))
            for key, tag in zip(keys, tags, strict=True)
        }
    )
    blocks = (
        BasicBlock("entry", (), (NormalEdge("entry", "use"),)),
        BasicBlock("use", (UseOperation("read", slot),)),
    )

    result = CompletionSolver(blocks, entry="entry", initial=channels).solve()
    keyed = result.keyed_uses["read"]

    assert set(keyed) == set(keys)
    assert dict(result.keyed_inputs["use"]) == dict(channels)
    for key, tag in zip(keys, tags, strict=True):
        assert keyed[key].get(slot).import_target == tag
    assert result.uses["read"].get(slot).is_uncertain
    assert result.uses["read"].get(slot).targets == frozenset(tags)


def _selected_route_blocks(slot: BindingSlot, destination: CompletionKey) -> tuple[BasicBlock, ...]:
    return (
        BasicBlock(
            "entry",
            (),
            (
                NormalEdge(
                    "entry",
                    "use",
                    destination,
                    source_completions=(CompletionKey.normal(), CompletionKey.return_()),
                ),
            ),
        ),
        BasicBlock("use", (UseOperation("read", slot),)),
    )


def _normal_return_exception_seed(slot: BindingSlot) -> CompletionMap:
    return CompletionMap(
        {
            CompletionKey.normal(): BindingEnvironment().transfer(
                slot, BindingState.imported("pkg.normal")
            ),
            CompletionKey.return_(): BindingEnvironment().transfer(
                slot, BindingState.imported("pkg.return")
            ),
            CompletionKey.exception(): BindingEnvironment().transfer(
                slot, BindingState.imported("pkg.exception")
            ),
        }
    )


def test_selected_normal_and_return_meet_at_one_targeted_break() -> None:
    slot = BindingSlot("module", "selected")
    destination = CompletionKey.break_("target")
    blocks = _selected_route_blocks(slot, destination)
    seeded = _normal_return_exception_seed(slot)

    result = CompletionSolver(blocks, entry="entry", initial=seeded).solve()
    keyed = result.keyed_uses["read"]

    assert set(keyed) == {destination}
    assert keyed[destination].get(slot) == BindingState.uncertain(("pkg.normal", "pkg.return"))
    assert result.uses["read"].get(slot) == BindingState.uncertain(("pkg.normal", "pkg.return"))


def test_selected_contributions_join_before_predecessor_accumulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = BindingSlot("module", "spied")
    destination = CompletionKey.break_("target")
    blocks = _selected_route_blocks(slot, destination)
    seeded = _normal_return_exception_seed(slot)
    calls: list[tuple[CompletionMap, CompletionMap]] = []
    original_join = CompletionMap.join

    def forwarding_join(self: CompletionMap, other: CompletionMap) -> CompletionMap:
        calls.append((self, other))
        return original_join(self, other)

    monkeypatch.setattr(CompletionMap, "join", forwarding_join)
    result = CompletionSolver(blocks, entry="entry", initial=seeded).solve()

    recorded = [map_value for pair in calls for map_value in pair]
    assert all(CompletionKey.exception() not in map_value for map_value in recorded)
    definite_indexes = [
        index
        for index, (_, other) in enumerate(calls)
        if set(other) == {destination} and other[destination].get(slot).is_import
    ]
    assert len(definite_indexes) == 2
    uncertain_index = next(
        index
        for index, (_, other) in enumerate(calls)
        if set(other) == {destination} and other[destination].get(slot).is_uncertain
    )
    assert definite_indexes[1] < uncertain_index
    assert {calls[index][1][destination].get(slot).import_target for index in definite_indexes} == {
        "pkg.normal",
        "pkg.return",
    }
    assert set(result.keyed_uses["read"]) == {destination}


@pytest.mark.parametrize(
    ("selected", "tag"),
    (
        (CompletionKey.normal(), "pkg.normal"),
        (CompletionKey.break_("loop"), "pkg.break.loop"),
        (CompletionKey.continue_("loop"), "pkg.continue.loop"),
        (CompletionKey.return_(), "pkg.return"),
        (CompletionKey.exception(), "pkg.exception"),
        (CompletionKey.invalid_control(), "pkg.invalid"),
        (CompletionKey.unknown_semantics(), "pkg.unknown"),
    ),
)
def test_exact_source_selection_routes_only_the_named_channel(
    selected: CompletionKey, tag: str
) -> None:
    slot = BindingSlot("module", "exact")
    domain = (
        (CompletionKey.normal(), "pkg.normal"),
        (CompletionKey.break_("loop"), "pkg.break.loop"),
        (CompletionKey.break_("other"), "pkg.break.other"),
        (CompletionKey.continue_("loop"), "pkg.continue.loop"),
        (CompletionKey.continue_("other"), "pkg.continue.other"),
        (CompletionKey.return_(), "pkg.return"),
        (CompletionKey.exception(), "pkg.exception"),
        (CompletionKey.invalid_control(), "pkg.invalid"),
        (CompletionKey.unknown_semantics(), "pkg.unknown"),
    )
    destination = CompletionKey.break_("sink")
    seeded = CompletionMap(
        {
            key: BindingEnvironment().transfer(slot, BindingState.imported(channel_tag))
            for key, channel_tag in domain
        }
    )
    blocks = (
        BasicBlock(
            "entry",
            (),
            (NormalEdge("entry", "use", destination, source_completions=(selected,)),),
        ),
        BasicBlock("use", (UseOperation("read", slot),)),
    )

    result = CompletionSolver(blocks, entry="entry", initial=seeded).solve()
    keyed = result.keyed_uses["read"]

    assert set(keyed) == {destination}
    assert keyed[destination].get(slot).is_import
    assert keyed[destination].get(slot).import_target == tag


def test_absent_selected_key_contributes_no_environment_or_output() -> None:
    slot = BindingSlot("module", "absent")
    seeded = CompletionMap(
        {
            CompletionKey.return_(): BindingEnvironment().transfer(
                slot, BindingState.imported("pkg.return")
            ),
            CompletionKey.exception(): BindingEnvironment().transfer(
                slot, BindingState.imported("pkg.exception")
            ),
        }
    )
    blocks = (
        BasicBlock(
            "entry",
            (),
            (
                NormalEdge(
                    "entry",
                    "use",
                    CompletionKey.break_("sink"),
                    source_completions=(CompletionKey.normal(),),
                ),
            ),
        ),
        BasicBlock("use", (UseOperation("read", slot),)),
    )

    result = CompletionSolver(blocks, entry="entry", initial=seeded).solve()

    assert set(result.keyed_outputs["entry"]) == {
        CompletionKey.return_(),
        CompletionKey.exception(),
    }
    assert "read" not in result.keyed_uses
    assert "use" not in result.keyed_inputs


def _two_source_collision_blocks(
    slot: BindingSlot,
    *,
    flip_selector_order: bool = False,
    flip_block_order: bool = False,
) -> tuple[BasicBlock, ...]:
    destination = CompletionKey.break_("loop")
    selectors = (
        (CompletionKey.return_(), CompletionKey.normal())
        if flip_selector_order
        else (CompletionKey.normal(), CompletionKey.return_())
    )
    entry = BasicBlock("entry", (), ("left", "right"))
    left = BasicBlock(
        "left",
        (StateOperation("left-bind", slot, BindingState.imported("pkg.left")),),
        (NormalEdge("left", "join", destination, source_completions=selectors),),
    )
    right = BasicBlock(
        "right",
        (StateOperation("right-bind", slot, BindingState.imported("pkg.right")),),
        (NormalEdge("right", "join", destination, source_completions=selectors),),
    )
    join = BasicBlock("join", (UseOperation("read", slot),))
    if flip_block_order:
        return (entry, right, left, join)
    return (entry, left, right, join)


def _two_channel_seed() -> CompletionMap:
    return CompletionMap(
        {
            CompletionKey.normal(): BindingEnvironment(),
            CompletionKey.return_(): BindingEnvironment(),
        }
    )


def test_two_source_blocks_meet_at_one_destination_key() -> None:
    slot = BindingSlot("module", "collision")
    seeded = _two_channel_seed()

    result = CompletionSolver(
        _two_source_collision_blocks(slot), entry="entry", initial=seeded
    ).solve()
    keyed = result.keyed_uses["read"]

    assert set(keyed) == {CompletionKey.break_("loop")}
    assert keyed[CompletionKey.break_("loop")].get(slot) == BindingState.uncertain(
        ("pkg.left", "pkg.right")
    )
    assert result.uses["read"].get(slot) == BindingState.uncertain(("pkg.left", "pkg.right"))


def test_two_source_collision_snapshots_are_order_invariant() -> None:
    slot = BindingSlot("module", "invariant")
    seeded = _two_channel_seed()
    snapshots = [
        CompletionSolver(
            _two_source_collision_blocks(
                slot, flip_selector_order=flip_selector, flip_block_order=flip_block
            ),
            entry="entry",
            initial=seeded,
        ).solve()
        for flip_selector in (False, True)
        for flip_block in (False, True)
    ]

    for snapshot in snapshots[1:]:
        assert snapshot == snapshots[0]
