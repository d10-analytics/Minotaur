"""Bounded static Python-to-Minotaur interpreter.

The v1 slice deliberately establishes only declarations, containment, local
and workspace-module imports, and direct calls with a statically known target.
Everything else is preserved as an unresolved reference; no runtime claim is
made and no source code is executed or imported.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    RelationshipKind,
    SymbolKind,
)
from minotaur.language_interpreter.accumulation import RelationshipAccumulator
from minotaur.language_interpreter.contract import (
    IMPORT_ROOT_HINT,
    IMPORTS_RESOLVED,
    IMPORTS_ROOT_MISMATCHED,
    IMPORTS_UNRESOLVED,
    AnalysisResult,
    Diagnostic,
    DiagnosticCode,
)
from minotaur.language_interpreter.emission import NodeEmitter, symbol_node
from minotaur.language_interpreter.python.discovery import discover_python_files
from minotaur.language_interpreter.python.parsing import parse_python
from minotaur.language_interpreter.source_text import LineIndex
from minotaur.language_interpreter.workspace import Workspace

NAMESPACE = "minotaur-python"
_PRODUCER = Producer(name="minotaur-python")


@dataclass(frozen=True, slots=True)
class _Module:
    path: str
    name: str
    is_package: bool
    tree: ast.Module
    source: str
    line_index: LineIndex
    file_id: str
    module_id: str


@dataclass(frozen=True, slots=True)
class _DeclaredSymbol:
    node_id: str
    container_id: str
    class_declarations: Mapping[str, str] | None = None


@dataclass
class _ImportTally:
    """Counts of import statements that did or did not resolve in the workspace.

    ``root_mismatched`` counts unresolved imports whose dotted name is a
    strict suffix of an analyzed module name (``import minotaur.cli`` while
    the graph knows ``src.minotaur.cli``). That is the signature of a
    ``--root`` that does not match the package layout, which silently turns
    every cross-module call into an unresolved reference; the CLI warns from
    these counts. Third-party and out-of-selection imports are unresolved but
    not mismatched, so they never trigger the warning.
    """

    resolved: int = 0
    unresolved: int = 0
    root_mismatched: int = 0
    prefixes: dict[str, int] = field(default_factory=dict)
    _suffixes: dict[str, str] | None = None

    def note_unresolved(self, name: str, modules: Mapping[str, object]) -> None:
        self.unresolved += 1
        if self._suffixes is None:
            self._suffixes = _module_suffixes(modules)
        prefix = self._suffixes.get(name)
        if prefix is None and "." in name:
            # ``from pkg.mod import symbol``: the module part is what must match.
            prefix = self._suffixes.get(name.rsplit(".", 1)[0])
        if prefix is not None:
            self.root_mismatched += 1
            self.prefixes[prefix] = self.prefixes.get(prefix, 0) + 1

    @property
    def root_hint(self) -> str | None:
        """The most common missing prefix as a root-relative directory."""
        if not self.prefixes:
            return None
        prefix = max(sorted(self.prefixes), key=self.prefixes.__getitem__)
        return prefix.replace(".", "/")


def _module_suffixes(modules: Mapping[str, object]) -> dict[str, str]:
    suffixes: dict[str, str] = {}
    for name in modules:
        parts = name.split(".")
        for index in range(1, len(parts)):
            suffixes.setdefault(".".join(parts[index:]), ".".join(parts[:index]))
    return suffixes


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
        # Reset to 0 (rather than leaving the incremented depth in place)
        # before visiting arguments: a call's arguments are never part of
        # a callee expression, even when this call itself sits inside an
        # enclosing call's callee expression. For `f(g)(h)`, the outer
        # visit_Call increments depth while visiting `f(g)` as its callee,
        # but `g` is an argument of the inner call and must still be
        # recorded as a reference despite the outer suppression.
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
        # this visitor's ownership. The class header (decorators, bases, and
        # keywords such as ``metaclass=``) is evaluated in the enclosing scope
        # as well, so a base class is a real dependency of that scope.
        for header in _class_header_nodes(node):
            self.visit(header)
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
            content = file_path.read_bytes()
            source = content.decode("utf-8")
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
        module = _make_module(relative, parsed.tree, source, LineIndex(source))
        modules.append(module)
        nodes.extend(
            (_file_node(relative, hashlib.sha256(content).hexdigest()), _module_node(module))
        )

    module_by_name = {module.name: module for module in modules}
    declarations: dict[str, str] = {}
    symbols_by_path: dict[str, dict[ast.stmt, _DeclaredSymbol]] = {}
    for module in modules:
        declared, symbols, declared_nodes = _declarations(module)
        declarations.update(declared)
        symbols_by_path[module.path] = symbols
        nodes.extend(declared_nodes)

    relationships = RelationshipAccumulator()
    tally = _ImportTally()
    emitter = NodeEmitter(NAMESPACE, "python")
    for module in modules:
        relationships.add(module.file_id, module.module_id, RelationshipKind.CONTAINS.value, None)
        _analyze_module(
            module,
            module_by_name,
            symbols_by_path[module.path],
            declarations,
            relationships,
            nodes,
            emitter,
            tally,
        )

    return AnalysisResult(
        GraphDocument(
            coordinate_encoding=CoordinateEncoding.UTF_8,
            nodes=tuple(nodes),
            relationships=relationships.documents(_PRODUCER),
            generated_by=_PRODUCER,
            # Flat keys: extension namespaces hold scalar-valued objects.
            extensions={
                "minotaur-python": {
                    IMPORTS_RESOLVED: tally.resolved,
                    IMPORTS_UNRESOLVED: tally.unresolved,
                    IMPORTS_ROOT_MISMATCHED: tally.root_mismatched,
                    **({IMPORT_ROOT_HINT: tally.root_hint} if tally.root_hint else {}),
                }
            },
        ),
        tuple(diagnostics),
    )


def _make_module(path: str, tree: ast.Module, source: str, line_index: LineIndex) -> _Module:
    name = _module_name(path)
    file_identity = NodeIdentity(IdentityBasis.FILE_PATH, NAMESPACE)
    file_id = compute_node_id(file_identity, node_class=NodeClass.FILE.value, path=path)
    module_identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    location = _module_location(path, line_index)
    module_id = compute_node_id(
        module_identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=SymbolKind.MODULE.value,
        location=location,
    )
    return _Module(
        path,
        name,
        path.rsplit("/", 1)[-1] == "__init__.py",
        tree,
        source,
        line_index,
        file_id,
        module_id,
    )


def _file_node(path: str, content_sha256: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, NAMESPACE)
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path=path),
        identity=identity,
        node_class=NodeClass.FILE,
        label=path,
        path=path,
        language="python",
        extensions={"minotaur-python": {"content_sha256": content_sha256}},
    )


def _module_node(module: _Module) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    location = _module_location(module.path, module.line_index)
    return Node(
        id=module.module_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=module.name,
        symbol_kind=SymbolKind.MODULE.value,
        language="python",
        location=location,
    )


def _declarations(
    module: _Module,
) -> tuple[dict[str, str], dict[ast.stmt, _DeclaredSymbol], list[Node]]:
    declarations: dict[str, str] = {module.name: module.module_id}
    symbols: dict[ast.stmt, _DeclaredSymbol] = {}
    nodes: list[Node] = []
    for statement in module.tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified = f"{module.name}.{statement.name}"
            kind = SymbolKind.CLASS if isinstance(statement, ast.ClassDef) else SymbolKind.FUNCTION
            node = symbol_node(
                qualified,
                kind,
                _location(module.path, statement),
                NAMESPACE,
                "python",
            )
            declarations[qualified] = node.id
            symbols[statement] = _DeclaredSymbol(node.id, module.module_id)
            nodes.append(node)
            if isinstance(statement, ast.ClassDef):
                # The module-wide declarations table intentionally models the
                # name bindings left after the module body executes. Two
                # ``class C`` statements therefore share qualified labels, and
                # methods from the later statement overwrite methods from the
                # earlier one in that table. That is correct for an explicit
                # global lookup such as ``C.run`` but not for ``self.run``:
                # an instance remains bound to the exact class object that
                # created it even when the module name is rebound later.
                #
                # Keep one last-wins method table per ClassDef and attach that
                # same table to the class and all of its direct methods. This
                # preserves repeated-method behavior within one class while
                # preventing resolution from crossing between two same-named
                # class statements.
                class_declarations: dict[str, str] = {}
                declared_members: list[tuple[ast.stmt, Node]] = []
                for member in statement.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        member_name = f"{qualified}.{member.name}"
                        member_node = symbol_node(
                            member_name,
                            SymbolKind.METHOD,
                            _location(module.path, member),
                            NAMESPACE,
                            "python",
                        )
                        declarations[member_name] = member_node.id
                        class_declarations[member.name] = member_node.id
                        declared_members.append((member, member_node))
                        nodes.append(member_node)
                symbols[statement] = _DeclaredSymbol(node.id, module.module_id, class_declarations)
                for member, member_node in declared_members:
                    symbols[member] = _DeclaredSymbol(member_node.id, node.id, class_declarations)
    return declarations, symbols, nodes


def _analyze_module(
    module: _Module,
    modules: dict[str, _Module],
    symbols: dict[ast.stmt, _DeclaredSymbol],
    declarations: dict[str, str],
    relationships: RelationshipAccumulator,
    nodes: list[Node],
    emitter: NodeEmitter,
    tally: _ImportTally,
) -> None:
    aliases = _imports(module, modules, relationships, nodes, emitter, tally)
    for symbol in symbols.values():
        relationships.add(
            symbol.container_id,
            symbol.node_id,
            RelationshipKind.CONTAINS.value,
            None,
        )
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
        emitter,
    )
    for statement in module.tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _decorator_references(
                statement,
                module.module_id,
                symbols[statement].node_id,
                module.path,
                relationships,
            )
            _calls(
                statement.body,
                declarations,
                aliases,
                module.name,
                module.path,
                symbols[statement].node_id,
                relationships,
                nodes,
                emitter,
                prefix_nodes=_signature_nodes(statement),
            )
        elif isinstance(statement, ast.ClassDef):
            _decorator_references(
                statement,
                module.module_id,
                symbols[statement].node_id,
                module.path,
                relationships,
            )
            # A class body executes at definition time in the class scope, so
            # its non-method statements (dataclass field defaults, aliases such
            # as `handler = staticmethod(helper)`, descriptor construction) are
            # real calls and references and are attributed to the class node.
            # Methods are excluded here and analyzed below in their own scope,
            # matching how _ScopeCallVisitor.visit_ClassDef treats a nested
            # class body inside a function.
            _calls(
                [
                    member
                    for member in statement.body
                    if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                ],
                declarations,
                aliases,
                module.name,
                module.path,
                symbols[statement].node_id,
                relationships,
                nodes,
                emitter,
                symbols[statement].class_declarations,
                prefix_nodes=_class_header_nodes(statement),
            )
            for member in statement.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _decorator_references(
                        member,
                        symbols[statement].node_id,
                        symbols[member].node_id,
                        module.path,
                        relationships,
                    )
                    _calls(
                        member.body,
                        declarations,
                        aliases,
                        module.name,
                        module.path,
                        symbols[member].node_id,
                        relationships,
                        nodes,
                        emitter,
                        symbols[member].class_declarations,
                        prefix_nodes=_signature_nodes(member),
                    )


def _decorator_references(
    statement: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    source: str,
    target: str,
    path: str,
    relationships: RelationshipAccumulator,
) -> None:
    """Record each decorator as an enclosing-scope use of its definition.

    ``target`` is the node created for this exact statement. Name-based lookup
    is intentionally avoided because repeated definitions have distinct nodes.
    """
    for decorator in statement.decorator_list:
        relationships.add(
            source,
            target,
            RelationshipKind.REFERENCES.value,
            _location(path, decorator),
        )


def _class_header_nodes(node: ast.ClassDef) -> tuple[ast.AST, ...]:
    """Return the expressions evaluated by a class statement's header.

    ``class Sub(Base, metaclass=Meta)`` depends on ``Base`` and ``Meta`` just
    as a decorator depends on its callable. Without these, every base class is
    reported as unreferenced by ``query unreferenced`` whenever subclassing is
    its only use.
    """
    return (
        *node.decorator_list,
        *node.bases,
        *(keyword.value for keyword in node.keywords),
    )


def _signature_nodes(
    statement: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.AST, ...]:
    """Return the decorator and signature expressions of one definition.

    A function's decorators, default arguments, and annotations are all
    expressions evaluated outside its body, so a visitor given only
    ``statement.body`` never sees them. Nested definitions do get them, because
    ``_ScopeCallVisitor`` reaches a nested ``FunctionDef`` through
    ``generic_visit`` and therefore traverses its whole signature; collecting
    them here keeps top-level functions and methods consistent with nested ones
    instead of making attribution depend on nesting depth.

    Decorator expressions remain attributed to the decorated function or method
    for the outward edge to the decorator; ``_decorator_references`` separately
    records the enclosing scope's inward reference to the decorated symbol.

    Annotations count as references for the same reason calls do:
    ``def f(x: Handler)`` is a real dependency on ``Handler``, and an agent
    asking whether a symbol is still used must be told about it before
    deleting the symbol.
    ``from __future__ import annotations`` does not change this — the annotation
    is still parsed into the expression recorded here.
    """
    arguments = statement.args
    signature: list[ast.AST] = list(statement.decorator_list)
    signature.extend(arguments.defaults)
    signature.extend(default for default in arguments.kw_defaults if default is not None)
    declared = (
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        *(argument for argument in (arguments.vararg, arguments.kwarg) if argument is not None),
    )
    signature.extend(
        argument.annotation for argument in declared if argument.annotation is not None
    )
    if statement.returns is not None:
        signature.append(statement.returns)
    return tuple(signature)


def _imports(
    module: _Module,
    modules: dict[str, _Module],
    relationships: RelationshipAccumulator,
    nodes: list[Node],
    emitter: NodeEmitter,
    tally: _ImportTally,
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in module.tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                target = modules.get(alias.name)
                if target is None:
                    tally.note_unresolved(alias.name, modules)
                    emitter.unresolved(
                        module.module_id,
                        alias.name,
                        _location(module.path, statement),
                        nodes,
                        relationships,
                    )
                else:
                    tally.resolved += 1
                    relationships.add(
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
                    tally.note_unresolved(reference, modules)
                    emitter.unresolved(
                        module.module_id,
                        reference,
                        _location(module.path, statement),
                        nodes,
                        relationships,
                    )
                else:
                    tally.resolved += 1
                    relationships.add(
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
    resolved: str | None = None
    for statement in module.tree.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and statement.name == member
        ):
            kind = SymbolKind.CLASS if isinstance(statement, ast.ClassDef) else SymbolKind.FUNCTION
            resolved = symbol_node(
                f"{module.name}.{member}",
                kind,
                _location(module.path, statement),
                NAMESPACE,
                "python",
            ).id
    return resolved


def _calls(
    statements: list[ast.stmt],
    declarations: dict[str, str],
    aliases: dict[str, str],
    module_name: str,
    path: str,
    caller: str,
    relationships: RelationshipAccumulator,
    nodes: list[Node],
    emitter: NodeEmitter,
    class_declarations: Mapping[str, str] | None = None,
    prefix_nodes: tuple[ast.AST, ...] = (),
) -> None:
    visitor = _ScopeCallVisitor()
    for node in prefix_nodes:
        visitor.visit(node)
    for statement in statements:
        visitor.visit(statement)
    for candidate in visitor.calls:
        text = _expression_text(candidate.func)
        target = _resolve_call(text, declarations, aliases, module_name, class_declarations)
        location = _location(path, candidate.func)
        if target is None:
            emitter.unresolved(caller, text, location, nodes, relationships)
        else:
            relationships.add(caller, target, RelationshipKind.CALLS.value, location)
    unresolved_references: list[tuple[str, Location]] = []
    resolved_texts: set[str] = set()
    for reference in visitor.references:
        text = _expression_text(reference)
        target = _resolve_call(text, declarations, aliases, module_name, class_declarations)
        if target is not None:
            resolved_texts.add(text)
            relationships.add(
                caller,
                target,
                RelationshipKind.REFERENCES.value,
                _location(path, reference),
            )
        else:
            unresolved_references.append((text, _location(path, reference)))
    for text, location in unresolved_references:
        if any(resolved_text.startswith(text + ".") for resolved_text in resolved_texts):
            continue
        emitter.unresolved(caller, text, location, nodes, relationships)


def _resolve_call(
    text: str,
    declarations: dict[str, str],
    aliases: dict[str, str],
    module_name: str,
    class_declarations: Mapping[str, str] | None,
) -> str | None:
    if "." not in text:
        target_name = aliases.get(text, f"{module_name}.{text}")
        return declarations.get(target_name)
    head, _, tail = text.partition(".")
    if head == "self" and class_declarations is not None:
        # ``self`` is tied to the class statement that owns the caller, not to
        # whichever same-named class was assigned to the module name last.
        # Resolve through that statement's method table so repeated method
        # names remain last-wins locally without leaking across class objects.
        return class_declarations.get(tail)
    alias_target_name = aliases.get(head)
    if alias_target_name is not None:
        return declarations.get(f"{alias_target_name}.{tail}")
    return declarations.get(f"{module_name}.{text}")


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


def _module_location(path: str, line_index: LineIndex) -> Location:
    return Location(path, Range(Position(0, 0), line_index.end_position()))


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
