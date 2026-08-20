"""Typed result records for symbol-oriented queries."""

from __future__ import annotations

from dataclasses import dataclass

from minotaur.graph_model.location import Location
from minotaur.graph_model.provenance import RelationshipKind
from minotaur.graph_model.relationship import Relationship
from minotaur.query.index import GraphIndex


@dataclass(frozen=True, slots=True)
class CallerRecord:
    path: str
    line: int
    column: int
    caller: str
    unresolved: bool = False
    reference: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "caller": self.caller,
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
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "kind": self.kind,
            "duplicate": self.duplicate,
        }


def callers(index: GraphIndex, qualified_name: str) -> tuple[CallerRecord, ...]:
    """Return resolved call sites and matching unresolved references."""
    target = index.symbols_by_label.get(qualified_name, ())
    if len(target) != 1:
        return ()
    target_id = target[0].id
    records: list[CallerRecord] = []
    for relationship in index.incoming(RelationshipKind.CALLS.value, target_id):
        caller = index.nodes.get(relationship.source)
        if caller is None:
            continue
        for location in _locations(relationship):
            records.append(_caller_record(location, caller.label))

    bare_name = qualified_name.rsplit(".", 1)[-1]
    for unresolved in index.unresolved_nodes:
        reference = unresolved.reference_text or unresolved.label
        if reference != bare_name and not reference.endswith(f".{bare_name}"):
            continue
        for relationship in index.incoming(RelationshipKind.REFERENCES.value, unresolved.id):
            caller = index.nodes.get(relationship.source)
            if caller is None:
                continue
            for location in _locations(relationship):
                records.append(
                    _caller_record(location, caller.label, unresolved=True, reference=reference)
                )
    return tuple(sorted(records, key=_caller_sort_key))


def definitions(index: GraphIndex, bare_name: str) -> tuple[DefinitionRecord, ...]:
    """Return symbols whose final qualified-name segment matches ``bare_name``."""
    matches = [
        node
        for node in index.symbols()
        if node.label.rsplit(".", 1)[-1] == bare_name and node.location is not None
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
    *,
    unresolved: bool = False,
    reference: str | None = None,
) -> CallerRecord:
    return CallerRecord(
        path=location.path,
        line=location.range.start.line + 1,
        column=location.range.start.character + 1,
        caller=caller,
        unresolved=unresolved,
        reference=reference,
    )


def _caller_sort_key(record: CallerRecord) -> tuple[object, ...]:
    return (record.path, record.line, record.column, record.caller, record.reference or "")
