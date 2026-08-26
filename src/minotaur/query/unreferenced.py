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
    excluded_patterns: tuple[re.Pattern[str], ...] = (),
    text_fallback: bool = False,
) -> tuple[UnreferencedRecord, ...]:
    """Return selected function, method, and class symbols without inbound use.

    ``source_paths`` is the graph's selected file set after any command-path
    filtering.  Only relationships whose source is the symbol itself are
    discarded; every other inbound call or reference — including one from the
    module node, which is how module-scope use is recorded — counts as use.
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
        if _eligible(node, excluded_names, excluded_patterns) and _is_unreferenced(index, node)
    ]

    mentions: frozenset[str] = frozenset()
    if text_fallback:
        mentions = _text_mentions(index, root, selected)

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


def compile_patterns(values: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Compile ``--exclude-pattern`` regexes, reporting a bad one as a ValueError."""
    compiled: list[re.Pattern[str]] = []
    for value in values:
        try:
            compiled.append(re.compile(value))
        except re.error as error:
            raise ValueError(f"invalid --exclude-pattern {value!r}: {error}") from error
    return tuple(compiled)


def _eligible(
    node: Node,
    excluded_names: frozenset[str],
    excluded_patterns: tuple[re.Pattern[str], ...] = (),
) -> bool:
    name = node.label.rsplit(".", 1)[-1]
    if name in excluded_names:
        return False
    # Patterns are searched against the qualified label so a caller can
    # express framework conventions Minotaur must not know about (pytest's
    # ``Test*`` classes, Qt overrides, generated modules) without Minotaur
    # hard-coding any language or framework.
    if any(pattern.search(node.label) for pattern in excluded_patterns):
        return False
    if name.startswith("test_"):
        return False
    return not (name.startswith("__") and name.endswith("__"))


def _is_unreferenced(index: GraphIndex, node: Node) -> bool:
    # Only the symbol's own node is excluded: a recursive self-call is
    # attributed to the symbol itself and does not make it used from anywhere
    # else. Decorators are attributed to the enclosing scope, so decoration
    # counts as use. The ``contains`` container is
    # deliberately *not* excluded. For a method that container is its class,
    # but for a top-level function it is the module node, which is also the
    # attributed source of every module-scope statement — so excluding it
    # discarded `app = create_app()`, `register(handler)`, and callback tables
    # as if they were part of the definition, and reported live functions dead.
    own_sources = {node.id}
    for kind in (RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value):
        if any(
            relationship.source not in own_sources for relationship in index.incoming(kind, node.id)
        ):
            return False
    return True


def _text_mentions(index: GraphIndex, root: Path, selected: frozenset[str]) -> frozenset[str]:
    counts: dict[str, int] = {}
    for relative in selected:
        path = root / relative
        for token in _TOKEN_PATTERN.findall(path.read_text(encoding="utf-8")):
            counts[token] = counts.get(token, 0) + 1
    # Every definition of the name contributes one occurrence -- its own ``def``
    # or ``class`` line -- so the baseline to beat is the number of definitions,
    # not one. Comparing against a fixed 1 made same-name definitions vouch for
    # each other: two unreferenced methods named ``render`` on different classes
    # each counted the other's ``def`` line, both were tagged ``[text-mention]``,
    # and a hygiene pass never surfaced either. Any occurrence beyond the
    # definitions may be a string, comment, getattr, or another syntax the graph
    # cannot resolve, so it keeps the suspect in the result.
    definitions = _definition_counts(index, selected)
    return frozenset(token for token, count in counts.items() if count > definitions.get(token, 0))


def _definition_counts(index: GraphIndex, selected: frozenset[str]) -> dict[str, int]:
    """Count graph definitions per bare name inside the scanned files.

    Only the kinds written as ``def``/``class`` statements are counted, and only
    where they live in a scanned file, so each counted definition corresponds to
    exactly one token occurrence in the text that ``_text_mentions`` reads.
    """
    counts: dict[str, int] = {}
    for node in index.symbols():
        location = node.location
        if node.symbol_kind not in _CANDIDATE_KINDS or location is None:
            continue
        if location.path not in selected:
            continue
        name = node.label.rsplit(".", 1)[-1]
        counts[name] = counts.get(name, 0) + 1
    return counts
