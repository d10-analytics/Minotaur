"""AST-authoritative, bounded T-SQL structural analysis.

SQLGlot is the only semantic authority in this module.  The small source
scanner exists only for batch boundaries and for proving that parser offsets
refer to the original source; it does not classify SQL statements.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlglot import exp, parse

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    RelationshipKind,
)
from minotaur.language_interpreter.accumulation import RelationshipAccumulator
from minotaur.language_interpreter.contract import AnalysisResult, Diagnostic, DiagnosticCode
from minotaur.language_interpreter.emission import NodeEmitter, file_node
from minotaur.language_interpreter.reading import RawSource, read_sources
from minotaur.language_interpreter.source_text import LineIndex
from minotaur.language_interpreter.workspace import Workspace

NAMESPACE = "minotaur-sql"
_PRODUCER = Producer(name=NAMESPACE)
_GO_RE = re.compile(r"[ \t]*GO(?:[ \t]+([0-9]+))?[ \t]*(?:--[^\r\n]*)?\Z", re.IGNORECASE)
_PROPERTY_PROCEDURES = frozenset(
    {"sp_addextendedproperty", "sp_updateextendedproperty", "sp_dropextendedproperty"}
)


@dataclass(frozen=True, slots=True)
class _Batch:
    text: str
    start: int
    end: int
    unsupported_count: bool = False


@dataclass(frozen=True, slots=True)
class _File:
    raw: RawSource
    line_index: LineIndex
    file_id: str
    node: Node


@dataclass(frozen=True, slots=True)
class _Declaration:
    kind: str
    key: tuple[str, ...]
    label: str
    location: Location
    node: Node
    file_id: str


@dataclass(frozen=True, slots=True)
class _Observation:
    owner: _Declaration
    reads: tuple[tuple[tuple[str, ...], str, Location], ...] = ()
    foreign_keys: tuple[tuple[tuple[str, ...], str, Location], ...] = ()


def analyze_sql_files(workspace: Workspace, files: tuple[Path, ...]) -> AnalysisResult:
    """Analyze selected SQL files; SQL is intentionally not registry-owned."""
    sources, diagnostics = read_sources(workspace, files)
    file_data = tuple(_make_file(source) for source in sources)
    nodes: list[Node] = [item.node for item in file_data]
    declarations: list[_Declaration] = []
    observations: list[_Observation] = []
    batches_by_path: dict[str, tuple[_Batch, ...]] = {}

    for item in file_data:
        batches, scan_diagnostics, complete = _scan_batches(
            item.raw.source, item.raw.relative, item.line_index
        )
        diagnostics.extend(scan_diagnostics)
        if not complete:
            batches_by_path[item.raw.relative] = ()
            continue
        batches_by_path[item.raw.relative] = tuple(batches)
        for batch in batches:
            if not batch.text.strip():
                continue
            try:
                trees = parse(batch.text, read="tsql")
            except Exception:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticCode.PARSE_ERROR, item.raw.relative, "unable to parse T-SQL batch"
                    )
                )
                continue
            for tree in trees:
                if tree is None:
                    continue
                result = _interpret_statement(tree, item, batch, diagnostics, declarations, nodes)
                if result is not None:
                    observations.append(result)

    by_key: dict[tuple[str, ...], list[_Declaration]] = {}
    for declaration in declarations:
        by_key.setdefault(declaration.key, []).append(declaration)
    for _key, values in by_key.items():
        if len(values) > 1:
            for value in values:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticCode.DUPLICATE_DECLARATION,
                        value.location.path,
                        "duplicate SQL declaration",
                        value.location,
                    )
                )

    relationships = RelationshipAccumulator()
    for item in file_data:
        for declaration in declarations:
            if declaration.file_id == item.file_id:
                relationships.add(
                    item.file_id, declaration.node.id, RelationshipKind.CONTAINS.value, None
                )

    schema_index = _typed_index(declarations, "schema")
    table_index = _typed_index(declarations, "table")
    view_index = _typed_index(declarations, "view")
    for key, values in schema_index.items():
        if len(values) != 1:
            continue
        schema = values[0]
        for declaration in declarations:
            if (
                declaration.kind in {"table", "view"}
                and len(declaration.key) == 2
                and declaration.key[0] == key[0]
            ):
                relationships.add(
                    schema.node.id,
                    declaration.node.id,
                    RelationshipKind.CONTAINS.value,
                    declaration.location,
                )

    emitter = NodeEmitter(NAMESPACE, "sql")
    for observation in observations:
        for key, text, location in observation.reads:
            _resolve(
                observation.owner,
                key,
                text,
                location,
                table_index,
                view_index,
                relationships,
                nodes,
                diagnostics,
                emitter,
                "sql:reads-from",
                "read",
            )
        for key, text, location in observation.foreign_keys:
            _resolve(
                observation.owner,
                key,
                text,
                location,
                table_index,
                {},
                relationships,
                nodes,
                diagnostics,
                emitter,
                "sql:foreign-key-to",
                "foreign-key",
            )

    # Replace the generic references kind for reads with the SQL extension.
    # Resolution above uses the shared emitter only for unresolved nodes; the
    # successful edge is written directly with the requested SQL meaning.
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=tuple(nodes),
        relationships=relationships.documents(_PRODUCER),
        generated_by=_PRODUCER,
    )
    diagnostics.sort(
        key=lambda item: (
            item.path,
            item.location.sort_key if item.location else ("", 0, 0, 0, 0),
            item.code.value,
            item.message,
        )
    )
    return AnalysisResult(document, tuple(diagnostics))


def _make_file(source: RawSource) -> _File:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, NAMESPACE)
    file_id = compute_node_id(identity, node_class=NodeClass.FILE.value, path=source.relative)
    return _File(
        source,
        LineIndex(source.source),
        file_id,
        file_node(source.relative, source.content, NAMESPACE, "sql"),
    )


def _typed_index(
    declarations: Iterable[_Declaration], kind: str
) -> dict[tuple[str, ...], list[_Declaration]]:
    result: dict[tuple[str, ...], list[_Declaration]] = {}
    for declaration in declarations:
        if declaration.kind == kind:
            result.setdefault(declaration.key, []).append(declaration)
    return result


def _resolve(
    owner: _Declaration,
    key: tuple[str, ...],
    text: str,
    location: Location,
    primary: dict[tuple[str, ...], list[_Declaration]],
    secondary: dict[tuple[str, ...], list[_Declaration]],
    relationships: RelationshipAccumulator,
    nodes: list[Node],
    diagnostics: list[Diagnostic],
    emitter: NodeEmitter,
    relationship_kind: str,
    relation: str,
) -> None:
    candidates = primary.get(key, []) + secondary.get(key, [])
    if len(candidates) == 1:
        relationships.add(owner.node.id, candidates[0].node.id, relationship_kind, location)
        return
    if len(candidates) > 1:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.AMBIGUOUS_REFERENCE,
                location.path,
                "ambiguous SQL reference",
                location,
            )
        )
    unresolved = emitter.unresolved(owner.node.id, text, location, nodes, relationships)
    # ``NodeEmitter`` always uses core references, which is exactly the
    # unresolved-target contract; successful SQL relationships remain typed.
    _ = unresolved, relation


def _interpret_statement(
    tree: Any,
    item: _File,
    batch: _Batch,
    diagnostics: list[Diagnostic],
    declarations: list[_Declaration],
    nodes: list[Node],
) -> _Observation | None:
    if isinstance(tree, exp.Create):
        return _interpret_create(tree, item, batch, diagnostics, declarations, nodes)
    if _neutral_statement(tree):
        return None
    _unsupported(tree, item, batch, diagnostics)
    return None


def _interpret_create(
    tree: Any,
    item: _File,
    batch: _Batch,
    diagnostics: list[Diagnostic],
    declarations: list[_Declaration],
    nodes: list[Node],
) -> _Observation | None:
    kind = str(tree.args.get("kind") or "").upper()
    target = tree.this
    if kind == "INDEX":
        if _valid_index(tree):
            return None
        _unsupported(tree, item, batch, diagnostics)
        return None
    if kind not in {"SCHEMA", "TABLE", "VIEW"}:
        _unsupported(tree, item, batch, diagnostics)
        return None
    if tree.args.get("exists") or tree.args.get("clone") or tree.args.get("refresh"):
        _unsupported(tree, item, batch, diagnostics)
        return None
    if kind in {"SCHEMA", "TABLE"} and tree.args.get("expression") is not None:
        _unsupported(tree, item, batch, diagnostics)
        return None
    table = target.this if kind in {"TABLE", "VIEW"} and isinstance(target, exp.Schema) else target
    if not isinstance(table, exp.Table):
        _unsupported(tree, item, batch, diagnostics)
        return None
    parts = _parts(table)
    if parts is None or len(parts) not in ({1} if kind == "SCHEMA" else {1, 2}):
        _unsupported(tree, item, batch, diagnostics)
        return None
    if _temporary_table(table) or any(_temporary(part) for part in parts):
        _unsupported(tree, item, batch, diagnostics)
        return None
    location = _table_location(table, item, batch)
    if location is None:
        _unsupported(tree, item, batch, diagnostics)
        return None
    key = tuple(part.casefold() for part in parts)
    node = _sql_symbol_node(".".join(parts), f"sql:{kind.casefold()}", location)
    declaration = _Declaration(kind.casefold(), key, ".".join(parts), location, node, item.file_id)
    declarations.append(declaration)
    nodes.append(node)
    if kind == "VIEW":
        expression = tree.args.get("expression")
        reads = _query_reads(expression, item, batch, diagnostics)
        if reads is None:
            # Remove the declaration: a rejected whole statement emits no
            # partial fact, including its declaration node.
            declarations.pop()
            nodes.pop()
            _unsupported(tree, item, batch, diagnostics)
            return None
        return _Observation(declaration, tuple(reads), ())
    if kind == "TABLE":
        fks = _foreign_keys(target, item, batch, diagnostics)
        if fks is None:
            declarations.pop()
            nodes.pop()
            _unsupported(tree, item, batch, diagnostics)
            return None
        return _Observation(declaration, (), tuple(fks))
    return _Observation(declaration)


def _sql_symbol_node(label: str, kind: str, location: Location) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    return Node(
        id=compute_node_id(
            identity, node_class=NodeClass.SYMBOL.value, symbol_kind=kind, location=location
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=kind,
        language="sql",
        location=location,
    )


def _parts(table: exp.Expression) -> tuple[str, ...] | None:
    if not isinstance(table, exp.Table):
        return None
    identifiers: list[exp.Identifier] = []
    for value in (table.args.get("catalog"), table.args.get("db"), table.args.get("this")):
        if value:
            if not isinstance(value, exp.Identifier):
                return None
            identifiers.append(value)
    return tuple(str(identifier.this) for identifier in identifiers)


def _temporary(part: str) -> bool:
    return part.startswith(("#", "@"))


def _temporary_table(table: exp.Table) -> bool:
    name = table.args.get("this")
    return isinstance(name, exp.Identifier) and bool(name.args.get("temporary"))


def _identifier_location(
    expression: exp.Expression, path: str, line_index: LineIndex, batch_start: int
) -> Location | None:
    identifiers = [node for node in expression.walk() if isinstance(node, exp.Identifier)]
    spans: list[tuple[int, int]] = []
    for node in identifiers:
        start = node.meta.get("start")
        end = node.meta.get("end")
        if isinstance(start, int) and isinstance(end, int):
            spans.append((start, end))
    if not spans:
        return None
    start = batch_start + min(span[0] for span in spans)
    end = batch_start + max(span[1] for span in spans) + 1
    if start < 0 or end > len(line_index.source) or line_index.source[start:end] == "":
        return None
    return Location(path, Range(line_index.position(start), line_index.position(end)))


def _table_location(table: exp.Table, item: _File, batch: _Batch) -> Location | None:
    identifiers = [
        value
        for value in (
            table.args.get("catalog"),
            table.args.get("db"),
            table.args.get("this"),
        )
        if isinstance(value, exp.Identifier)
    ]
    spans: list[tuple[int, int]] = []
    for identifier in identifiers:
        start = identifier.meta.get("start")
        end = identifier.meta.get("end")
        if isinstance(start, int) and isinstance(end, int):
            spans.append((start, end))
    if not spans:
        return None
    start = batch.start + min(span[0] for span in spans)
    end = batch.start + max(span[1] for span in spans) + 1
    if start < 0 or end > len(item.line_index.source):
        return None
    return Location(
        item.raw.relative,
        Range(item.line_index.position(start), item.line_index.position(end)),
    )


def _foreign_keys(
    target: exp.Expression, item: _File, batch: _Batch, diagnostics: list[Diagnostic]
) -> list[tuple[tuple[str, ...], str, Location]] | None:
    if not isinstance(target, exp.Schema):
        return []
    result: list[tuple[tuple[str, ...], str, Location]] = []
    for reference in target.find_all(exp.Reference):
        referenced = reference.this.this if isinstance(reference.this, exp.Schema) else None
        if not isinstance(referenced, exp.Table):
            return None
        parts = _parts(referenced)
        location = _table_location(referenced, item, batch)
        if (
            parts is None
            or len(parts) not in {1, 2}
            or _temporary_table(referenced)
            or any(_temporary(part) for part in parts)
            or location is None
        ):
            return None
        result.append((tuple(part.casefold() for part in parts), ".".join(parts), location))
    return result


def _query_reads(
    expression: Any,
    item: _File,
    batch: _Batch,
    diagnostics: list[Diagnostic],
) -> list[tuple[tuple[str, ...], str, Location]] | None:
    if expression is None:
        return None
    if not isinstance(expression, (exp.Query, exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return None
    result: list[tuple[tuple[str, ...], str, Location]] = []

    def visit_query(query: Any, cte_names: frozenset[str]) -> bool:
        if not isinstance(query, (exp.Query, exp.Select, exp.Union, exp.Intersect, exp.Except)):
            return False
        with_expr = query.args.get("with_") if isinstance(query, exp.Expression) else None
        local = set(cte_names)
        if isinstance(with_expr, exp.With):
            if with_expr.args.get("recursive"):
                return False
            aliases = {cte.alias_or_name.casefold() for cte in with_expr.expressions}
            dependencies: dict[str, set[str]] = {alias: set() for alias in aliases}
            for cte in with_expr.expressions:
                alias = cte.alias_or_name.casefold()
                for table in cte.this.find_all(exp.Table):
                    parts = _parts(table)
                    if parts is not None and len(parts) == 1 and parts[0].casefold() in aliases:
                        dependencies[alias].add(parts[0].casefold())

            def cyclic(name: str, trail: frozenset[str]) -> bool:
                if name in trail:
                    return True
                return any(cyclic(child, trail | {name}) for child in dependencies[name])

            if any(cyclic(alias, frozenset()) for alias in aliases):
                return False
            for cte in with_expr.expressions:
                alias = cte.alias_or_name.casefold()
                if not isinstance(
                    cte.this, (exp.Query, exp.Select, exp.Union, exp.Intersect, exp.Except)
                ):
                    return False
                if not visit_query(cte.this, frozenset(local)):
                    return False
                local.add(alias)
        if isinstance(query, (exp.Union, exp.Intersect, exp.Except)):
            return visit_query(query.left, frozenset(local)) and visit_query(
                query.right, frozenset(local)
            )
        if isinstance(query, exp.Select):
            if query.args.get("into") is not None:
                return False
            sources: list[exp.Expression] = []
            from_clause = query.args.get("from_")
            if isinstance(from_clause, exp.From) and from_clause.this is not None:
                sources.append(from_clause.this)
                sources.extend(from_clause.expressions)
            sources.extend(
                join.this for join in query.args.get("joins") or () if isinstance(join, exp.Join)
            )
            for source in sources:
                if isinstance(source, exp.Table):
                    parts = _parts(source)
                    location = _table_location(source, item, batch)
                    if (
                        parts is None
                        or len(parts) not in {1, 2}
                        or _temporary_table(source)
                        or any(_temporary(part) for part in parts)
                    ):
                        return False
                    if len(parts) == 1 and parts[0].casefold() in local:
                        continue
                    if location is None:
                        return False
                    result.append(
                        (tuple(part.casefold() for part in parts), ".".join(parts), location)
                    )
                elif isinstance(source, exp.Subquery):
                    if not visit_query(source.this, frozenset(local)):
                        return False
                else:
                    return False
            # Nested subqueries in projections/filters are still query scopes.
            for nested in query.find_all(exp.Subquery):
                if not visit_query(nested.this, frozenset(local)):
                    return False
        return True

    if not visit_query(expression, frozenset()):
        return None
    return result


def _valid_index(tree: Any) -> bool:
    index = tree.this
    if not isinstance(index, exp.Index):
        return False
    target = index.args.get("table")
    parts = _parts(target) if isinstance(target, exp.Table) else None
    params = index.args.get("params")
    return bool(
        parts
        and len(parts) in {1, 2}
        and isinstance(target, exp.Table)
        and not _temporary_table(target)
        and not any(_temporary(part) for part in parts)
        and isinstance(params, exp.IndexParameters)
        and params.args.get("columns")
    )


def _neutral_statement(tree: exp.Expression) -> bool:
    if isinstance(tree, exp.Execute):
        target = tree.this
        parts = _parts(target) if isinstance(target, exp.Table) else None
        return bool(
            parts
            and len(parts) == 2
            and parts[0].casefold() == "sys"
            and parts[1].casefold() in _PROPERTY_PROCEDURES
        )
    if isinstance(tree, exp.Alter):
        actions = tree.args.get("actions") or []
        return bool(actions) and all(
            isinstance(action, exp.ColumnDef)
            and not any(
                isinstance(child, (exp.Reference, exp.ForeignKey)) for child in action.walk()
            )
            for action in actions
        )
    return False


def _unsupported(
    tree: exp.Expression, item: _File, batch: _Batch, diagnostics: list[Diagnostic]
) -> None:
    location = _identifier_location(tree, item.raw.relative, item.line_index, batch.start)
    diagnostics.append(
        Diagnostic(
            DiagnosticCode.UNSUPPORTED_SYNTAX,
            item.raw.relative,
            "unsupported T-SQL syntax",
            location,
        )
    )


def _scan_batches(
    source: str, path: str, line_index: LineIndex
) -> tuple[list[_Batch], list[Diagnostic], bool]:
    batches: list[_Batch] = []
    diagnostics: list[Diagnostic] = []
    batch_start = 0
    line_start = 0
    state = "normal"
    block_depth = 0
    while line_start <= len(source):
        line_end = source.find("\n", line_start)
        if line_end < 0:
            line_end = len(source)
            next_start = len(source) + 1
        else:
            next_start = line_end + 1
        raw_line = source[line_start:line_end]
        command_line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if state == "normal":
            match = _GO_RE.fullmatch(command_line)
            if match:
                batches.append(
                    _Batch(
                        source[batch_start:line_start],
                        batch_start,
                        line_start,
                        match.group(1) is not None,
                    )
                )
                if match.group(1) is not None:
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticCode.UNSUPPORTED_SYNTAX,
                            path,
                            "GO count is unsupported",
                            _line_location(path, line_index, line_start, line_end),
                        )
                    )
                batch_start = next_start
                line_start = next_start
                continue
        i = line_start
        while i < line_end:
            char = source[i]
            nxt = source[i + 1] if i + 1 < len(source) else ""
            if state == "normal":
                if char == "-" and nxt == "-":
                    i = line_end
                    break
                if char == "/" and nxt == "*":
                    state = "block"
                    block_depth = 1
                    i += 2
                    continue
                if char == "'":
                    state = "single"
                elif char == '"':
                    state = "double"
                elif char == "[":
                    state = "bracket"
            elif state == "single":
                if char == "'":
                    if nxt == "'":
                        i += 2
                        continue
                    state = "normal"
            elif state == "double":
                if char == '"':
                    if nxt == '"':
                        i += 2
                        continue
                    state = "normal"
            elif state == "bracket" and char == "]":
                if nxt == "]":
                    i += 2
                    continue
                state = "normal"
            elif state == "block":
                if char == "/" and nxt == "*":
                    block_depth += 1
                    i += 2
                    continue
                if char == "*" and nxt == "/":
                    block_depth -= 1
                    i += 2
                    if block_depth == 0:
                        state = "normal"
                    continue
            i += 1
        if next_start > len(source):
            break
        line_start = next_start
    if state != "normal":
        diagnostics.append(
            Diagnostic(DiagnosticCode.PARSE_ERROR, path, "unterminated SQL lexical state")
        )
        return [], diagnostics, False
    if batch_start <= len(source):
        batches.append(_Batch(source[batch_start:], batch_start, len(source)))
    return batches, diagnostics, True


def _line_location(path: str, line_index: LineIndex, start: int, end: int) -> Location:
    return Location(path, Range(line_index.position(start), line_index.position(end)))
