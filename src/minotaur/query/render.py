"""Shared output helpers for query JSON envelopes and safe text.

``dump_json`` renders the one canonical JSON form for every query envelope.
``escape_text`` is the single owner of the safe-text character rule for text
renderers: each character whose ``str`` form is not printable is replaced by
Python's own spelling (``repr(c)[1:-1]``) with no quoting added, while
printable characters -- including every non-ASCII letter -- pass through, so
re-running the helper changes nothing. Ordinary renderer text escapes one
interpolated graph/repo field at a time; ``query context`` source lines are
exempt from the tab rule only and still use this helper for every other
unsafe character.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from minotaur.query.freshness import AnalyzerChange

if TYPE_CHECKING:
    from minotaur.query.system import SystemQueryResult


class QueryRecord(Protocol):
    def to_dict(self) -> dict[str, object]: ...


def escape_text(text: str) -> str:
    """Spell every non-printable character in ``text`` with Python's own escape.

    This is the one definition of an unsafe character: a character for which
    ``c.isprintable()`` is false is replaced by ``repr(c)[1:-1]`` -- ``\\n``,
    ``\\r``, ``\\t``, ``\\x1b``, ``\\x7f``, the C1 range, and ``\\uNNNN`` format
    characters such as ``\\u202e`` -- while printable characters, including
    spaces, backslashes, quotes, and every non-ASCII letter, are returned
    unchanged and no quotes are added. Escaped output is itself fully
    printable, so applying the helper twice changes nothing.
    """
    return "".join(repr(char)[1:-1] if not char.isprintable() else char for char in text)


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
    stale_analyzer: AnalyzerChange | None,
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
            "stale_analyzer": stale_analyzer.to_dict() if stale_analyzer is not None else None,
        }
    )


def render_system_json(result: SystemQueryResult[Any]) -> str:
    """Render one composed system-query result through the canonical wire view."""
    return dump_json(result.to_dict())


def render_system_text(result: SystemQueryResult[Any], summary: str) -> str:
    """Prefix the unchanged summary with composed coverage and optional details."""
    payload = result.to_dict()
    coverage = json.dumps(payload["coverage"], sort_keys=True, separators=(",", ":"))
    output = f"coverage {coverage}\n{summary}"
    relationships = payload.get("relationships")
    if relationships is not None:
        encoded = json.dumps(relationships, sort_keys=True, separators=(",", ":"))
        output += f"relationships {encoded}\n"
    return output


def render_systems_json(result: SystemQueryResult[Any]) -> str:
    """Render the all-systems overview through the canonical JSON dumper."""
    return dump_json(result.to_dict())


def render_systems_text(result: SystemQueryResult[Any]) -> str:
    """Render the compact or detailed all-systems overview as stable text."""
    payload = result.to_dict()
    coverage = cast(Mapping[str, object], payload["coverage"])
    output = f"coverage {dump_json(coverage)}"
    records = cast(Sequence[Mapping[str, object]], payload["results"])
    if records:
        for record in records:
            declared = cast(Mapping[str, object], record["declared_files"])
            output += (
                f"{escape_text(str(record['name']))}  declared {declared['total']}  "
                f"represented {declared['represented']}  absent {declared['absent']}\n"
            )
    else:
        output += "no declared systems\n"
    if "connections" in payload:
        declared_files = {
            str(record["name"]): cast(
                list[str], cast(Mapping[str, object], record["declared_files"])["paths"]
            )
            for record in records
        }
        output += f"declared_files {dump_json(declared_files)}"
        output += f"connections {dump_json(payload['connections'])}"
    return output
