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
from collections.abc import Iterable, Iterator, Mapping, Sequence
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


@dataclass(frozen=True, slots=True, init=False)
class ActivationShape:
    """Static lexical slots owned by one activation.

    Local slots are storage for the activation and are reset on every entry.
    Delegated slots belong to an enclosing owner and therefore pass through
    entry and exit.  The shape contains only static slot identities; it does
    not encode an execution path, frame, or runtime instance.
    """

    local_slots: tuple[BindingSlot, ...] = ()
    delegated_slots: tuple[BindingSlot, ...] = ()

    def __init__(
        self,
        local_slots: Iterable[BindingSlot] = (),
        delegated_slots: Iterable[BindingSlot] = (),
        *,
        locals: Iterable[BindingSlot] | None = None,
        delegated: Iterable[BindingSlot] | None = None,
    ) -> None:
        """Build a shape using either long or concise slot names."""

        if locals is not None:
            if tuple(local_slots):
                raise ValueError("specify either local_slots or locals, not both")
            local_slots = locals
        if delegated is not None:
            if tuple(delegated_slots):
                raise ValueError("specify either delegated_slots or delegated, not both")
            delegated_slots = delegated
        object.__setattr__(self, "local_slots", tuple(local_slots))
        object.__setattr__(self, "delegated_slots", tuple(delegated_slots))
        self.__post_init__()

    def __post_init__(self) -> None:
        try:
            local_slots = tuple(self.local_slots)
            delegated_slots = tuple(self.delegated_slots)
        except TypeError as error:
            raise TypeError("activation slots must be finite") from error
        if any(not isinstance(slot, BindingSlot) for slot in local_slots + delegated_slots):
            raise TypeError("activation slots must be BindingSlot values")
        if len(set(local_slots)) != len(local_slots):
            raise ValueError("activation local slots must be unique")
        if len(set(delegated_slots)) != len(delegated_slots):
            raise ValueError("activation delegated slots must be unique")
        if set(local_slots).intersection(delegated_slots):
            raise ValueError("activation slots cannot be both local and delegated")
        object.__setattr__(
            self,
            "local_slots",
            tuple(sorted(local_slots, key=lambda slot: slot.slot_id)),
        )
        object.__setattr__(
            self,
            "delegated_slots",
            tuple(sorted(delegated_slots, key=lambda slot: slot.slot_id)),
        )

    @property
    def locals(self) -> tuple[BindingSlot, ...]:
        """Slots reset when this activation is entered."""

        return self.local_slots

    @property
    def delegated(self) -> tuple[BindingSlot, ...]:
        """Slots owned by an enclosing activation."""

        return self.delegated_slots

    @property
    def slots(self) -> tuple[BindingSlot, ...]:
        """All statically named slots in deterministic order."""

        return tuple(sorted(self.local_slots + self.delegated_slots, key=lambda slot: slot.slot_id))

    def require_equal(self, other: ActivationShape) -> ActivationShape:
        """Validate that two joins refer to exactly the same static shape."""

        if not isinstance(other, ActivationShape):
            raise TypeError("activation shape join requires another ActivationShape")
        if self != other:
            raise ValueError("cannot join activations with unequal shapes")
        return self

    def join(self, other: ActivationShape) -> ActivationShape:
        """Return this shape after equal-shape validation."""

        return self.require_equal(other)

    validate_equal = require_equal


Activation = ActivationShape


def join_activation_shapes(*shapes: ActivationShape) -> ActivationShape:
    """Validate and return one shape for a set of activation predecessors."""

    if not shapes:
        raise ValueError("an activation shape join requires at least one shape")
    first = shapes[0]
    if not isinstance(first, ActivationShape):
        raise TypeError("activation shapes must be ActivationShape values")
    for shape in shapes[1:]:
        first.require_equal(shape)
    return first


class CompletionKind(str, Enum):
    """The finite kinds of an owner-region completion."""

    NORMAL = "normal"
    BREAK = "break"
    CONTINUE = "continue"
    RETURN = "return"
    EXCEPTION = "exception"
    INVALID_CONTROL = "invalid-control"
    UNKNOWN_SEMANTICS = "unknown-semantics"


def _coerce_completion_kind(value: CompletionKind | str) -> CompletionKind:
    if isinstance(value, CompletionKind):
        return value
    try:
        return CompletionKind(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"unknown completion kind: {value!r}") from error


@dataclass(frozen=True, slots=True)
class CompletionKey:
    """Routing identity independent of a lexical binding slot.

    ``target`` is meaningful only for targeted ``break`` and ``continue``
    completions.  It names a stable region allocated by a lowerer; it is
    never an execution-path or predecessor identity.
    """

    kind: CompletionKind
    target: str | None = None

    def __post_init__(self) -> None:
        kind = _coerce_completion_kind(self.kind)
        object.__setattr__(self, "kind", kind)
        if kind in (CompletionKind.BREAK, CompletionKind.CONTINUE):
            if not isinstance(self.target, str) or not self.target:
                raise ValueError(f"{kind.value} completion requires a target region")
        elif self.target is not None:
            raise ValueError(f"{kind.value} completion cannot carry a target region")

    @classmethod
    def normal(cls) -> CompletionKey:
        return cls(CompletionKind.NORMAL)

    @classmethod
    def break_(cls, target: str) -> CompletionKey:
        return cls(CompletionKind.BREAK, target)

    @classmethod
    def continue_(cls, target: str) -> CompletionKey:
        return cls(CompletionKind.CONTINUE, target)

    @classmethod
    def returned(cls) -> CompletionKey:
        return cls(CompletionKind.RETURN)

    @classmethod
    def return_(cls) -> CompletionKey:
        return cls.returned()

    @classmethod
    def exception(cls) -> CompletionKey:
        return cls(CompletionKind.EXCEPTION)

    @classmethod
    def invalid_control(cls) -> CompletionKey:
        return cls(CompletionKind.INVALID_CONTROL)

    @classmethod
    def unknown_semantics(cls) -> CompletionKey:
        return cls(CompletionKind.UNKNOWN_SEMANTICS)

    @property
    def is_normal(self) -> bool:
        return self.kind is CompletionKind.NORMAL

    @property
    def is_exception(self) -> bool:
        return self.kind is CompletionKind.EXCEPTION

    @property
    def is_terminal(self) -> bool:
        return not self.is_normal

    @property
    def region(self) -> str | None:
        """Alias for the optional stable loop/region target."""

        return self.target


# Explicit spellings are convenient for callers that use a channel enum or a
# key enum in annotations.  They intentionally denote the same value type.
CompletionChannel = CompletionKind
CompletionType = CompletionKind


def _coerce_completion_key(value: CompletionKey | CompletionKind | str) -> CompletionKey:
    if isinstance(value, CompletionKey):
        return value
    return CompletionKey(_coerce_completion_kind(value))


@dataclass(frozen=True, slots=True)
class CompletionMap(Mapping[CompletionKey, BindingEnvironment]):
    """Immutable map of completion routing keys to environments."""

    entries: Mapping[CompletionKey, BindingEnvironment] = MappingProxyType({})

    def __post_init__(self) -> None:
        normalized: dict[CompletionKey, BindingEnvironment] = {}
        for raw_key, environment in dict(self.entries).items():
            key = _coerce_completion_key(raw_key)
            if not isinstance(environment, BindingEnvironment):
                raise TypeError("completion values must be BindingEnvironment values")
            normalized[key] = environment
        object.__setattr__(
            self,
            "entries",
            MappingProxyType(
                dict(
                    sorted(
                        normalized.items(),
                        key=lambda item: (item[0].kind.value, item[0].target or ""),
                    )
                )
            ),
        )

    @classmethod
    def normal(cls, environment: BindingEnvironment | None = None) -> CompletionMap:
        return cls({CompletionKey.normal(): environment or BindingEnvironment()})

    @classmethod
    def from_environment(cls, environment: BindingEnvironment) -> CompletionMap:
        return cls.normal(environment)

    def __getitem__(self, key: CompletionKey) -> BindingEnvironment:
        return self.entries[_coerce_completion_key(key)]

    def __iter__(self) -> Iterator[CompletionKey]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def channels(self) -> Mapping[CompletionKey, BindingEnvironment]:
        return self.entries

    @property
    def values_by_key(self) -> Mapping[CompletionKey, BindingEnvironment]:
        return self.entries

    def join(self, other: CompletionMap) -> CompletionMap:
        """Join maps pointwise without ever merging distinct routing keys."""

        if not isinstance(other, CompletionMap):
            raise TypeError("completion join requires another CompletionMap")
        result: dict[CompletionKey, BindingEnvironment] = dict(self.entries)
        for key, environment in other.entries.items():
            if key in result:
                result[key] = result[key].meet(environment)
            else:
                result[key] = environment
        return CompletionMap(result)

    meet = join

    def with_channel(self, key: CompletionKey, environment: BindingEnvironment) -> CompletionMap:
        updated = dict(self.entries)
        updated[key] = environment
        return CompletionMap(updated)

    def resume(
        self,
        cleanup_result: CompletionMap,
        *,
        suppress_exceptions: bool = False,
    ) -> CompletionMap:
        return resume_cleanup(self, cleanup_result, suppress_exceptions=suppress_exceptions)

    def cleanup(
        self,
        operations: Iterable[StateOperation] = (),
        *,
        override: CompletionKey | None = None,
        suppress_exceptions: bool = False,
    ) -> CompletionMap:
        return CleanupRegion(tuple(operations)).apply(
            self, override=override, suppress_exceptions=suppress_exceptions
        )


KeyedEnvironment = CompletionMap
CompletionEnvironment = CompletionMap
FlowMap = CompletionMap


def _enter_environment(
    environment: BindingEnvironment, shape: ActivationShape
) -> BindingEnvironment:
    """Reset local storage while preserving every non-local binding."""

    bindings = dict(environment.bindings)
    for slot in shape.local_slots:
        bindings[slot] = BindingState.unbound()
    return BindingEnvironment(bindings, environment.prefixes)


def _exit_environment(
    environment: BindingEnvironment, shape: ActivationShape
) -> BindingEnvironment:
    """Project one activation's local storage out of its environment."""

    bindings = {
        slot: state for slot, state in environment.bindings.items() if slot not in shape.local_slots
    }
    return BindingEnvironment(bindings, environment.prefixes)


@dataclass(frozen=True, slots=True)
class EnterActivation:
    """Create fresh local state for one statically described activation."""

    shape: ActivationShape

    def __post_init__(self) -> None:
        if not isinstance(self.shape, ActivationShape):
            raise TypeError("activation entry requires an ActivationShape")

    def apply(
        self, value: BindingEnvironment | CompletionMap
    ) -> BindingEnvironment | CompletionMap:
        if isinstance(value, BindingEnvironment):
            return _enter_environment(value, self.shape)
        if isinstance(value, CompletionMap):
            return CompletionMap(
                {
                    key: _enter_environment(environment, self.shape)
                    for key, environment in value.items()
                }
            )
        raise TypeError("activation entry requires a BindingEnvironment or CompletionMap")

    enter = apply
    execute = apply

    def __call__(
        self, value: BindingEnvironment | CompletionMap
    ) -> BindingEnvironment | CompletionMap:
        return self.apply(value)


@dataclass(frozen=True, slots=True)
class ExitActivation:
    """Project activation locals out of every completion channel on exit."""

    shape: ActivationShape

    def __post_init__(self) -> None:
        if not isinstance(self.shape, ActivationShape):
            raise TypeError("activation exit requires an ActivationShape")

    def apply(
        self, value: BindingEnvironment | CompletionMap
    ) -> BindingEnvironment | CompletionMap:
        if isinstance(value, BindingEnvironment):
            return _exit_environment(value, self.shape)
        if isinstance(value, CompletionMap):
            return CompletionMap(
                {
                    key: _exit_environment(environment, self.shape)
                    for key, environment in value.items()
                }
            )
        raise TypeError("activation exit requires a BindingEnvironment or CompletionMap")

    project = apply
    execute = apply

    def __call__(
        self, value: BindingEnvironment | CompletionMap
    ) -> BindingEnvironment | CompletionMap:
        return self.apply(value)


def enter_activation(
    value: BindingEnvironment | CompletionMap,
    shape: ActivationShape,
) -> BindingEnvironment | CompletionMap:
    """Enter an activation, resetting its local slots."""

    return EnterActivation(shape).apply(value)


def exit_activation(
    value: BindingEnvironment | CompletionMap,
    shape: ActivationShape,
) -> BindingEnvironment | CompletionMap:
    """Exit an activation, projecting its local slots pointwise."""

    return ExitActivation(shape).apply(value)


def project_activation(
    value: BindingEnvironment | CompletionMap,
    shape: ActivationShape,
) -> BindingEnvironment | CompletionMap:
    """Alias for :func:`exit_activation`."""

    return exit_activation(value, shape)


@dataclass(frozen=True, slots=True)
class NormalEdge:
    """A typed ordinary-flow edge between two ordered blocks."""

    source: str
    target: str
    completion: CompletionKey | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("normal edge source must be a non-empty string")
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("normal edge target must be a non-empty string")
        if self.completion is not None and not isinstance(self.completion, CompletionKey):
            object.__setattr__(self, "completion", _coerce_completion_key(self.completion))

    @property
    def edge_id(self) -> tuple[str, str]:
        """The stable identity of this edge."""

        return (self.source, self.target)

    @property
    def key(self) -> CompletionKey | None:
        """Explicit completion routing key, or ``None`` to preserve one."""

        return self.completion

    @property
    def completion_key(self) -> CompletionKey | None:
        return self.completion


CompletionEdge = NormalEdge


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

    @property
    def id(self) -> str:
        """Stable operation identity."""

        return self.operation_id


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

    @property
    def id(self) -> str:
        """Stable operation identity."""

        return self.operation_id

    @property
    def site_id(self) -> str:
        """The use-site identity recorded in a resolution snapshot."""

        return self.operation_id


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
    if len({(edge.target, edge.completion) for edge in result}) != len(result):
        raise ValueError("a block cannot contain duplicate normal edges")
    return tuple(
        sorted(
            result,
            key=lambda edge: (
                edge.target,
                edge.completion.kind.value if edge.completion is not None else "",
                edge.completion.target if edge.completion is not None else "",
            ),
        )
    )


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
    completion_inputs: Mapping[str, CompletionMap] = MappingProxyType({})
    completion_outputs: Mapping[str, CompletionMap] = MappingProxyType({})
    completion_uses: Mapping[str, CompletionMap] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _immutable_mapping(self.inputs))
        object.__setattr__(self, "outputs", _immutable_mapping(self.outputs))
        object.__setattr__(self, "uses", _immutable_mapping(self.uses))
        object.__setattr__(
            self,
            "completion_inputs",
            MappingProxyType(
                {
                    key: value if isinstance(value, CompletionMap) else CompletionMap(value)
                    for key, value in sorted(self.completion_inputs.items())
                }
            ),
        )
        object.__setattr__(
            self,
            "completion_outputs",
            MappingProxyType(
                {
                    key: value if isinstance(value, CompletionMap) else CompletionMap(value)
                    for key, value in sorted(self.completion_outputs.items())
                }
            ),
        )
        object.__setattr__(
            self,
            "completion_uses",
            MappingProxyType(
                {
                    key: value if isinstance(value, CompletionMap) else CompletionMap(value)
                    for key, value in sorted(self.completion_uses.items())
                }
            ),
        )

    @property
    def block_inputs(self) -> Mapping[str, BindingEnvironment]:
        return self.inputs

    @property
    def block_outputs(self) -> Mapping[str, BindingEnvironment]:
        return self.outputs

    @property
    def use_snapshots(self) -> Mapping[str, BindingEnvironment]:
        return self.uses

    @property
    def snapshots(self) -> Mapping[str, BindingEnvironment]:
        """Alias for use-site snapshots."""

        return self.uses

    @property
    def keyed_inputs(self) -> Mapping[str, CompletionMap]:
        return self.completion_inputs

    @property
    def keyed_outputs(self) -> Mapping[str, CompletionMap]:
        return self.completion_outputs

    @property
    def keyed_uses(self) -> Mapping[str, CompletionMap]:
        return self.completion_uses

    def use_for(self, site_id: str) -> BindingEnvironment | None:
        return self.uses.get(site_id)

    def keyed_use_for(self, site_id: str) -> CompletionMap | None:
        return self.completion_uses.get(site_id)

    def input_for(self, block_id: str) -> BindingEnvironment | None:
        return self.inputs.get(block_id)

    def output_for(self, block_id: str) -> BindingEnvironment | None:
        return self.outputs.get(block_id)


def meet_completion_maps(*maps: CompletionMap) -> CompletionMap:
    """Meet reachable environments per completion key."""

    result = CompletionMap()
    for value in maps:
        if not isinstance(value, CompletionMap):
            raise TypeError("completion map meet requires CompletionMap values")
        result = result.join(value)
    return result


def cross_key_snapshot(value: CompletionMap) -> BindingEnvironment:
    """Meet all keyed executions for one use site without changing routing."""

    if not isinstance(value, CompletionMap):
        raise TypeError("cross-key snapshot requires a CompletionMap")
    environments = tuple(value.values())
    if not environments:
        return BindingEnvironment()
    result = environments[0]
    for environment in environments[1:]:
        result = result.meet(environment)
    return result


cross_key_meet = cross_key_snapshot
meet_keyed_snapshot = cross_key_snapshot


@dataclass(frozen=True, slots=True)
class CleanupRegion:
    """Pointwise cleanup transformation for pending completion channels."""

    operations: tuple[StateOperation, ...] = ()

    def __post_init__(self) -> None:
        operations = tuple(self.operations)
        if any(not isinstance(operation, StateOperation) for operation in operations):
            raise TypeError("cleanup operations must be StateOperation values")
        object.__setattr__(self, "operations", operations)

    def execute(self, incoming: CompletionMap) -> CompletionMap:
        """Run cleanup independently for every incoming routing key."""

        if not isinstance(incoming, CompletionMap):
            raise TypeError("cleanup input must be a CompletionMap")
        result: dict[CompletionKey, BindingEnvironment] = {}
        for key, environment in incoming.items():
            transformed = environment
            for operation in self.operations:
                transformed = transformed.transfer(
                    operation.slot, operation.state, operation.target
                )
            result[key] = transformed
        return CompletionMap(result)

    run = execute

    def apply(
        self,
        incoming: CompletionMap,
        *,
        override: CompletionKey | None = None,
        suppress_exceptions: bool = False,
    ) -> CompletionMap:
        """Resume pending keys, or route cleanup terminal flow as an override.

        Suppression is deliberately restricted to exceptional pending flow.
        A cleanup return/raise/break/continue/unknown key therefore cannot
        accidentally suppress a pending return or targeted loop completion.
        """

        if override is not None:
            override = _coerce_completion_key(override)
            if override.is_normal:
                raise ValueError("normal cleanup is represented by resume, not override")
        transformed = self.execute(incoming)
        result: dict[CompletionKey, BindingEnvironment] = {}
        for pending_key, environment in transformed.items():
            output_key = override or pending_key
            if suppress_exceptions and pending_key.is_exception:
                output_key = CompletionKey.normal()
            if output_key in result:
                result[output_key] = result[output_key].meet(environment)
            else:
                result[output_key] = environment
        return CompletionMap(result)

    transform = apply

    def resume(
        self,
        incoming: CompletionMap,
        *,
        override: CompletionKey | None = None,
        suppress_exceptions: bool = False,
    ) -> CompletionMap:
        return self.apply(incoming, override=override, suppress_exceptions=suppress_exceptions)


def resume_cleanup(
    pending: CompletionMap,
    cleanup_result: CompletionMap,
    *,
    suppress_exceptions: bool = False,
) -> CompletionMap:
    """Correlate ordinary cleanup completion with each pending key.

    ``cleanup_result`` is keyed by the cleanup's own completion channels.  Its
    normal channel resumes each pending key; any non-normal channel is an
    explicit cleanup override.  No pending key is merged with another one.
    """

    if not isinstance(pending, CompletionMap) or not isinstance(cleanup_result, CompletionMap):
        raise TypeError("cleanup resume requires two CompletionMap values")
    normal_environment = cleanup_result.get(CompletionKey.normal())
    overrides = tuple(
        (key, environment) for key, environment in cleanup_result.items() if not key.is_normal
    )
    result: dict[CompletionKey, BindingEnvironment] = (
        {key: (normal_environment or environment) for key, environment in pending.items()}
        if normal_environment is not None
        else {}
    )
    if suppress_exceptions:
        for key in tuple(result):
            if key.is_exception:
                environment = result.pop(key)
                existing = result.get(CompletionKey.normal())
                result[CompletionKey.normal()] = (
                    existing.meet(environment) if existing is not None else environment
                )
    for key, environment in overrides:
        existing = result.get(key)
        result[key] = existing.meet(environment) if existing is not None else environment
    if normal_environment is None and not overrides:
        return CompletionMap()
    return CompletionMap(result)


cleanup_resume = resume_cleanup


class BindingSolver:
    """Compute ordinary binding flow to a deterministic fixed point."""

    def __init__(
        self,
        blocks: Iterable[BasicBlock] | Mapping[str, BasicBlock],
        entry: str | None = None,
        initial: BindingEnvironment | None = None,
        *,
        initial_environment: BindingEnvironment | None = None,
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
        if initial is not None and initial_environment is not None:
            raise ValueError("specify only one initial environment")
        self.initial = (
            initial_environment
            if initial_environment is not None
            else initial
            if initial is not None
            else BindingEnvironment()
        )
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


class CompletionSolver(BindingSolver):
    """Deterministic fixed-point solver whose environments remain keyed."""

    def __init__(
        self,
        blocks: Iterable[BasicBlock] | Mapping[str, BasicBlock],
        entry: str | None = None,
        initial: CompletionMap | BindingEnvironment | None = None,
        *,
        initial_environment: CompletionMap | BindingEnvironment | None = None,
        max_steps: int | None = None,
    ) -> None:
        if initial is not None and initial_environment is not None:
            raise ValueError("specify only one initial environment")
        supplied = initial_environment if initial_environment is not None else initial
        if supplied is None:
            completion_initial = CompletionMap.normal()
        elif isinstance(supplied, BindingEnvironment):
            completion_initial = CompletionMap.normal(supplied)
        elif isinstance(supplied, CompletionMap):
            completion_initial = supplied
        else:
            raise TypeError("completion solver initial state must be a CompletionMap")
        super().__init__(blocks, entry, initial=BindingEnvironment(), max_steps=max_steps)
        self.initial_completion = completion_initial

    def solve(self) -> ResolutionSnapshot:
        predecessors: dict[str, tuple[NormalEdge, ...]] = {
            block_id: tuple(
                sorted(
                    (
                        edge
                        for block in self._blocks.values()
                        for edge in block.normal_edges
                        if edge.target == block_id
                    ),
                    key=lambda edge: (
                        edge.source,
                        edge.completion.kind.value if edge.completion is not None else "",
                        edge.completion.target if edge.completion is not None else "",
                    ),
                )
            )
            for block_id in self._ordered_ids
        }
        outputs: dict[str, CompletionMap] = {}
        inputs: dict[str, CompletionMap] = {}
        uses: dict[str, CompletionMap] = {}
        worklist: deque[str] = deque((self.entry,))
        queued = {self.entry}
        steps = 0

        while worklist:
            block_id = worklist.popleft()
            queued.discard(block_id)
            steps += 1
            if steps > self.max_steps:
                raise RuntimeError("completion worklist did not converge within max_steps")

            incoming = CompletionMap()
            if block_id == self.entry:
                incoming = self.initial_completion
            for edge in predecessors[block_id]:
                predecessor_output = outputs.get(edge.source)
                if predecessor_output is None:
                    continue
                routed: dict[CompletionKey, BindingEnvironment] = {}
                for key, environment in predecessor_output.items():
                    routed[edge.completion or key] = environment
                incoming = incoming.join(CompletionMap(routed))
            if not incoming:
                continue
            if inputs.get(block_id) == incoming and block_id in outputs:
                continue
            inputs[block_id] = incoming
            environment_by_key: dict[CompletionKey, BindingEnvironment] = {}
            for key, environment in incoming.items():
                current = environment
                for operation in self._blocks[block_id].operations:
                    if isinstance(operation, UseOperation):
                        prior = uses.get(operation.operation_id, CompletionMap())
                        uses[operation.operation_id] = prior.join(CompletionMap({key: current}))
                    else:
                        current = current.transfer(
                            operation.slot, operation.state, operation.target
                        )
                environment_by_key[key] = current
            output = CompletionMap(environment_by_key)
            if outputs.get(block_id) == output:
                continue
            outputs[block_id] = output
            for edge in self._blocks[block_id].normal_edges:
                if edge.target not in queued:
                    worklist.append(edge.target)
                    queued.add(edge.target)

        shared_uses = {site_id: cross_key_snapshot(value) for site_id, value in uses.items()}
        shared_inputs = {block_id: cross_key_snapshot(value) for block_id, value in inputs.items()}
        shared_outputs = {
            block_id: cross_key_snapshot(value) for block_id, value in outputs.items()
        }
        return ResolutionSnapshot(
            shared_inputs,
            shared_outputs,
            shared_uses,
            inputs,
            outputs,
            uses,
        )


KeyedBindingSolver = CompletionSolver
CompletionFlowSolver = CompletionSolver


def solve(
    blocks: Iterable[BasicBlock] | Mapping[str, BasicBlock],
    entry: str | None = None,
    initial: BindingEnvironment | None = None,
    *,
    initial_environment: BindingEnvironment | None = None,
    max_steps: int | None = None,
) -> ResolutionSnapshot:
    """Convenience entry point for the sole ordinary-flow solver."""

    return BindingSolver(
        blocks,
        entry,
        initial,
        initial_environment=initial_environment,
        max_steps=max_steps,
    ).solve()


def solve_completions(
    blocks: Iterable[BasicBlock] | Mapping[str, BasicBlock],
    entry: str | None = None,
    initial: CompletionMap | BindingEnvironment | None = None,
    *,
    initial_environment: CompletionMap | BindingEnvironment | None = None,
    max_steps: int | None = None,
) -> ResolutionSnapshot:
    """Solve ordered blocks while retaining every completion routing key."""

    return CompletionSolver(
        blocks,
        entry,
        initial,
        initial_environment=initial_environment,
        max_steps=max_steps,
    ).solve()


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
    "Activation",
    "ActivationShape",
    "EnterActivation",
    "ExitActivation",
    "CompletionChannel",
    "CompletionEdge",
    "CompletionEnvironment",
    "CompletionFlowSolver",
    "CompletionKey",
    "CompletionKind",
    "CompletionMap",
    "CompletionSolver",
    "CompletionType",
    "CleanupRegion",
    "Environment",
    "BasicBlock",
    "Block",
    "BindingSolver",
    "FlowMap",
    "KeyedBindingSolver",
    "KeyedEnvironment",
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
    "meet_completion_maps",
    "cross_key_meet",
    "cross_key_snapshot",
    "meet_keyed_snapshot",
    "meet_states",
    "join_activation_shapes",
    "enter_activation",
    "exit_activation",
    "project_activation",
    "reimport",
    "transfer",
    "resume_cleanup",
    "cleanup_resume",
    "solve_completions",
]
