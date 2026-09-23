"""Typed result records for symbol-oriented queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from minotaur.graph_model.location import Location
from minotaur.graph_model.provenance import RelationshipKind
from minotaur.graph_model.relationship import Relationship
from minotaur.query.index import GraphIndex
from minotaur.query.sql import CURRENT_SQL_DEPENDENCY_KINDS


@dataclass(frozen=True, slots=True)
class CallerRecord:
    path: str
    line: int
    column: int
    caller: str
    kind: str
    unresolved: bool = False
    reference: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "caller": self.caller,
            "column": self.column,
            "kind": self.kind,
            "line": self.line,
            "path": self.path,
            "unresolved": self.unresolved,
        }
        if self.reference is not None:
            result["reference"] = self.reference
        return result


@dataclass(frozen=True, slots=True)
class DefinitionRecord:
    path: str
    line: int
    symbol: str
    kind: str
    duplicate: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate": self.duplicate,
            "kind": self.kind,
            "line": self.line,
            "path": self.path,
            "symbol": self.symbol,
        }


def label_bare_name(label: str) -> str:
    """Return the final declaration-name segment from a qualified label."""
    return label.rsplit(".", 1)[-1]


def callers(index: GraphIndex, qualified_name: str) -> tuple[CallerRecord, ...]:
    """Return resolved call sites and matching unresolved references.

    Resolution failures propagate as ``SymbolResolutionError``: an unknown or
    ambiguous name must reach the caller as an error, never as an empty tuple
    that renders as a confident ``no callers``.
    """
    target_id = index.resolve(qualified_name).id
    resolved_records: list[CallerRecord] = []
    for kind in (RelationshipKind.CALLS.value, *CURRENT_SQL_DEPENDENCY_KINDS):
        for relationship in index.incoming(kind, target_id):
            caller = index.nodes.get(relationship.source)
            if caller is None:
                continue
            for location in _locations(relationship):
                resolved_records.append(_caller_record(location, caller.label, relationship.kind))

    bare_name = qualified_name.rsplit(".", 1)[-1]
    unresolved_records: list[CallerRecord] = []
    for unresolved in index.unresolved_nodes:
        reference = unresolved.reference_text or unresolved.label
        for relationship in index.incoming(RelationshipKind.REFERENCES.value, unresolved.id):
            caller = index.nodes.get(relationship.source)
            if caller is None:
                continue
            if not _matches_reference(reference, bare_name, sql=unresolved.language == "sql"):
                continue
            for location in _locations(relationship):
                unresolved_records.append(
                    _caller_record(
                        location,
                        caller.label,
                        relationship.kind,
                        unresolved=True,
                        reference=reference,
                    )
                )
    # Keep the high-confidence resolved hits together. Unresolved matches are
    # a recall tail, even when their source location sorts before a resolved
    # call site; this makes the confidence distinction visible in text output.
    return tuple(
        sorted(resolved_records, key=_caller_sort_key)
        + sorted(unresolved_records, key=_caller_sort_key)
    )


def definitions(index: GraphIndex, bare_name: str) -> tuple[DefinitionRecord, ...]:
    """Return symbols whose final qualified-name segment matches ``bare_name``."""
    matches = [
        node
        for node in index.symbols()
        if (
            node.location is not None
            and (
                label_bare_name(node.label) == bare_name
                or (
                    node.language == "sql"
                    and label_bare_name(node.label).casefold() == bare_name.casefold()
                )
            )
        )
    ]
    duplicate = len(matches) > 1
    records: list[DefinitionRecord] = []
    for node in matches:
        location = node.location
        if location is None:  # narrowed by the selection above; defensive for callers
            continue
        records.append(
            DefinitionRecord(
                path=location.path,
                line=location.range.start.line + 1,
                symbol=node.label,
                kind=node.symbol_kind or "unknown",
                duplicate=duplicate,
            )
        )
    return tuple(sorted(records, key=lambda record: (record.path, record.line, record.symbol)))


def _locations(relationship: Relationship) -> tuple[Location, ...]:
    # Kept as a tiny adapter so record construction cannot accidentally expose
    # evidence/provenance details in a query result. Multiple independent
    # evidence records can support the same physical site, but a query hit is
    # intentionally one line per site because the renderers omit provenance.
    return tuple(
        dict.fromkeys(
            location for evidence in relationship.evidence for location in evidence.locations
        )
    )


def _caller_record(
    location: Location,
    caller: str,
    kind: str,
    *,
    unresolved: bool = False,
    reference: str | None = None,
) -> CallerRecord:
    return CallerRecord(
        path=location.path,
        line=location.range.start.line + 1,
        column=location.range.start.character + 1,
        caller=caller,
        kind=kind,
        unresolved=unresolved,
        reference=reference,
    )


def _caller_sort_key(record: CallerRecord) -> tuple[object, ...]:
    return (
        record.path,
        record.line,
        record.column,
        record.caller,
        record.kind,
        record.reference or "",
    )


def render_callers_text(records: Sequence[CallerRecord]) -> str:
    """Render one line per call site, marking unresolved recall matches."""
    if not records:
        return "no callers\n"
    return "".join(_caller_text(record) for record in records)


def render_definitions_text(records: Sequence[DefinitionRecord]) -> str:
    """Render one line per definition, marking shared bare names."""
    if not records:
        return "no definitions\n"
    return "".join(_definition_text(record) for record in records)


def _caller_text(record: CallerRecord) -> str:
    suffix = f" [{record.kind}]"
    if record.unresolved:
        suffix += " [unresolved]"
    label = (
        record.reference if record.unresolved and record.reference is not None else record.caller
    )
    return f"{record.path}:{record.line}:{record.column}  {label}{suffix}\n"


def _matches_reference(reference: str, bare_name: str, *, sql: bool) -> bool:
    """Match a recall reference, case-folding only SQL-produced nodes."""
    if reference == bare_name or reference.endswith(f".{bare_name}"):
        return True
    if not sql:
        return False
    folded_reference = reference.casefold()
    folded_bare_name = bare_name.casefold()
    return folded_reference == folded_bare_name or folded_reference.endswith(f".{folded_bare_name}")


def _definition_text(record: DefinitionRecord) -> str:
    suffix = " [duplicate-name]" if record.duplicate else ""
    return f"{record.path}:{record.line}  {record.symbol}  {record.kind}{suffix}\n"
