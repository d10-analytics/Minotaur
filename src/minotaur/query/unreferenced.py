"""Find symbols with no inbound call or reference relationships."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import RelationshipKind
from minotaur.query.index import GraphIndex

_TOKEN_PATTERN = re.compile(r"\w+")
_CANDIDATE_KINDS = frozenset({"class", "function", "method"})


@dataclass(frozen=True, slots=True)
class UnreferencedRecord:
    """One graph-clean symbol and the reason text fallback retained it."""

    path: str
    line: int
    symbol: str
    kind: str
    text_mention: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "line": self.line,
            "path": self.path,
            "symbol": self.symbol,
            "text_mention": self.text_mention,
        }


def unreferenced(
    index: GraphIndex,
    root: Path,
    source_paths: Iterable[str],
    excluded_names: frozenset[str] = frozenset(),
    *,
    text_fallback: bool = False,
) -> tuple[UnreferencedRecord, ...]:
    """Return selected function, method, and class symbols without inbound use.

    ``source_paths`` is the graph's selected file set after any command-path
    filtering.  Graph relationships from a symbol itself or from its
    ``contains`` container describe the symbol's own definition and do not
    make it externally referenced.
    """
    selected = frozenset(source_paths)
    candidates = [
        node
        for node in index.symbols()
        if node.symbol_kind in _CANDIDATE_KINDS
        and node.location is not None
        and node.location.path in selected
    ]
    suspects = [
        node
        for node in candidates
        if _eligible(node, excluded_names) and _is_unreferenced(index, node)
    ]

    mentions: frozenset[str] = frozenset()
    if text_fallback:
        mentions = _text_mentions(root, selected)

    records: list[UnreferencedRecord] = []
    for node in suspects:
        location = node.location
        if location is None:  # narrowed by candidate construction; defensive for callers
            continue
        records.append(
            UnreferencedRecord(
                path=location.path,
                line=location.range.start.line + 1,
                symbol=node.label,
                kind=node.symbol_kind or "unknown",
                text_mention=node.label.rsplit(".", 1)[-1] in mentions,
            )
        )
    return tuple(sorted(records, key=lambda record: (record.path, record.line, record.symbol)))


def render_text(records: Iterable[UnreferencedRecord]) -> str:
    """Render one compact line per suspect without graph internals."""
    records = tuple(records)
    if not records:
        return "no unreferenced symbols\n"
    return "".join(
        f"{record.path}:{record.line}  {record.symbol}  {record.kind}"
        f"{' [text-mention]' if record.text_mention else ''}\n"
        for record in records
    )


def load_exclusions(path: Path | None) -> frozenset[str]:
    """Load names from a JSON exclusion list or mapping, with line fallback."""
    if path is None:
        return frozenset()
    content = path.read_text(encoding="utf-8")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return frozenset(line.strip() for line in content.splitlines() if line.strip())
    values: list[str] = []
    if isinstance(data, list):
        values.extend(value for value in data if isinstance(value, str))
    elif isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(item for item in value if isinstance(item, str))
    else:
        raise ValueError("exclude file must contain a JSON list or object of names")
    return frozenset(values)


def _eligible(node: Node, excluded_names: frozenset[str]) -> bool:
    name = node.label.rsplit(".", 1)[-1]
    if name in excluded_names:
        return False
    if name.startswith("test_"):
        return False
    return not (name.startswith("__") and name.endswith("__"))


def _is_unreferenced(index: GraphIndex, node: Node) -> bool:
    own_sources = {node.id}
    own_sources.update(
        relationship.source
        for relationship in index.incoming(RelationshipKind.CONTAINS.value, node.id)
    )
    for kind in (RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value):
        if any(
            relationship.source not in own_sources for relationship in index.incoming(kind, node.id)
        ):
            return False
    return True


def _text_mentions(root: Path, selected: frozenset[str]) -> frozenset[str]:
    counts: dict[str, int] = {}
    for relative in selected:
        path = root / relative
        for token in _TOKEN_PATTERN.findall(path.read_text(encoding="utf-8")):
            counts[token] = counts.get(token, 0) + 1
    # One occurrence is the definition itself. Any additional occurrence may
    # be a string, comment, getattr, or another syntax the graph cannot resolve.
    return frozenset(token for token, count in counts.items() if count > 1)
