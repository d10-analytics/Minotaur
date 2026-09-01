"""Behavioral coverage for the immutable Python binding-flow state kernel."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from minotaur.language_interpreter.python.binding_flow import (
    BindingEnvironment,
    BindingProvenance,
    BindingSlot,
    BindingState,
    PrefixBinding,
    PrefixEnvironment,
    lookup_prefix,
    meet_states,
)


def test_slots_are_static_identity_and_do_not_alias() -> None:
    module_value = BindingSlot("module", "value")
    function_value = BindingSlot("function", "value")
    second_module_value = BindingSlot("module", "value", 1)

    assert module_value != function_value
    assert module_value != second_module_value
    assert module_value.slot_id == ("module", "value", 0)
    with pytest.raises(FrozenInstanceError):
        module_value.name = "other"  # type: ignore[misc]


def test_meet_table_preserves_definite_imports_and_collapses_conflicts() -> None:
    states = (
        BindingState.unbound(),
        BindingState.imported("pkg.left"),
        BindingState.imported("pkg.right"),
        BindingState.non_import(),
        BindingState.uncertain(("pkg.left",)),
    )

    assert meet_states(states[0], states[0]) is states[0]
    assert meet_states(states[1], states[1]) == states[1]
    assert meet_states(states[1], states[2]) == BindingState.uncertain(("pkg.left", "pkg.right"))
    assert meet_states(states[1], states[3]) == BindingState.uncertain()
    assert meet_states(states[0], states[3]) == BindingState.uncertain()
    assert meet_states(states[4], states[2]) == BindingState.uncertain(("pkg.left", "pkg.right"))
    assert meet_states(states[2], states[4]) == meet_states(states[4], states[2])
    assert meet_states(states[1], states[2]).provenance is BindingProvenance.UNCERTAIN
    assert meet_states(states[1], states[2]).target is None


def test_environment_transfer_invalidation_and_reimport_are_persistent() -> None:
    slot = BindingSlot("module", "helper")
    original = BindingEnvironment()
    imported = original.transfer(slot, BindingState.imported("library.helper"))
    invalidated = imported.invalidate(slot)
    recovered = invalidated.reimport(slot, "library.helper")

    assert original.get(slot).is_unbound
    assert imported.get(slot).import_target == "library.helper"
    assert invalidated.get(slot).is_non_import
    assert recovered.get(slot).import_target == "library.helper"
    assert imported.get(slot).is_import
    assert recovered != invalidated


def test_caller_mappings_and_uncertainty_sequences_are_copied() -> None:
    slot = BindingSlot("module", "value")
    targets = ["pkg.one"]
    caller_map = {slot: BindingState.uncertain(targets)}
    environment = BindingEnvironment(caller_map)
    targets.append("pkg.two")
    caller_map[slot] = BindingState.non_import()

    assert environment.get(slot).possibilities == frozenset(("pkg.one",))
    assert environment.get(slot).is_uncertain
    with pytest.raises(TypeError):
        environment.bindings[slot] = BindingState.non_import()  # type: ignore[index]


def test_prefix_tombstone_blocks_parent_recall_without_affecting_siblings() -> None:
    state = PrefixEnvironment().import_prefix("pkg", "pkg")
    state = state.import_prefix("pkg.mod", "pkg.mod").invalidate("pkg.mod.member")

    assert state.lookup("pkg.mod.member.call") is None
    assert state.lookup("pkg.mod.other") == "pkg.mod.other"
    assert state.lookup("pkg.sibling") == "pkg.sibling"
    assert state.lookup("missing.child") is None


def test_prefix_invalidation_is_negative_and_exact_reimport_recovers_one_path() -> None:
    state = PrefixEnvironment().import_prefix("pkg", "pkg")
    state = state.invalidate("pkg.mod.member").invalidate("pkg.other.member")
    recovered = state.reimport("pkg.mod.member", "pkg.mod.member")

    assert state.lookup("pkg.mod.member.call") is None
    assert state.lookup("pkg.other.member.call") is None
    assert state.lookup("pkg.mod.sibling") == "pkg.mod.sibling"
    assert recovered.lookup("pkg.mod.member.call") == "pkg.mod.member.call"
    assert recovered.lookup("pkg.other.member.call") is None


def test_environment_owns_prefix_state_and_module_level_lookup_is_read_only() -> None:
    source = {"pkg.mod": "pkg.mod"}
    environment = BindingEnvironment(prefixes=PrefixEnvironment(source))
    source["pkg.extra"] = "pkg.extra"
    changed = environment.import_prefix("pkg.new", "pkg.new")

    assert lookup_prefix(environment, "pkg.extra") is None
    assert environment.lookup_prefix("pkg.mod") == "pkg.mod"
    assert environment.lookup_prefix("pkg.new") is None
    assert changed.lookup_prefix("pkg.new") == "pkg.new"
    assert environment.prefixes.entries["pkg.mod"] == PrefixBinding.imported("pkg.mod")


def test_prefix_entries_are_immutable_and_tombstones_are_explicit() -> None:
    entries = {"pkg.mod": PrefixBinding.deleted()}
    state = PrefixEnvironment(entries)
    entries.clear()

    assert state.tombstones == frozenset(("pkg.mod",))
    assert state.lookup("pkg.mod.child") is None
    with pytest.raises(TypeError):
        state.entries["pkg"] = PrefixBinding.imported("pkg")  # type: ignore[index]
