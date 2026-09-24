"""Find symbols with no inbound call or reference relationships."""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp, parse_one

from minotaur.graph_model.location import Location
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import RelationshipKind
from minotaur.language_interpreter.source_text import LineIndex
from minotaur.query.index import GraphIndex
from minotaur.query.sql import CURRENT_SQL_DEPENDENCY_KINDS
from minotaur.query.symbols import label_bare_name

_TOKEN_PATTERN = re.compile(r"\w+")
_CANDIDATE_KINDS = frozenset({"class", "function", "method"})
_SQL_CANDIDATE_KINDS = frozenset({"sql:table", "sql:view"})


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
    """Return selected code and SQL symbols without inbound use.

    ``source_paths`` is the graph's selected file set after any command-path
    filtering.  Only relationships whose source is the symbol itself are
    discarded; every other inbound call or reference — including one from the
    module node, which is how module-scope use is recorded — counts as use.
    """
    selected = frozenset(source_paths)
    candidates = [
        node
        for node in index.symbols()
        if _is_candidate(node) and node.location is not None and node.location.path in selected
    ]
    sql_bare_names = _sql_bare_names(candidates, root) if text_fallback or excluded_names else {}
    suspects = [
        node
        for node in candidates
        if _eligible(
            node,
            excluded_names,
            excluded_patterns,
            bare_name=_bare_name(node, sql_bare_names),
        )
        and _is_unreferenced(index, node)
    ]

    core_mentions: frozenset[str] = frozenset()
    sql_mentions: frozenset[str] = frozenset()
    if text_fallback:
        core_mentions, sql_mentions = _text_mentions(index, root, selected, sql_bare_names)

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
                text_mention=(
                    _bare_name(node, sql_bare_names).casefold() in sql_mentions
                    if _is_sql_candidate(node)
                    else label_bare_name(node.label) in core_mentions
                ),
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
    *,
    bare_name: str | None = None,
) -> bool:
    name = bare_name if bare_name is not None else label_bare_name(node.label)
    if name in excluded_names:
        return False
    # Patterns are searched against the qualified label so a caller can
    # express framework conventions Minotaur must not know about (pytest's
    # ``Test*`` classes, Qt overrides, generated modules) without Minotaur
    # hard-coding any language or framework.
    if any(pattern.search(node.label) for pattern in excluded_patterns):
        return False
    if _is_sql_candidate(node):
        return True
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
    kinds = (
        CURRENT_SQL_DEPENDENCY_KINDS
        if _is_sql_candidate(node)
        else (RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value)
    )
    for kind in kinds:
        if any(
            relationship.source not in own_sources for relationship in index.incoming(kind, node.id)
        ):
            return False
    return True


def _is_sql_candidate(node: Node) -> bool:
    return node.language == "sql" and node.symbol_kind in _SQL_CANDIDATE_KINDS


def _is_candidate(node: Node) -> bool:
    return node.symbol_kind in _CANDIDATE_KINDS or _is_sql_candidate(node)


def _text_mentions(
    index: GraphIndex,
    root: Path,
    selected: frozenset[str],
    sql_bare_names: dict[str, str],
) -> tuple[frozenset[str], frozenset[str]]:
    counts: dict[str, int] = {}
    folded_counts: dict[str, int] = {}
    for relative in selected:
        path = root / relative
        for token in _TOKEN_PATTERN.findall(path.read_text(encoding="utf-8")):
            counts[token] = counts.get(token, 0) + 1
            folded = token.casefold()
            folded_counts[folded] = folded_counts.get(folded, 0) + 1
    # Every definition of the name contributes one occurrence -- its own ``def``
    # or ``class`` line -- so the baseline to beat is the number of definitions,
    # not one. Comparing against a fixed 1 made same-name definitions vouch for
    # each other: two unreferenced methods named ``render`` on different classes
    # each counted the other's ``def`` line, both were tagged ``[text-mention]``,
    # and a hygiene pass never surfaced either. Any occurrence beyond the
    # definitions may be a string, comment, getattr, or another syntax the graph
    # cannot resolve, so it keeps the suspect in the result.
    definitions = _definition_counts(index, selected)
    sql_definitions = _sql_definition_counts(index, selected, sql_bare_names)
    return (
        frozenset(token for token, count in counts.items() if count > definitions.get(token, 0)),
        frozenset(
            token for token, count in folded_counts.items() if count > sql_definitions.get(token, 0)
        ),
    )


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
        name = label_bare_name(node.label)
        counts[name] = counts.get(name, 0) + 1
    return counts


def _sql_definition_counts(
    index: GraphIndex, selected: frozenset[str], sql_bare_names: dict[str, str]
) -> dict[str, int]:
    """Count selected SQL table/view declarations by case-insensitive bare name."""
    counts: dict[str, int] = {}
    for node in index.symbols():
        location = node.location
        if not _is_sql_candidate(node) or location is None or location.path not in selected:
            continue
        name = _bare_name(node, sql_bare_names).casefold()
        counts[name] = counts.get(name, 0) + 1
    return counts


def _bare_name(node: Node, sql_bare_names: dict[str, str]) -> str:
    """Return a final name, retaining dots inside a quoted SQL identifier."""
    return sql_bare_names.get(node.id, label_bare_name(node.label))


def _sql_bare_names(nodes: Iterable[Node], root: Path) -> dict[str, str]:
    """Recover final SQL identifier segments from their analyzed declaration spans."""
    source_by_path: dict[str, str] = {}
    names: dict[str, str] = {}
    for node in nodes:
        location = node.location
        if not _is_sql_candidate(node) or location is None:
            continue
        source = source_by_path.get(location.path)
        if source is None:
            try:
                source = (root / location.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # A stale graph remains queryable without its source. Its flattened
                # label is the best information available in that snapshot.
                continue
            source_by_path[location.path] = source
        name = _sql_final_identifier(source, location)
        if name is not None:
            names[node.id] = name
    return names


def _sql_final_identifier(source: str, location: Location) -> str | None:
    """Parse an SQL declaration span and return its final identifier segment."""
    span = _source_span(source, location)
    if span is None:
        return None
    try:
        statement = parse_one(f"CREATE TABLE {span} (id int)", read="tsql")
    except Exception:
        return None
    if not isinstance(statement, exp.Create):
        return None
    table = statement.this
    if isinstance(table, exp.Schema):
        table = table.this
    if not isinstance(table, exp.Table):
        return None
    identifier = table.args.get("this")
    return str(identifier.this) if isinstance(identifier, exp.Identifier) else None


def _source_span(source: str, location: Location) -> str | None:
    """Return one UTF-8 graph location from decoded source text."""
    index = LineIndex(source)
    try:
        start = _source_offset(index, location.range.start.line, location.range.start.character)
        end = _source_offset(index, location.range.end.line, location.range.end.character)
    except (IndexError, ValueError):
        return None
    return source[start:end] if start < end else None


def _source_offset(index: LineIndex, line: int, character: int) -> int:
    line_start = index.line_starts[line]
    target = index.byte_prefix[line_start] + character
    offset = bisect_left(index.byte_prefix, target, lo=line_start)
    if offset == len(index.byte_prefix) or index.byte_prefix[offset] != target:
        raise ValueError("position is not at a UTF-8 character boundary")
    return offset
