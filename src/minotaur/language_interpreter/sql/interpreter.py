"""AST-authoritative, bounded T-SQL structural analysis.

SQLGlot is the only semantic authority in this module.  The small source
scanner exists only for batch boundaries and for proving that parser offsets
refer to the original source; it does not classify SQL statements.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlglot import exp, parse

from minotaur.config import SqlSettings
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
from minotaur.graph_model.relationship import Relationship
from minotaur.language_interpreter.accumulation import RelationshipAccumulator
from minotaur.language_interpreter.contract import (
    AnalysisResult,
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
)
from minotaur.language_interpreter.emission import NodeEmitter, file_node
from minotaur.language_interpreter.reading import RawSource, read_sources
from minotaur.language_interpreter.source_text import LineIndex
from minotaur.language_interpreter.sql.diagnostics import analyze_view_warnings
from minotaur.language_interpreter.workspace import Workspace

NAMESPACE = "minotaur-sql"
_PRODUCER = Producer(name=NAMESPACE)
_GO_RE = re.compile(r"[ \t]*GO(?:[ \t]+([0-9]+))?[ \t]*(?:--[^\r\n]*)?\Z", re.IGNORECASE)
_FK_ACTION_RE = re.compile(
    r"ON (DELETE|UPDATE) (?:CASCADE|SET NULL|SET DEFAULT|NO ACTION|RESTRICT)\Z",
    re.IGNORECASE,
)
_PROPERTY_PROCEDURES = frozenset(
    {"sp_addextendedproperty", "sp_updateextendedproperty", "sp_dropextendedproperty"}
)


class _SqlglotWarningFilter(logging.Filter):
    """Suppress parser fallback warnings produced by this analysis call."""

    def __init__(self) -> None:
        super().__init__()
        self._thread = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread != self._thread or record.levelno < logging.WARNING


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
class _ForeignKey:
    key: tuple[str, ...]
    text: str
    location: Location
    local_columns: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    constraint_name: str


@dataclass(frozen=True, slots=True)
class _Observation:
    owner: _Declaration | None
    reads: tuple[tuple[tuple[str, ...], str, Location], ...] = ()
    foreign_keys: tuple[_ForeignKey, ...] = ()
    source: tuple[tuple[str, ...], str, Location, str] | None = None


def analyze_sql_files(
    workspace: Workspace,
    files: tuple[Path, ...],
    sql_settings: SqlSettings | None = None,
    *,
    settings: SqlSettings | None = None,
) -> AnalysisResult:
    """Analyze selected SQL files through the shared final registry entry."""
    if sql_settings is not None and settings is not None:
        raise ValueError("pass only one SQL settings value")
    sql_settings = settings if settings is not None else sql_settings
    effective_settings = sql_settings if sql_settings is not None else SqlSettings()
    sources, diagnostics = read_sources(workspace, files)
    readable_paths = frozenset(source.relative for source in sources)
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
                trees = _parse_batch(batch.text)
            except Exception:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticCode.PARSE_ERROR, item.raw.relative, "unable to parse T-SQL batch"
                    )
                )
                continue
            function_continuation = False
            for tree in trees:
                if tree is None:
                    continue
                if function_continuation:
                    function_continuation = False
                    if isinstance(tree, exp.EndStatement):
                        continue
                result = _interpret_statement(tree, item, batch, diagnostics, declarations, nodes)
                if result is not None:
                    observations.append(result)
                function_continuation = (
                    isinstance(tree, exp.Create)
                    and str(tree.args.get("kind") or "").upper() == "FUNCTION"
                    and bool(tree.args.get("begin"))
                )

    by_key: dict[tuple[str, ...], list[_Declaration]] = {}
    for declaration in declarations:
        by_key.setdefault(declaration.key, []).append(declaration)
    for _key, values in by_key.items():
        if len(values) > 1:
            migration_count = sum(
                effective_settings.matches_migration(value.location.path) for value in values
            )
            if migration_count == len(values):
                category = "multi-migration"
            elif migration_count == 0:
                category = "multi-canonical"
            else:
                category = "canonical-and-migration"
            for value in values:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticCode.DUPLICATE_DECLARATION,
                        value.location.path,
                        "duplicate SQL declaration",
                        value.location,
                        DiagnosticSeverity.WARNING,
                        {NAMESPACE: {"category": category}},
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
    unresolved_fk_nodes: dict[tuple[str, str], str] = {}
    for observation in observations:
        owner = observation.owner
        if observation.source is not None:
            source_key, source_text, source_location, file_id = observation.source
            candidates = table_index.get(source_key, [])
            if len(candidates) != 1:
                if len(candidates) > 1:
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticCode.AMBIGUOUS_REFERENCE,
                            source_location.path,
                            "ambiguous SQL reference",
                            source_location,
                        )
                    )
                emitter.unresolved(file_id, source_text, source_location, nodes, relationships)
                continue
            owner = candidates[0]
        assert owner is not None
        for key, text, location in observation.reads:
            _resolve(
                owner,
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
        for foreign_key in observation.foreign_keys:
            _resolve_foreign_key(
                owner,
                foreign_key.key,
                foreign_key.text,
                foreign_key.location,
                table_index,
                relationships,
                nodes,
                diagnostics,
                emitter,
                "sql:foreign-key-to",
                "foreign-key",
                _foreign_key_extensions(foreign_key),
                effective_settings,
                readable_paths,
                foreign_key.constraint_name,
                unresolved_fk_nodes,
            )

    relationship_documents = relationships.documents(_PRODUCER)
    nodes, fk_components = _with_fk_components(nodes, relationship_documents)

    # Replace the generic references kind for reads with the SQL extension.
    # Resolution above uses the shared emitter only for unresolved nodes; the
    # successful edge is written directly with the requested SQL meaning.
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=tuple(nodes),
        relationships=relationship_documents,
        generated_by=_PRODUCER,
        extensions={NAMESPACE: {"fk_components": fk_components}},
    )
    diagnostics.sort(
        key=lambda item: (
            item.path,
            item.location.sort_key if item.location else ("", 0, 0, 0, 0),
            item.code.value,
            item.message,
        )
    )
    diagnostics.extend(analyze_view_warnings(document, sql_settings))
    diagnostics.sort(
        key=lambda item: (
            item.path,
            item.location.sort_key if item.location else ("", 0, 0, 0, 0),
            item.code.value,
            item.message,
        )
    )
    return AnalysisResult(document, tuple(diagnostics))


def _with_fk_components(
    nodes: list[Node], relationships: tuple[Relationship, ...]
) -> tuple[list[Node], list[dict[str, object]]]:
    """Attach deterministic FK component metadata to SQL table nodes."""
    table_ids = {node.id for node in nodes if node.symbol_kind == "sql:table"}
    parent = {node_id: node_id for node_id in table_ids}

    def find(node_id: str) -> str:
        root = node_id
        while parent[root] != root:
            root = parent[root]
        while parent[node_id] != node_id:
            next_node = parent[node_id]
            parent[node_id] = root
            node_id = next_node
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for relationship in relationships:
        if relationship.kind != "sql:foreign-key-to":
            continue
        if relationship.source in table_ids and relationship.target in table_ids:
            union(relationship.source, relationship.target)

    members_by_root: dict[str, list[str]] = {}
    for node_id in sorted(table_ids):
        members_by_root.setdefault(find(node_id), []).append(node_id)
    members = [tuple(group) for group in members_by_root.values()]
    members.sort(key=lambda group: (-len(group), group))
    summaries = [
        {"id": component_id, "size": len(group), "members": list(group)}
        for component_id, group in enumerate(members)
    ]
    component_by_node = {
        node_id: component_id for component_id, group in enumerate(members) for node_id in group
    }
    replaced: list[Node] = []
    for node in nodes:
        component_id = component_by_node.get(node.id)
        if component_id is None:
            replaced.append(node)
            continue
        extensions = {name: dict(value) for name, value in (node.extensions or {}).items()}
        sql_extensions = dict(extensions.get(NAMESPACE, {}))
        sql_extensions["fk_component"] = component_id
        extensions[NAMESPACE] = sql_extensions
        replaced.append(replace(node, extensions=extensions))
    return replaced, summaries


def _make_file(source: RawSource) -> _File:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, NAMESPACE)
    file_id = compute_node_id(identity, node_class=NodeClass.FILE.value, path=source.relative)
    return _File(
        source,
        LineIndex(source.source),
        file_id,
        file_node(source.relative, source.content, NAMESPACE, "sql"),
    )


def _parse_batch(source: str) -> list[exp.Expr | None]:
    logger = logging.getLogger("sqlglot")
    warning_filter = _SqlglotWarningFilter()
    logger.addFilter(warning_filter)
    try:
        return parse(source, read="tsql")
    finally:
        logger.removeFilter(warning_filter)


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
    extensions: dict[str, dict[str, object]] | None = None,
) -> None:
    candidates = primary.get(key, []) + secondary.get(key, [])
    if len(candidates) == 1:
        relationships.add(
            owner.node.id,
            candidates[0].node.id,
            relationship_kind,
            location,
            extensions,
        )
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


def _resolve_foreign_key(
    owner: _Declaration,
    key: tuple[str, ...],
    text: str,
    location: Location,
    primary: dict[tuple[str, ...], list[_Declaration]],
    relationships: RelationshipAccumulator,
    nodes: list[Node],
    diagnostics: list[Diagnostic],
    emitter: NodeEmitter,
    relationship_kind: str,
    relation: str,
    extensions: dict[str, dict[str, object]] | None,
    settings: SqlSettings,
    readable_paths: frozenset[str],
    constraint_name: str,
    unresolved_fk_nodes: dict[tuple[str, str], str],
) -> None:
    """Resolve one FK while retaining a per-observation orphan diagnostic."""
    candidates = primary.get(key, [])
    if len(candidates) == 1:
        relationships.add(
            owner.node.id,
            candidates[0].node.id,
            relationship_kind,
            location,
            extensions,
        )
        return
    reason = "ambiguous" if len(candidates) > 1 else "undeclared"
    if reason == "undeclared":
        mapped_path = settings.foreign_key_target_files.get(".".join(key))
        if mapped_path in readable_paths:
            reason = "extraction-gap"
    diagnostics.append(
        Diagnostic(
            DiagnosticCode.ORPHANED_FOREIGN_KEY,
            location.path,
            "orphaned foreign key",
            location,
            DiagnosticSeverity.WARNING,
            {
                NAMESPACE: {
                    "source_table": owner.label,
                    "constraint_name": constraint_name,
                    "target": text,
                    "reason": reason,
                }
            },
        )
    )
    _emit_coalesced_fk_unresolved(owner, text, location, nodes, relationships, unresolved_fk_nodes)
    _ = relation


def _emit_coalesced_fk_unresolved(
    owner: _Declaration,
    text: str,
    location: Location,
    nodes: list[Node],
    relationships: RelationshipAccumulator,
    unresolved_nodes: dict[tuple[str, str], str],
) -> None:
    """Keep one generic unresolved FK target per source and target text."""
    key = (owner.node.id, text)
    node_id = unresolved_nodes.get(key)
    if node_id is None:
        identity = NodeIdentity(
            IdentityBasis.UNRESOLVED_REFERENCE,
            NAMESPACE,
            originating_node=owner.node.id,
        )
        node_id = compute_node_id(
            identity,
            node_class=NodeClass.UNRESOLVED_REFERENCE.value,
            location=location,
            reference_text=text,
        )
        unresolved_nodes[key] = node_id
        nodes.append(
            Node(
                id=node_id,
                identity=identity,
                node_class=NodeClass.UNRESOLVED_REFERENCE,
                label=text,
                reference_text=text,
                language="sql",
                location=location,
            )
        )
    relationships.add(owner.node.id, node_id, RelationshipKind.REFERENCES.value, location)


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
    if isinstance(tree, exp.Alter):
        observation = _interpret_alter(tree, item, batch)
        if observation is not None:
            return observation
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
    if kind in {"PROCEDURE", "FUNCTION"}:
        if (
            tree.args.get("exists")
            or tree.args.get("clone")
            or tree.args.get("refresh")
            or _is_create_or_replace(batch.text)
        ):
            _unsupported(tree, item, batch, diagnostics)
            return None
        if (kind == "PROCEDURE" and isinstance(target, exp.StoredProcedure)) or (
            kind == "FUNCTION" and isinstance(target, exp.UserDefinedFunction)
        ):
            table = target.this
        elif kind == "FUNCTION" and isinstance(target, exp.Table):
            table = target
        else:
            _unsupported(tree, item, batch, diagnostics)
            return None
        if not isinstance(table, exp.Table):
            _unsupported(tree, item, batch, diagnostics)
            return None
        parts = _parts(table)
        if parts is None or len(parts) not in {1, 2} or _nonpersistent_table(table):
            _unsupported(tree, item, batch, diagnostics)
            return None
        location = _table_location(table, item, batch)
        if location is None:
            _unsupported(tree, item, batch, diagnostics)
            return None
        key = tuple(part.casefold() for part in parts)
        node = _sql_symbol_node(parts, f"sql:{kind.casefold()}", location)
        declaration = _Declaration(
            kind.casefold(), key, ".".join(parts), location, node, item.file_id
        )
        declarations.append(declaration)
        nodes.append(node)
        return _Observation(declaration)
    if kind in {"INDEX", "NONCLUSTERED INDEX", "CLUSTERED INDEX"}:
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
    if _nonpersistent_table(table):
        _unsupported(tree, item, batch, diagnostics)
        return None
    location = _table_location(table, item, batch)
    if location is None:
        _unsupported(tree, item, batch, diagnostics)
        return None
    key = tuple(part.casefold() for part in parts)
    node = _sql_symbol_node(parts, f"sql:{kind.casefold()}", location)
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


def _is_create_or_replace(source: str) -> bool:
    return re.match(r"\s*CREATE\s+OR\s+REPLACE\b", source, re.IGNORECASE) is not None


def _sql_symbol_node(parts: tuple[str, ...], kind: str, location: Location) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    return Node(
        id=compute_node_id(
            identity, node_class=NodeClass.SYMBOL.value, symbol_kind=kind, location=location
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=".".join(parts),
        symbol_kind=kind,
        language="sql",
        location=location,
        extensions={NAMESPACE: {"name": parts[-1]}},
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


def _nonpersistent_table(table: exp.Table) -> bool:
    identifiers = (
        table.args.get("catalog"),
        table.args.get("db"),
        table.args.get("this"),
    )
    return any(
        isinstance(identifier, exp.Identifier)
        and bool(identifier.args.get("temporary") or identifier.args.get("global_"))
        for identifier in identifiers
    )


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
) -> list[_ForeignKey] | None:
    if not isinstance(target, exp.Schema):
        return []
    result: list[_ForeignKey] = []
    for reference in target.find_all(exp.Reference):
        parent = reference.parent
        local_columns: tuple[str, ...]
        if isinstance(parent, exp.ColumnConstraint) and isinstance(parent.parent, exp.ColumnDef):
            column = parent.parent.this
            local_columns = (str(column.this),) if isinstance(column, exp.Identifier) else ()
            constraint_name = (
                str(parent.this.this) if isinstance(parent.this, exp.Identifier) else "unnamed"
            )
        elif isinstance(parent, exp.ForeignKey):
            local_columns = (
                tuple(str(column.this) for column in parent.expressions)
                if all(isinstance(column, exp.Identifier) for column in parent.expressions)
                else ()
            )
            constraint = parent.parent
            constraint_name = (
                str(constraint.this.this)
                if isinstance(constraint, exp.Constraint)
                and isinstance(constraint.this, exp.Identifier)
                else "unnamed"
            )
        else:
            local_columns = ()
            constraint_name = "unnamed"
        foreign_key = _foreign_key_details(
            reference, local_columns, item, batch, constraint_name=constraint_name
        )
        if foreign_key is None:
            return None
        result.append(foreign_key)
    return result


def _reference_target(
    reference: exp.Reference, item: _File, batch: _Batch
) -> tuple[tuple[str, ...], str, Location] | None:
    referenced = reference.this.this if isinstance(reference.this, exp.Schema) else None
    if not isinstance(referenced, exp.Table):
        return None
    parts = _parts(referenced)
    location = _table_location(referenced, item, batch)
    if (
        parts is None
        or len(parts) not in {1, 2}
        or _nonpersistent_table(referenced)
        or location is None
    ):
        return None
    return tuple(part.casefold() for part in parts), ".".join(parts), location


def _foreign_key_details(
    reference: exp.Reference,
    local_columns: tuple[str, ...],
    item: _File,
    batch: _Batch,
    *,
    constraint_name: str = "unnamed",
) -> _ForeignKey | None:
    endpoint = _reference_target(reference, item, batch)
    if endpoint is None:
        return None
    referenced = reference.this
    referenced_columns = (
        tuple(str(column.this) for column in referenced.expressions)
        if isinstance(referenced, exp.Schema)
        and all(isinstance(column, exp.Identifier) for column in referenced.expressions)
        else ()
    )
    return _ForeignKey(*endpoint, local_columns, referenced_columns, constraint_name)


def _foreign_key_extensions(
    foreign_key: _ForeignKey,
) -> dict[str, dict[str, object]] | None:
    if not foreign_key.local_columns or len(foreign_key.local_columns) != len(
        foreign_key.referenced_columns
    ):
        return None
    return {
        NAMESPACE: {
            "foreign_key_columns": [
                {"local": local, "referenced": referenced}
                for local, referenced in zip(
                    foreign_key.local_columns,
                    foreign_key.referenced_columns,
                    strict=True,
                )
            ]
        }
    }


def _valid_fk_options(reference: exp.Reference) -> bool:
    seen: set[str] = set()
    for option in reference.args.get("options") or []:
        if not isinstance(option, str):
            return False
        match = _FK_ACTION_RE.fullmatch(option)
        if match is None or match.group(1).upper() in seen:
            return False
        seen.add(match.group(1).upper())
    return True


def _interpret_alter(tree: exp.Alter, item: _File, batch: _Batch) -> _Observation | None:
    target = tree.this
    if not isinstance(target, exp.Table) or str(tree.args.get("kind") or "").upper() != "TABLE":
        return None
    if any(
        tree.args.get(option)
        for option in (
            "exists",
            "only",
            "options",
            "cluster",
            "not_valid",
            "check",
            "cascade",
            "iceberg",
        )
    ):
        return None
    parts = _parts(target)
    location = _table_location(target, item, batch)
    if (
        parts is None
        or len(parts) not in {1, 2}
        or _nonpersistent_table(target)
        or location is None
    ):
        return None
    actions = tree.args.get("actions") or []
    if not actions or not all(isinstance(action, exp.AddConstraint) for action in actions):
        return None
    foreign_keys: list[_ForeignKey] = []
    for action in actions:
        constraints = action.args.get("expressions") or []
        if not constraints:
            return None
        for constraint in constraints:
            if not isinstance(constraint, exp.Constraint) or not isinstance(
                constraint.this, exp.Identifier
            ):
                return None
            expressions = constraint.args.get("expressions") or []
            if not expressions or not isinstance(expressions[0], exp.ForeignKey):
                return None
            if len(expressions) > 2 or not all(
                isinstance(extra, exp.NotForReplicationColumnConstraint)
                for extra in expressions[1:]
            ):
                return None
            reference = expressions[0].args.get("reference")
            if not isinstance(reference, exp.Reference) or not _valid_fk_options(reference):
                return None
            local_columns = (
                tuple(str(column.this) for column in expressions[0].expressions)
                if all(isinstance(column, exp.Identifier) for column in expressions[0].expressions)
                else ()
            )
            foreign_key = _foreign_key_details(
                reference, local_columns, item, batch, constraint_name=str(constraint.this.this)
            )
            if foreign_key is None:
                return None
            foreign_keys.append(foreign_key)
    return _Observation(
        None,
        (),
        tuple(foreign_keys),
        (tuple(part.casefold() for part in parts), ".".join(parts), location, item.file_id),
    )


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
                for table in _tables_outside_nested_with(cte.this):
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
                    if parts is None or len(parts) not in {1, 2} or _nonpersistent_table(source):
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


def _tables_outside_nested_with(expression: Any) -> Iterable[exp.Table]:
    """Yield table references in one CTE scope without entering nested scopes."""
    if isinstance(expression, exp.Query) and expression.args.get("with_") is not None:
        return
    if isinstance(expression, exp.Table):
        yield expression
        return
    if not isinstance(expression, exp.Expression):
        return
    for child in expression.iter_expressions():
        yield from _tables_outside_nested_with(child)


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
        and not _nonpersistent_table(target)
        and isinstance(params, exp.IndexParameters)
        and params.args.get("columns")
    )


def _neutral_statement(tree: exp.Expression) -> bool:
    if isinstance(tree, exp.Execute):
        return _property_execute_target(tree.this)
    if isinstance(tree, exp.Alter):
        target = tree.this
        parts = _parts(target) if isinstance(target, exp.Table) else None
        actions = tree.args.get("actions") or []
        return bool(
            str(tree.args.get("kind") or "").upper() == "TABLE"
            and parts
            and len(parts) in {1, 2}
            and isinstance(target, exp.Table)
            and not _nonpersistent_table(target)
            and actions
        ) and all(
            isinstance(action, exp.ColumnDef)
            and not any(
                isinstance(child, (exp.Reference, exp.ForeignKey)) for child in action.walk()
            )
            for action in actions
        )
    return False


def _property_execute_target(target: exp.Expression) -> bool:
    if not isinstance(target, exp.Table):
        return False
    procedure = target.args.get("this")
    if not isinstance(procedure, exp.Identifier):
        return False
    if target.args.get("catalog") is not None:
        return False
    database = target.args.get("db")
    if database is not None and (
        not isinstance(database, exp.Identifier) or str(database.this).casefold() != "sys"
    ):
        return False
    return str(procedure.this).casefold() in _PROPERTY_PROCEDURES


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
