"""Bounded static Python-to-Minotaur interpreter.

The v1 slice deliberately establishes only declarations, containment, local
and workspace-module imports, and direct calls with a statically known target.
Everything else is preserved as an unresolved reference; no runtime claim is
made and no source code is executed or imported.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence, Producer
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    Provenance,
    RelationshipKind,
    SymbolKind,
)
from minotaur.graph_model.relationship import Relationship
from minotaur.language_interpreter.contract import AnalysisResult, Diagnostic, DiagnosticCode
from minotaur.language_interpreter.python.discovery import discover_python_files
from minotaur.language_interpreter.python.parsing import parse_python
from minotaur.language_interpreter.workspace import Workspace

_NAMESPACE = "minotaur-python"
_PRODUCER = Producer(name="minotaur-python")


@dataclass(frozen=True, slots=True)
class _Module:
    path: str
    name: str
    is_package: bool
    tree: ast.Module
    source: str
    file_id: str
    module_id: str


class _ScopeCallVisitor(ast.NodeVisitor):
    """Collect calls nested within one top-level lexical scope."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []
        self.references: list[ast.Name | ast.Attribute] = []
        self._call_func_depth = 0

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        # The callable expression is represented by the calls relationship;
        # only its arguments can contribute independent references. Keep
        # traversing the callable expression so nested calls are preserved,
        # while suppressing loads from that expression.
        enclosing_func_depth = self._call_func_depth
        self._call_func_depth += 1
        self.visit(node.func)
        self._call_func_depth = 0
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._call_func_depth = enclosing_func_depth

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and self._call_func_depth == 0:
            self.references.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # A nested class body executes while the enclosing scope is active, but
        # methods of that class execute in their own scope and remain outside
        # this visitor's ownership.
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(statement)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and self._call_func_depth == 0:
            self.references.append(node)
        self.generic_visit(node)


def analyze_python_workspace(root: Path) -> AnalysisResult:
    """Analyze a Python workspace into one UTF-8 Minotaur graph document.

    A malformed or unreadable file produces a diagnostic and contributes no
    facts. Other files continue to be analyzed, so one editor-in-progress file
    cannot silently erase the rest of a workspace graph.
    """
    # Keep the original full-workspace API as a small wrapper. Existing library
    # callers retain their behavior while the CLI can analyze a narrower set of
    # files without duplicating Python parsing and graph-building logic.
    workspace = Workspace(root)
    return analyze_python_files(workspace, discover_python_files(workspace))


def analyze_python_files(workspace: Workspace, files: tuple[Path, ...]) -> AnalysisResult:
    """Analyze selected Python files rooted in ``workspace``.

    Callers own source selection and must provide existing regular files below
    the workspace root. Keeping that policy outside this interpreter makes it
    reusable by all languages and avoids Python-specific path-validation rules.
    Files are normalized into root-relative order here as a second defensive
    layer: API callers may supply arbitrary order, but graph bytes and emitted
    diagnostics must not change merely because argument order changed.
    """
    diagnostics: list[Diagnostic] = []
    modules: list[_Module] = []
    nodes: list[Node] = []

    for file_path in sorted(files, key=lambda path: path.relative_to(workspace.root).as_posix()):
        relative = file_path.relative_to(workspace.root).as_posix()
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            diagnostics.append(Diagnostic(DiagnosticCode.SOURCE_READ_ERROR, relative, str(error)))
            continue
        try:
            parsed = parse_python(source, relative)
        except SyntaxError as error:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.PARSE_ERROR,
                    relative,
                    error.msg,
                    _syntax_location(relative, error),
                )
            )
            continue
        module = _make_module(relative, parsed.tree, source)
        modules.append(module)
        nodes.extend((_file_node(relative), _module_node(module)))

    module_by_name = {module.name: module for module in modules}
    declarations: dict[str, str] = {}
    declarations_by_path: dict[str, dict[str, str]] = {}
    containers: dict[str, str] = {}
    for module in modules:
        declared, declared_containers, declared_nodes = _declarations(module)
        declarations.update(declared)
        declarations_by_path[module.path] = declared
        containers.update(declared_containers)
        nodes.extend(declared_nodes)

    relationships: dict[tuple[str, str, str], list[Location]] = defaultdict(list)
    for module in modules:
        _append(
            relationships, module.file_id, module.module_id, RelationshipKind.CONTAINS.value, None
        )
        _analyze_module(
            module,
            module_by_name,
            declarations_by_path[module.path],
            declarations,
            containers,
            relationships,
            nodes,
        )

    return AnalysisResult(
        GraphDocument(
            coordinate_encoding=CoordinateEncoding.UTF_8,
            nodes=tuple(nodes),
            relationships=tuple(
                _relationship(key, locations) for key, locations in relationships.items()
            ),
            generated_by=_PRODUCER,
        ),
        tuple(diagnostics),
    )


def _make_module(path: str, tree: ast.Module, source: str) -> _Module:
    name = _module_name(path)
    file_identity = NodeIdentity(IdentityBasis.FILE_PATH, _NAMESPACE)
    file_id = compute_node_id(file_identity, node_class=NodeClass.FILE.value, path=path)
    module_identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, _NAMESPACE)
    location = _module_location(path, source)
    module_id = compute_node_id(
        module_identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=SymbolKind.MODULE.value,
        location=location,
    )
    return _Module(
        path, name, path.rsplit("/", 1)[-1] == "__init__.py", tree, source, file_id, module_id
    )


def _file_node(path: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, _NAMESPACE)
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path=path),
        identity=identity,
        node_class=NodeClass.FILE,
        label=path,
        path=path,
        language="python",
    )


def _module_node(module: _Module) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, _NAMESPACE)
    location = _module_location(module.path, module.source)
    return Node(
        id=module.module_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=module.name,
        symbol_kind=SymbolKind.MODULE.value,
        language="python",
        location=location,
    )


def _declarations(module: _Module) -> tuple[dict[str, str], dict[str, str], list[Node]]:
    declarations: dict[str, str] = {module.name: module.module_id}
    containers: dict[str, str] = {}
    nodes: list[Node] = []
    for statement in module.tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified = f"{module.name}.{statement.name}"
            kind = SymbolKind.CLASS if isinstance(statement, ast.ClassDef) else SymbolKind.FUNCTION
            node = _symbol_node(module.path, statement, qualified, kind)
            declarations[qualified] = node.id
            containers[qualified] = module.module_id
            nodes.append(node)
            if isinstance(statement, ast.ClassDef):
                for member in statement.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        member_name = f"{qualified}.{member.name}"
                        member_node = _symbol_node(
                            module.path, member, member_name, SymbolKind.METHOD
                        )
                        declarations[member_name] = member_node.id
                        containers[member_name] = node.id
                        nodes.append(member_node)
    return declarations, containers, nodes


def _analyze_module(
    module: _Module,
    modules: dict[str, _Module],
    local_declarations: dict[str, str],
    declarations: dict[str, str],
    containers: dict[str, str],
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
) -> None:
    aliases = _imports(module, modules, relationships, nodes)
    for qualified, node_id in local_declarations.items():
        container = containers.get(qualified)
        if container is None:
            continue
        _append(relationships, container, node_id, RelationshipKind.CONTAINS.value, None)
    _calls(
        [
            statement
            for statement in module.tree.body
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ],
        declarations,
        aliases,
        module.name,
        module.path,
        module.module_id,
        relationships,
        nodes,
    )
    for statement in module.tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _calls(
                statement.body,
                declarations,
                aliases,
                module.name,
                module.path,
                declarations[f"{module.name}.{statement.name}"],
                relationships,
                nodes,
            )
        elif isinstance(statement, ast.ClassDef):
            for member in statement.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _calls(
                        member.body,
                        declarations,
                        aliases,
                        module.name,
                        module.path,
                        declarations[f"{module.name}.{statement.name}.{member.name}"],
                        relationships,
                        nodes,
                        statement.name,
                    )


def _imports(
    module: _Module,
    modules: dict[str, _Module],
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in module.tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                target = modules.get(alias.name)
                if target is None:
                    _unresolved(
                        module.module_id,
                        alias.name,
                        _location(module.path, statement),
                        relationships,
                        nodes,
                    )
                else:
                    _append(
                        relationships,
                        module.module_id,
                        target.module_id,
                        RelationshipKind.IMPORTS.value,
                        _location(module.path, statement),
                    )
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(statement, ast.ImportFrom):
            base = _relative_module(
                module.name, module.is_package, statement.module, statement.level
            )
            target_module = modules.get(base) if base is not None else None
            for alias in statement.names:
                reference = f"{base}.{alias.name}" if base else alias.name
                resolved_target = declarations_for_module(modules, reference)
                if resolved_target is None:
                    _unresolved(
                        module.module_id,
                        reference,
                        _location(module.path, statement),
                        relationships,
                        nodes,
                    )
                else:
                    _append(
                        relationships,
                        module.module_id,
                        resolved_target,
                        RelationshipKind.IMPORTS.value,
                        _location(module.path, statement),
                    )
                    aliases[alias.asname or alias.name] = reference
            if target_module is not None and base is not None:
                aliases.setdefault(base.rsplit(".", 1)[-1], base)
    return aliases


def declarations_for_module(modules: dict[str, _Module], reference: str) -> str | None:
    """Resolve a module or its top-level declaration without importing it."""
    direct_module = modules.get(reference)
    if direct_module is not None:
        return direct_module.module_id
    module_name, _, member = reference.rpartition(".")
    module = modules.get(module_name)
    if module is None:
        return None
    if not member:
        return module.module_id
    for statement in module.tree.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and statement.name == member
        ):
            kind = SymbolKind.CLASS if isinstance(statement, ast.ClassDef) else SymbolKind.FUNCTION
            return _symbol_node(module.path, statement, f"{module.name}.{member}", kind).id
    return None


def _calls(
    statements: list[ast.stmt],
    declarations: dict[str, str],
    aliases: dict[str, str],
    module_name: str,
    path: str,
    caller: str,
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
    class_name: str | None = None,
) -> None:
    visitor = _ScopeCallVisitor()
    for statement in statements:
        visitor.visit(statement)
    for candidate in visitor.calls:
        text = _expression_text(candidate.func)
        target = _resolve_call(text, declarations, aliases, module_name, class_name)
        location = _location(path, candidate.func)
        if target is None:
            _unresolved(caller, text, location, relationships, nodes)
        else:
            _append(relationships, caller, target, RelationshipKind.CALLS.value, location)
    for reference in visitor.references:
        text = _expression_text(reference)
        target = _resolve_call(text, declarations, aliases, module_name, class_name)
        if target is not None:
            _append(
                relationships,
                caller,
                target,
                RelationshipKind.REFERENCES.value,
                _location(path, reference),
            )


def _resolve_call(
    text: str,
    declarations: dict[str, str],
    aliases: dict[str, str],
    module_name: str,
    class_name: str | None,
) -> str | None:
    if "." not in text:
        target_name = aliases.get(text, f"{module_name}.{text}")
        return declarations.get(target_name)
    head, _, tail = text.partition(".")
    if head == "self" and class_name is not None:
        return declarations.get(f"{module_name}.{class_name}.{tail}")
    alias_target_name = aliases.get(head)
    if alias_target_name is not None:
        return declarations.get(f"{alias_target_name}.{tail}")
    return declarations.get(f"{module_name}.{text}")


def _unresolved(
    origin: str,
    text: str,
    location: Location,
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
) -> None:
    identity = NodeIdentity(IdentityBasis.UNRESOLVED_REFERENCE, _NAMESPACE, originating_node=origin)
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE.value,
        location=location,
        reference_text=text,
    )
    if not any(node.id == node_id for node in nodes):
        nodes.append(
            Node(
                id=node_id,
                identity=identity,
                node_class=NodeClass.UNRESOLVED_REFERENCE,
                label=text,
                reference_text=text,
                language="python",
                location=location,
            )
        )
    _append(relationships, origin, node_id, RelationshipKind.REFERENCES.value, location)


def _symbol_node(path: str, statement: ast.AST, label: str, kind: SymbolKind) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, _NAMESPACE)
    location = _location(path, statement)
    return Node(
        id=compute_node_id(
            identity, node_class=NodeClass.SYMBOL.value, symbol_kind=kind.value, location=location
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=kind.value,
        language="python",
        location=location,
    )


def _relationship(key: tuple[str, str, str], locations: list[Location]) -> Relationship:
    unique = tuple(dict.fromkeys(locations))
    evidence = Evidence(Provenance.STATIC_ANALYSIS, producer=_PRODUCER, locations=unique)
    return Relationship(source=key[0], target=key[1], kind=key[2], evidence=(evidence,))


def _append(
    relationships: dict[tuple[str, str, str], list[Location]],
    source: str,
    target: str,
    kind: str,
    location: Location | None,
) -> None:
    if location is not None:
        relationships[(source, target, kind)].append(location)
    else:
        relationships.setdefault((source, target, kind), [])


def _module_name(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "__init__"


def _relative_module(
    current: str, is_package: bool, imported: str | None, level: int
) -> str | None:
    if level == 0:
        return imported
    parts = current.split(".")
    if not is_package:
        parts.pop()
    if level > len(parts):
        return None
    base = parts[: len(parts) - level + 1]
    if imported:
        base.extend(imported.split("."))
    return ".".join(base)


def _module_location(path: str, source: str) -> Location:
    lines = source.splitlines(keepends=True)
    if not lines:
        return Location(path, Range(Position(0, 0), Position(0, 0)))
    last = lines[-1]
    return Location(
        path,
        Range(Position(0, 0), Position(len(lines) - 1, len(last.encode("utf-8").rstrip(b"\r\n")))),
    )


def _location(path: str, node: ast.AST) -> Location:
    start_line = getattr(node, "lineno", 1) - 1
    start_column = getattr(node, "col_offset", 0)
    end_line = getattr(node, "end_lineno", start_line + 1) - 1
    end_column = getattr(node, "end_col_offset", start_column)
    return Location(path, Range(Position(start_line, start_column), Position(end_line, end_column)))


def _syntax_location(path: str, error: SyntaxError) -> Location | None:
    if error.lineno is None:
        return None
    line = error.lineno - 1
    column = max((error.offset or 1) - 1, 0)
    return Location(path, Range(Position(line, column), Position(line, column)))


def _expression_text(expression: ast.expr) -> str:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return f"{_expression_text(expression.value)}.{expression.attr}"
    return ast.unparse(expression)
