"""Immutable state used by the Python binding-flow analysis.

This module deliberately contains no syntax, graph, or control-flow code.  A
``BindingSlot`` identifies one statically allocated lexical location.  The
state associated with that location is a small, immutable lattice that keeps
an import's qualified target separate from the fact that a location was
bound.  ``BindingEnvironment`` combines those slots with qualified-prefix
state; both are persistent values, so a caller can safely retain a snapshot
while constructing the next one.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class BindingProvenance(str, Enum):
    """The finite provenance carried by one lexical binding."""

    UNBOUND = "unbound"
    IMPORT = "import"
    NON_IMPORT = "non-import"
    UNCERTAIN = "uncertain"

    # Descriptive spellings make the lattice vocabulary explicit while
    # retaining the concise wire values used by the implementation.
    DEFINITE_IMPORT = "import"
    DEFINITE_NON_IMPORT = "non-import"


# Short aliases are useful to downstream private packages and keep the enum
# name unambiguous when imported alongside graph-model provenance.
BindingKind = BindingProvenance
Provenance = BindingProvenance


@dataclass(frozen=True, slots=True)
class BindingSlot:
    """Static lexical-storage identity.

    ``scope`` is an owner chosen by the lowering layer (for example,
    ``"module"`` or a function's stable lexical label); it is not an AST,
    graph, frame, predecessor, or execution-path identity.  ``ordinal`` is
    available for two same-named slots in one scope, while remaining part of
    the static identity.
    """

    scope: str
    name: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or not self.scope:
            raise ValueError("binding slot scope must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("binding slot name must be a non-empty string")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool):
            raise TypeError("binding slot ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("binding slot ordinal must be non-negative")

    @property
    def scope_id(self) -> str:
        """The stable scope component of this identity."""

        return self.scope

    @property
    def owner(self) -> str:
        """Alias for the static scope owner."""

        return self.scope

    @property
    def slot_id(self) -> tuple[str, str, int]:
        """A hashable identity useful as a deterministic map key."""

        return (self.scope, self.name, self.ordinal)


def _coerce_provenance(value: BindingProvenance | str) -> BindingProvenance:
    if isinstance(value, BindingProvenance):
        return value
    try:
        return BindingProvenance(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"unknown binding provenance: {value!r}") from error


@dataclass(frozen=True, slots=True)
class BindingState:
    """One immutable value in the binding provenance lattice.

    For a definite import, ``target`` is the qualified imported name.  An
    uncertain state retains a normalized finite set of possible targets when
    they are known, but never presents one candidate as definite.  This keeps
    distinct imports distinct across transfer and prevents provenance from
    being silently guessed at a join.
    """

    provenance: BindingProvenance
    target: str | None = None
    possibilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        provenance = _coerce_provenance(self.provenance)
        object.__setattr__(self, "provenance", provenance)
        if self.target is not None and (not isinstance(self.target, str) or not self.target):
            raise ValueError("binding target must be a non-empty string or None")
        if not isinstance(self.possibilities, frozenset):
            try:
                possible = frozenset(self.possibilities)
            except TypeError as error:
                raise TypeError("binding possibilities must be finite strings") from error
            object.__setattr__(self, "possibilities", possible)
        if any(not isinstance(value, str) or not value for value in self.possibilities):
            raise ValueError("binding possibilities must be finite non-empty strings")

        if provenance is BindingProvenance.IMPORT:
            if self.target is None or self.possibilities:
                raise ValueError("definite import state requires exactly one target")
        elif provenance is BindingProvenance.UNCERTAIN:
            if self.target is not None:
                raise ValueError("uncertain state cannot carry a definite target")
        elif self.target is not None or self.possibilities:
            raise ValueError("non-import states cannot carry import targets")

    @classmethod
    def unbound(cls) -> BindingState:
        return _UNBOUND

    @classmethod
    def imported(cls, target: str) -> BindingState:
        return cls(BindingProvenance.IMPORT, target=target)

    definite_import = imported

    @classmethod
    def non_import(cls) -> BindingState:
        return _NON_IMPORT

    definite_non_import = non_import

    @classmethod
    def uncertain(cls, targets: Iterable[str] = ()) -> BindingState:
        return _uncertain(targets)

    @property
    def is_import(self) -> bool:
        return self.provenance is BindingProvenance.IMPORT

    @property
    def is_definite_import(self) -> bool:
        return self.is_import

    @property
    def is_unbound(self) -> bool:
        return self.provenance is BindingProvenance.UNBOUND

    @property
    def is_non_import(self) -> bool:
        return self.provenance is BindingProvenance.NON_IMPORT

    @property
    def is_uncertain(self) -> bool:
        return self.provenance is BindingProvenance.UNCERTAIN

    @property
    def import_target(self) -> str | None:
        return self.target if self.is_import else None

    @property
    def targets(self) -> frozenset[str]:
        """All finite candidates, including a definite import's target."""

        if self.is_import and self.target is not None:
            return frozenset((self.target,))
        return self.possibilities


_UNBOUND = BindingState(BindingProvenance.UNBOUND)
_NON_IMPORT = BindingState(BindingProvenance.NON_IMPORT)


def _uncertain(targets: Iterable[str]) -> BindingState:
    normalized = frozenset(targets)
    return BindingState(BindingProvenance.UNCERTAIN, possibilities=normalized)


def _as_state(value: BindingState | BindingProvenance | str) -> BindingState:
    if isinstance(value, BindingState):
        return value
    provenance = _coerce_provenance(value)
    if provenance is BindingProvenance.UNBOUND:
        return _UNBOUND
    if provenance is BindingProvenance.NON_IMPORT:
        return _NON_IMPORT
    if provenance is BindingProvenance.UNCERTAIN:
        return _uncertain(())
    raise ValueError("a definite import state requires a qualified target")


def meet_states(
    left: BindingState | BindingProvenance | str,
    right: BindingState | BindingProvenance | str,
) -> BindingState:
    """Meet two states, preserving uncertainty and qualified targets.

    The operation is commutative and deterministic.  A join of unequal
    definite imports never selects the first predecessor; it becomes finite
    uncertainty instead.
    """

    first = _as_state(left)
    second = _as_state(right)
    if first == second:
        return first
    if first.is_uncertain or second.is_uncertain:
        # An uncertainty with no candidates means the analysis has no
        # provenance at all.  A later definite candidate cannot make that
        # unknown state definite or even narrow it to one possibility.
        if (first.is_uncertain and not first.possibilities) or (
            second.is_uncertain and not second.possibilities
        ):
            return _uncertain(())
        possible = set(first.possibilities) | set(second.possibilities)
        if first.is_import and first.target is not None:
            possible.add(first.target)
        if second.is_import and second.target is not None:
            possible.add(second.target)
        return _uncertain(possible)
    if first.is_import and second.is_import:
        assert first.target is not None and second.target is not None
        return _uncertain((first.target, second.target))
    return _uncertain(())


def meet(
    left: BindingState | BindingEnvironment | PrefixEnvironment,
    right: BindingState | BindingEnvironment | PrefixEnvironment,
) -> BindingState | BindingEnvironment | PrefixEnvironment:
    """Meet either scalar binding states or one of the persistent maps."""

    if isinstance(left, BindingState) and isinstance(right, BindingState):
        return meet_states(left, right)
    if isinstance(left, BindingEnvironment) and isinstance(right, BindingEnvironment):
        return left.meet(right)
    if isinstance(left, PrefixEnvironment) and isinstance(right, PrefixEnvironment):
        return left.meet(right)
    raise TypeError("meet operands must have the same binding-flow type")


@dataclass(frozen=True, slots=True)
class PrefixBinding:
    """An immutable qualified-prefix outcome."""

    target: str | None = None
    tombstone: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tombstone, bool):
            raise TypeError("prefix tombstone must be a boolean")
        if self.target is None and not self.tombstone:
            raise ValueError("prefix binding requires a target or tombstone")
        if self.tombstone and self.target is not None:
            raise ValueError("a prefix tombstone cannot carry a target")
        if self.target is not None and (not isinstance(self.target, str) or not self.target):
            raise ValueError("prefix target must be a non-empty string or None")

    @classmethod
    def imported(cls, target: str) -> PrefixBinding:
        return cls(target=target)

    @classmethod
    def deleted(cls) -> PrefixBinding:
        return cls(tombstone=True)

    @property
    def is_tombstone(self) -> bool:
        return self.tombstone


def _parts(path: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(path, str):
        result = tuple(part for part in path.split(".") if part)
    else:
        result = tuple(path)
    if not result or any(not isinstance(part, str) or not part for part in result):
        raise ValueError("qualified prefix must contain non-empty components")
    return result


def _path_text(path: tuple[str, ...]) -> str:
    return ".".join(path)


@dataclass(frozen=True, slots=True)
class PrefixEnvironment:
    """Persistent longest-prefix import map with explicit tombstones."""

    entries: Mapping[str, PrefixBinding] = MappingProxyType({})

    def __post_init__(self) -> None:
        normalized: dict[str, PrefixBinding] = {}
        for raw_path, raw_value in dict(self.entries).items():
            path = _path_text(_parts(raw_path))
            if isinstance(raw_value, PrefixBinding):
                value = raw_value
            elif raw_value is None:
                value = PrefixBinding.deleted()
            elif isinstance(raw_value, str):
                value = PrefixBinding.imported(raw_value)
            else:
                raise TypeError("prefix entries must be PrefixBinding or target strings")
            normalized[path] = value
        object.__setattr__(self, "entries", MappingProxyType(normalized))

    @property
    def tombstones(self) -> frozenset[str]:
        return frozenset(path for path, value in self.entries.items() if value.tombstone)

    @property
    def imports(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                path: value.target
                for path, value in self.entries.items()
                if not value.tombstone and value.target is not None
            }
        )

    def lookup(self, path: str | Iterable[str]) -> str | None:
        """Resolve a qualified name, honoring the nearest tombstone first."""

        requested = _parts(path)
        for length in range(len(requested), 0, -1):
            prefix = _path_text(requested[:length])
            value = self.entries.get(prefix)
            if value is None:
                continue
            if value.tombstone:
                return None
            assert value.target is not None
            suffix = requested[length:]
            return ".".join((value.target, *suffix)) if suffix else value.target
        return None

    resolve = lookup

    def import_prefix(
        self, path: str | Iterable[str], target: str | None = None
    ) -> PrefixEnvironment:
        key = _path_text(_parts(path))
        new_entries = dict(self.entries)
        new_entries[key] = PrefixBinding.imported(target or key)
        return PrefixEnvironment(new_entries)

    def reimport(self, path: str | Iterable[str], target: str | None = None) -> PrefixEnvironment:
        """Recover exactly one path while retaining all unrelated tombstones."""

        return self.import_prefix(path, target)

    def invalidate(self, path: str | Iterable[str]) -> PrefixEnvironment:
        key = _path_text(_parts(path))
        new_entries = dict(self.entries)
        new_entries[key] = PrefixBinding.deleted()
        return PrefixEnvironment(new_entries)

    def meet(self, other: PrefixEnvironment) -> PrefixEnvironment:
        if not isinstance(other, PrefixEnvironment):
            raise TypeError("prefix meet requires another PrefixEnvironment")
        keys = set(self.entries) | set(other.entries)
        result: dict[str, PrefixBinding] = {}
        for key in sorted(keys):
            left = self.entries.get(key)
            right = other.entries.get(key)
            if left == right and left is not None:
                result[key] = left
            elif left is not None or right is not None:
                # Any disagreement (including one predecessor with no entry)
                # is negative at this exact path.  Dropping a child here
                # would let a parent import resurrect it during lookup.
                result[key] = PrefixBinding.deleted()
        return PrefixEnvironment(result)


# Longer descriptive alias retained for callers that name the state directly.
QualifiedPrefixState = PrefixEnvironment
PrefixState = PrefixEnvironment
PrefixMap = PrefixEnvironment
QualifiedPrefixMap = PrefixEnvironment


@dataclass(frozen=True, slots=True)
class BindingEnvironment:
    """Immutable slot and qualified-prefix state."""

    bindings: Mapping[BindingSlot, BindingState] = MappingProxyType({})
    prefixes: PrefixEnvironment = PrefixEnvironment()

    def __post_init__(self) -> None:
        normalized: dict[BindingSlot, BindingState] = {}
        for slot, state in dict(self.bindings).items():
            if not isinstance(slot, BindingSlot):
                raise TypeError("binding keys must be BindingSlot values")
            normalized[slot] = _as_state(state)
        object.__setattr__(self, "bindings", MappingProxyType(normalized))
        if not isinstance(self.prefixes, PrefixEnvironment):
            object.__setattr__(self, "prefixes", PrefixEnvironment(self.prefixes))

    def get(self, slot: BindingSlot) -> BindingState:
        return self.bindings.get(slot, _UNBOUND)

    lookup = get

    def transfer(
        self,
        slot: BindingSlot,
        state: BindingState | BindingProvenance | str,
        target: str | None = None,
    ) -> BindingEnvironment:
        """Set one slot to a copied immutable state."""

        state_value = BindingState.imported(target) if target is not None else _as_state(state)
        new_bindings = dict(self.bindings)
        new_bindings[slot] = state_value
        return BindingEnvironment(new_bindings, self.prefixes)

    set = transfer

    def invalidate(self, slot: BindingSlot) -> BindingEnvironment:
        return self.transfer(slot, _NON_IMPORT)

    def reimport(
        self,
        slot: BindingSlot,
        target: str,
    ) -> BindingEnvironment:
        return self.transfer(slot, BindingState.imported(target))

    def import_prefix(
        self,
        path: str | Iterable[str],
        target: str | None = None,
    ) -> BindingEnvironment:
        return BindingEnvironment(self.bindings, self.prefixes.import_prefix(path, target))

    def invalidate_prefix(self, path: str | Iterable[str]) -> BindingEnvironment:
        return BindingEnvironment(self.bindings, self.prefixes.invalidate(path))

    def lookup_prefix(self, path: str | Iterable[str]) -> str | None:
        return self.prefixes.lookup(path)

    def meet(self, other: BindingEnvironment) -> BindingEnvironment:
        if not isinstance(other, BindingEnvironment):
            raise TypeError("binding meet requires another BindingEnvironment")
        slots = set(self.bindings) | set(other.bindings)
        merged = {
            slot: meet_states(self.get(slot), other.get(slot))
            for slot in sorted(slots, key=lambda value: value.slot_id)
        }
        return BindingEnvironment(merged, self.prefixes.meet(other.prefixes))


Environment = BindingEnvironment
BindingFlowState = BindingEnvironment


@dataclass(frozen=True, slots=True)
class NormalEdge:
    """A typed ordinary-flow edge between two ordered blocks."""

    source: str
    target: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("normal edge source must be a non-empty string")
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("normal edge target must be a non-empty string")

    @property
    def edge_id(self) -> tuple[str, str]:
        """The stable identity of this edge."""

        return (self.source, self.target)


@dataclass(frozen=True, slots=True)
class StateOperation:
    """An immutable description of one slot-state transfer.

    The operation deliberately has no environment argument or environment
    method.  Only :class:`BindingSolver` interprets it, which keeps builders
    declarative and makes propagation a single-owner concern.
    """

    operation_id: str
    slot: BindingSlot
    state: BindingState | BindingProvenance | str
    target: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("state operation ID must be a non-empty string")
        if not isinstance(self.slot, BindingSlot):
            raise TypeError("state operation slot must be a BindingSlot")
        normalized = (
            BindingState.imported(self.target) if self.target is not None else _as_state(self.state)
        )
        object.__setattr__(self, "state", normalized)


@dataclass(frozen=True, slots=True)
class UseOperation:
    """An immutable description of a binding use-site snapshot."""

    operation_id: str
    slot: BindingSlot

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("use operation ID must be a non-empty string")
        if not isinstance(self.slot, BindingSlot):
            raise TypeError("use operation slot must be a BindingSlot")


# Descriptive aliases allow callers to choose the vocabulary used by their
# lowering layer without introducing another operation implementation.
BindingOperation = StateOperation
TransferOperation = StateOperation
UseSiteOperation = UseOperation
Operation = StateOperation | UseOperation


def _normal_edges(block_id: str, edges: Sequence[NormalEdge | str]) -> tuple[NormalEdge, ...]:
    result: list[NormalEdge] = []
    for edge in edges:
        if isinstance(edge, NormalEdge):
            if edge.source != block_id:
                raise ValueError("normal edge source does not match its block")
            result.append(edge)
        elif isinstance(edge, str):
            result.append(NormalEdge(block_id, edge))
        else:
            raise TypeError("normal edges must be NormalEdge or target strings")
    if len({edge.target for edge in result}) != len(result):
        raise ValueError("a block cannot contain duplicate normal edges")
    return tuple(sorted(result, key=lambda edge: edge.target))


@dataclass(frozen=True, slots=True)
class BasicBlock:
    """An ordered, declarative block in the ordinary binding-flow IR."""

    block_id: str
    operations: tuple[StateOperation | UseOperation, ...] = ()
    normal_edges: tuple[NormalEdge, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.block_id, str) or not self.block_id:
            raise ValueError("block ID must be a non-empty string")
        try:
            operations = tuple(self.operations)
        except TypeError as error:
            raise TypeError("block operations must be finite") from error
        if any(
            not isinstance(operation, (StateOperation, UseOperation)) for operation in operations
        ):
            raise TypeError("block operations must be state or use operations")
        if len({operation.operation_id for operation in operations}) != len(operations):
            raise ValueError("operation IDs must be unique within a block")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(
            self,
            "normal_edges",
            _normal_edges(self.block_id, tuple(self.normal_edges)),
        )

    @property
    def id(self) -> str:
        """Short alias for the stable block identity."""

        return self.block_id

    @property
    def edges(self) -> tuple[NormalEdge, ...]:
        return self.normal_edges


OrderedBlock = BasicBlock
Block = BasicBlock


def _immutable_mapping(
    values: Mapping[str, BindingEnvironment],
) -> Mapping[str, BindingEnvironment]:
    return MappingProxyType({key: values[key] for key in sorted(values)})


@dataclass(frozen=True, slots=True)
class ResolutionSnapshot:
    """The immutable result of one complete ordinary-flow solve."""

    inputs: Mapping[str, BindingEnvironment]
    outputs: Mapping[str, BindingEnvironment]
    uses: Mapping[str, BindingEnvironment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _immutable_mapping(self.inputs))
        object.__setattr__(self, "outputs", _immutable_mapping(self.outputs))
        object.__setattr__(self, "uses", _immutable_mapping(self.uses))

    @property
    def block_inputs(self) -> Mapping[str, BindingEnvironment]:
        return self.inputs

    @property
    def block_outputs(self) -> Mapping[str, BindingEnvironment]:
        return self.outputs

    @property
    def use_snapshots(self) -> Mapping[str, BindingEnvironment]:
        return self.uses

    def input_for(self, block_id: str) -> BindingEnvironment | None:
        return self.inputs.get(block_id)

    def output_for(self, block_id: str) -> BindingEnvironment | None:
        return self.outputs.get(block_id)


class BindingSolver:
    """Compute ordinary binding flow to a deterministic fixed point."""

    def __init__(
        self,
        blocks: Iterable[BasicBlock] | Mapping[str, BasicBlock],
        entry: str | None = None,
        initial: BindingEnvironment | None = None,
        *,
        max_steps: int | None = None,
    ) -> None:
        if isinstance(blocks, Mapping):
            source = tuple(blocks.values())
            if any(key != block.block_id for key, block in blocks.items()):
                raise ValueError("block mapping keys must match block IDs")
        else:
            source = tuple(blocks)
        if not source or any(not isinstance(block, BasicBlock) for block in source):
            raise ValueError("solver requires at least one BasicBlock")
        if len({block.block_id for block in source}) != len(source):
            raise ValueError("block IDs must be unique")
        operation_ids = [
            operation.operation_id for block in source for operation in block.operations
        ]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("operation IDs must be unique across the flow")
        self._blocks = {block.block_id: block for block in source}
        self._ordered_ids = tuple(sorted(self._blocks))
        self.entry = entry if entry is not None else self._ordered_ids[0]
        if self.entry not in self._blocks:
            raise ValueError(f"unknown solver entry block: {self.entry!r}")
        for block in source:
            for edge in block.normal_edges:
                if edge.target not in self._blocks:
                    raise ValueError(f"normal edge targets unknown block: {edge.target!r}")
        self.initial = initial if initial is not None else BindingEnvironment()
        if not isinstance(self.initial, BindingEnvironment):
            raise TypeError("solver initial state must be a BindingEnvironment")
        default_steps = max(64, len(self._blocks) * 64)
        self.max_steps = default_steps if max_steps is None else max_steps
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool):
            raise TypeError("solver max_steps must be an integer")
        if self.max_steps <= 0:
            raise ValueError("solver max_steps must be positive")

    def solve(self) -> ResolutionSnapshot:
        """Run a bounded deterministic worklist and return immutable snapshots."""

        predecessors: dict[str, tuple[str, ...]] = {
            block_id: tuple(
                sorted(
                    block.block_id
                    for block in self._blocks.values()
                    if any(edge.target == block_id for edge in block.normal_edges)
                )
            )
            for block_id in self._ordered_ids
        }
        outputs: dict[str, BindingEnvironment] = {}
        inputs: dict[str, BindingEnvironment] = {}
        uses: dict[str, BindingEnvironment] = {}
        worklist: deque[str] = deque((self.entry,))
        queued = {self.entry}
        steps = 0

        while worklist:
            block_id = worklist.popleft()
            queued.discard(block_id)
            steps += 1
            if steps > self.max_steps:
                raise RuntimeError("binding-flow worklist did not converge within max_steps")

            incoming = [outputs[pred] for pred in predecessors[block_id] if pred in outputs]
            if block_id == self.entry:
                incoming.insert(0, self.initial)
            if not incoming:
                continue
            block_input = incoming[0]
            for predecessor_state in incoming[1:]:
                block_input = block_input.meet(predecessor_state)
            prior_input = inputs.get(block_id)
            if prior_input == block_input and block_id in outputs:
                continue
            inputs[block_id] = block_input
            environment = block_input
            for operation in self._blocks[block_id].operations:
                if isinstance(operation, UseOperation):
                    uses[operation.operation_id] = environment
                else:
                    environment = environment.transfer(
                        operation.slot, operation.state, operation.target
                    )
            if outputs.get(block_id) == environment:
                continue
            outputs[block_id] = environment
            for edge in self._blocks[block_id].normal_edges:
                if edge.target not in queued:
                    worklist.append(edge.target)
                    queued.add(edge.target)

        return ResolutionSnapshot(inputs, outputs, uses)


def solve(
    blocks: Iterable[BasicBlock] | Mapping[str, BasicBlock],
    entry: str | None = None,
    initial: BindingEnvironment | None = None,
    *,
    max_steps: int | None = None,
) -> ResolutionSnapshot:
    """Convenience entry point for the sole ordinary-flow solver."""

    return BindingSolver(blocks, entry, initial, max_steps=max_steps).solve()


def transfer(
    environment: BindingEnvironment,
    slot: BindingSlot,
    state: BindingState | BindingProvenance | str,
    target: str | None = None,
) -> BindingEnvironment:
    return environment.transfer(slot, state, target)


def invalidate(environment: BindingEnvironment, slot: BindingSlot) -> BindingEnvironment:
    return environment.invalidate(slot)


def reimport(
    environment: BindingEnvironment,
    slot: BindingSlot,
    target: str,
) -> BindingEnvironment:
    return environment.reimport(slot, target)


def lookup_prefix(
    environment: PrefixEnvironment | BindingEnvironment,
    path: str | Iterable[str],
) -> str | None:
    if isinstance(environment, BindingEnvironment):
        return environment.lookup_prefix(path)
    return environment.lookup(path)


__all__ = [
    "BindingEnvironment",
    "BindingFlowState",
    "BindingOperation",
    "BindingKind",
    "BindingProvenance",
    "BindingSlot",
    "BindingState",
    "Environment",
    "BasicBlock",
    "Block",
    "BindingSolver",
    "NormalEdge",
    "Operation",
    "PrefixBinding",
    "PrefixEnvironment",
    "PrefixMap",
    "PrefixState",
    "Provenance",
    "QualifiedPrefixState",
    "QualifiedPrefixMap",
    "OrderedBlock",
    "ResolutionSnapshot",
    "StateOperation",
    "TransferOperation",
    "UseOperation",
    "UseSiteOperation",
    "invalidate",
    "lookup_prefix",
    "meet",
    "meet_states",
    "reimport",
    "transfer",
]
