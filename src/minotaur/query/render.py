"""Stable text and JSON renderers for query result records."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol

from minotaur.query.symbols import CallerRecord, DefinitionRecord


class QueryRecord(Protocol):
    def to_dict(self) -> dict[str, object]: ...


def render_text(query: str, records: Sequence[QueryRecord]) -> str:
    if not records:
        return "no callers\n" if query == "callers" else "no definitions\n"
    if query == "callers":
        return "".join(
            _caller_text(record) for record in records if isinstance(record, CallerRecord)
        )
    return "".join(
        _definition_text(record) for record in records if isinstance(record, DefinitionRecord)
    )


def render_json(query: str, records: Sequence[QueryRecord]) -> str:
    payload = {"query": query, "results": [record.to_dict() for record in records]}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _caller_text(record: CallerRecord) -> str:
    suffix = " [unresolved]" if record.unresolved else ""
    label = (
        record.reference if record.unresolved and record.reference is not None else record.caller
    )
    return f"{record.path}:{record.line}:{record.column}  {label}{suffix}\n"


def _definition_text(record: DefinitionRecord) -> str:
    suffix = " [duplicate-name]" if record.duplicate else ""
    return f"{record.path}:{record.line}  {record.symbol}  {record.kind}{suffix}\n"
