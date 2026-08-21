"""Shared JSON envelope helpers for query output."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol


class QueryRecord(Protocol):
    def to_dict(self) -> dict[str, object]: ...


def dump_json(payload: object) -> str:
    """Serialize one query payload in the single canonical JSON form.

    Every query routes its JSON through this helper -- including ``diff`` and
    ``context``, whose envelopes are not record lists -- so sort order,
    separators, and the trailing newline cannot drift between commands.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def render_json(
    query_name: str,
    records: Sequence[QueryRecord],
    *,
    refreshed: bool,
    stale: Sequence[str],
) -> str:
    """Render the shared record-query envelope for one query result.

    Text rendering stays with each query module because its line format is
    query-specific, but the JSON envelope is one contract for agents, so it is
    written once here instead of being hand-rolled per module.

    ``refreshed`` and ``stale`` are required rather than defaulted: freshness
    is the answer's provenance, and an agent parsing JSON has no stderr to
    read, so a caller must state whether the graph it answered from was
    rewritten and which paths had drifted.
    """
    return dump_json(
        {
            "query": query_name,
            "refreshed": refreshed,
            "results": [record.to_dict() for record in records],
            "stale": sorted(stale),
        }
    )
