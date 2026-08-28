"""Bounded static Python-to-Minotaur interpreter.

The v1 slice emits declarations and containment, local and workspace-module
imports, direct calls, references (including decorator and base-class
references), and unresolved references. No runtime claim is made and no source
code is executed or imported.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
)
from minotaur.language_interpreter.emission import NodeEmitter, symbol_node
from minotaur.language_interpreter.paths import resolve_relative
from minotaur.language_interpreter.python.discovery import discover_python_files
from minotaur.language_interpreter.reading import ParseFailure, read_and_parse
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
    location: Location
    file_id: str
    module_id: str


@dataclass(frozen=True, slots=True)
class _DeclaredSymbol:
    node_id: str
    container_id: str
    class_declarations: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class _ScopeContext:
    declarations: Mapping[str, str]
    aliases: Mapping[str, str]
    module_name: str
    path: str
    relationships: RelationshipAccumulator
    nodes: list[Node]
    emitter: NodeEmitter
    bound_names: frozenset[str] = frozenset()
    builtins: frozenset[str] = frozenset()


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
    _suffixes: dict[str, str] = field(init=False)

    def __init__(self, modules: Mapping[str, object]) -> None:
        self.resolved = 0
        self.unresolved = 0
        self.root_mismatched = 0
        self.prefixes = {}
        self._suffixes = _module_suffixes(modules)

    def note_unresolved(self, name: str) -> None:
        self.unresolved += 1
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
        self._scope_bound_names: list[frozenset[str]] = []
        self.call_bound_names: dict[ast.Call, frozenset[str]] = {}

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.call_bound_names[node] = frozenset().union(*self._scope_bound_names)
        # The callable expression is represented by the calls relationship;
        # suppress only its immediate head. Interior expressions (subscript
        # keys, conditionals, and f-string values) remain ordinary loads.
        self._visit_callee_head(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def _visit_callee_head(self, node: ast.expr) -> None:
        """Visit callee interiors while omitting only the callable head."""
        if isinstance(node, ast.Name):
            return
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, (ast.Attribute, ast.Name)):
                self._visit_callee_head(node.value)
            else:
                # A complex member base is an ordinary expression: retain its
                # base identifier and interior names (obj[key].method()).
                self.visit(node.value)
            return
        if isinstance(node, ast.Subscript):
            # A direct subscript callee suppresses only its table head while
            # retaining keys such as ``handler`` as ordinary references.
            self._visit_callee_head(node.value)
            self.visit(node.slice)
            return
        self.visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and not any(
            node.id in bound_names for bound_names in self._scope_bound_names
        ):
            self.references.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope_bound_names.append(frozenset(_type_param_names(node)))
        self._visit_definition_header(node)
        self._scope_bound_names.pop()
        self._scope_bound_names.append(_bound_names(node))
        for statement in node.body:
            self.visit(statement)
        self._scope_bound_names.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope_bound_names.append(frozenset(_type_param_names(node)))
        self._visit_definition_header(node)
        self._scope_bound_names.pop()
        self._scope_bound_names.append(_bound_names(node))
        for statement in node.body:
            self.visit(statement)
        self._scope_bound_names.pop()

    def _visit_definition_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in _signature_nodes(node):
            self.visit(expression)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        for kw_default in node.args.kw_defaults:
            if kw_default is not None:
                self.visit(kw_default)
        bound_names = frozenset(_argument_names(node.args))
        self._scope_bound_names.append(bound_names)
        self.visit(node.body)
        self._scope_bound_names.pop()

    def visit_TypeAlias(self, node: ast.AST) -> None:
        # PEP 695 type parameters are scoped to the alias expression only;
        # they must not suppress a same-named load later in the module.
        value = getattr(node, "value", None)
        if isinstance(value, ast.AST):
            self._scope_bound_names.append(frozenset(_type_param_names(node)))
            for expression in _type_param_expressions(node):
                self.visit(expression)
            self.visit(value)
            self._scope_bound_names.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # A nested class body executes while the enclosing scope is active, but
        # methods of that class execute in their own scope and remain outside
        # this visitor's ownership. The class header (decorators, bases, and
        # keywords such as ``metaclass=``) is evaluated in the enclosing scope
        # as well, so a base class is a real dependency of that scope.
        self._scope_bound_names.append(frozenset(_type_param_names(node)))
        for header in _class_header_nodes(node):
            self.visit(header)
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(statement)
        self._scope_bound_names.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        head = _expression_text(node).partition(".")[0]
        if isinstance(node.ctx, ast.Load) and not any(
            head in bound_names for bound_names in self._scope_bound_names
        ):
            self.references.append(node)
        # Record only the outermost attribute in a chain. Non-attribute,
        # non-name bases still need traversal so names in e.g. obj[key].attr
        # remain visible.
        if not isinstance(node.value, (ast.Attribute, ast.Name)):
            self.visit(node.value)


class _BindingCollector(ast.NodeVisitor):
    """Collect lexical binders without crossing nested execution scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)
        self._visit_definition_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)
        self._visit_definition_header(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)
        for expression in _class_header_nodes(node):
            self.visit(expression)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Lambda parameters and body belong to the lambda scope.
        return

    def _visit_definition_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Definition decorators, defaults, and annotations execute in the
        # enclosing scope; they can contain stores in unusual expressions.
        for expression in _signature_nodes(node):
            self.visit(expression)


def _type_param_names(node: ast.AST) -> set[str]:
    """Return names from optional PEP 695 ``type_params`` fields."""
    names: set[str] = set()
    for parameter in getattr(node, "type_params", ()):
        name = getattr(parameter, "name", None)
        if name is None and isinstance(parameter, ast.Name):
            name = parameter.id
        if isinstance(name, str):
            names.add(name)
    return names


def _type_param_expressions(node: ast.AST) -> tuple[ast.expr, ...]:
    """Return PEP 695 bounds and defaults for reference analysis."""
    expressions: list[ast.expr] = []
    for parameter in getattr(node, "type_params", ()):
        for field_name in ("bound", "default_value"):
            expression = getattr(parameter, field_name, None)
            if isinstance(expression, ast.expr):
                expressions.append(expression)
    return tuple(expressions)


def _argument_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _bound_names(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Collect names bound by one function's lexical scope."""
    collector = _BindingCollector()
    collector.names.update(_argument_names(statement.args))
    collector.names.update(_type_param_names(statement))
    for body_statement in statement.body:
        collector.visit(body_statement)
    collector.names.difference_update(collector.global_names)
    collector.names.update(collector.nonlocal_names)
    return frozenset(collector.names)


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
    modules: list[_Module] = []
    nodes: list[Node] = []

    sources, diagnostics = read_and_parse(workspace, files, _parse_python)
    for parsed in sources:
        module = _make_module(parsed.relative, parsed.tree, parsed.source, LineIndex(parsed.source))
        modules.append(module)
        nodes.extend(
            (
                _file_node(parsed.relative, hashlib.sha256(parsed.content).hexdigest()),
                _module_node(module),
            )
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
    tally = _ImportTally(module_by_name)
    emitter = NodeEmitter(NAMESPACE, "python")
    # Both sides of an equivalence run use this same Python interpreter, so
    # dir(builtins) is identical. A divergence therefore signals a genuine
    # environment difference worth surfacing, not a spurious graph change.
    builtin_names = frozenset(dir(builtins))
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
            builtin_names,
        )

    return AnalysisResult(
        GraphDocument(
            coordinate_encoding=CoordinateEncoding.UTF_8,
            nodes=tuple(nodes),
            relationships=relationships.documents(_PRODUCER),
            generated_by=_PRODUCER,
            # Flat keys: extension namespaces hold scalar-valued objects.
            extensions={
                NAMESPACE: {
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
        location,
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
        extensions={NAMESPACE: {"content_sha256": content_sha256}},
    )


def _module_node(module: _Module) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    return Node(
        id=module.module_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=module.name,
        symbol_kind=SymbolKind.MODULE.value,
        language="python",
        location=module.location,
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
    builtin_names: frozenset[str],
) -> None:
    aliases = _imports(module, modules, declarations, relationships, nodes, emitter, tally)
    context = _ScopeContext(
        declarations,
        aliases,
        module.name,
        module.path,
        relationships,
        nodes,
        emitter,
        frozenset(),
        builtin_names,
    )
    for symbol in symbols.values():
        relationships.add(
            symbol.container_id,
            symbol.node_id,
            RelationshipKind.CONTAINS.value,
            None,
        )
    _calls(
        context,
        [
            statement
            for statement in module.tree.body
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ],
        module.module_id,
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
            # Defaults, annotations, and decorators execute in the enclosing
            # scope; body loads use the function's lexical binders.
            _calls(
                replace(context, bound_names=frozenset(_type_param_names(statement))),
                [],
                symbols[statement].node_id,
                prefix_nodes=_signature_nodes(statement),
            )
            _calls(
                replace(context, bound_names=_bound_names(statement)),
                statement.body,
                symbols[statement].node_id,
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
                replace(context, bound_names=frozenset(_type_param_names(statement))),
                [
                    member
                    for member in statement.body
                    if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                ],
                symbols[statement].node_id,
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
                    method_context = replace(
                        context,
                        bound_names=_bound_names(member) | frozenset(_type_param_names(statement)),
                    )
                    # Method headers execute in the class's enclosing scope,
                    # while method bodies use their own lexical binders.
                    _calls(
                        replace(
                            context,
                            bound_names=frozenset(
                                _type_param_names(statement) | _type_param_names(member)
                            ),
                        ),
                        [],
                        symbols[member].node_id,
                        symbols[member].class_declarations,
                        prefix_nodes=_signature_nodes(member),
                    )
                    _calls(
                        method_context,
                        member.body,
                        symbols[member].node_id,
                        symbols[member].class_declarations,
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
        *_type_param_expressions(node),
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
    signature.extend(_type_param_expressions(statement))
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
    declarations: dict[str, str],
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
                    tally.note_unresolved(alias.name)
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
                resolved_target = declarations.get(reference)
                if resolved_target is None:
                    tally.note_unresolved(reference)
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


def _calls(
    context: _ScopeContext,
    statements: list[ast.stmt],
    caller: str,
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
        call_context = replace(
            context,
            bound_names=context.bound_names | visitor.call_bound_names[candidate],
        )
        target = _resolve_call(text, call_context, class_declarations)
        location = _location(context.path, candidate.func)
        head = text.partition(".")[0]
        if target is None and _suppress_builtin_call(candidate.func, call_context):
            continue
        if target is None and head in call_context.bound_names and head not in {"self", "cls"}:
            # A local or parameter call is dynamic. This also intentionally
            # wins over a module import alias shadowed by the local binding.
            continue
        if target is None:
            context.emitter.unresolved(caller, text, location, context.nodes, context.relationships)
        else:
            context.relationships.add(caller, target, RelationshipKind.CALLS.value, location)
    unresolved_references: list[tuple[str, Location]] = []
    for reference in visitor.references:
        text = _expression_text(reference)
        target = _resolve_call(text, context, class_declarations)
        head = text.partition(".")[0]
        if target is None and head in context.bound_names:
            continue
        if target is None and head in context.builtins:
            continue
        if target is not None:
            context.relationships.add(
                caller,
                target,
                RelationshipKind.REFERENCES.value,
                _location(context.path, reference),
            )
        else:
            unresolved_text = (
                _base_identifier(reference) if isinstance(reference, ast.Attribute) else text
            ) or text.partition(".")[0]
            unresolved_references.append((unresolved_text, _location(context.path, reference)))
    for text, location in unresolved_references:
        context.emitter.unresolved(caller, text, location, context.nodes, context.relationships)


def _suppress_builtin_call(expression: ast.expr, context: _ScopeContext) -> bool:
    """Suppress builtin calls while retaining meaningful member bases."""
    if isinstance(expression, ast.Name):
        return expression.id in context.builtins
    # ``str.foo(x)`` is a builtin member expression, but ``super().run()``
    # retains its outer unresolved fact because its base is a nested call.
    return (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and (expression.value.id in context.builtins)
    )


def _base_identifier(expression: ast.expr) -> str | None:
    """Return the root identifier of a member expression's value."""
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, (ast.Attribute, ast.Subscript)):
        return _base_identifier(expression.value)
    if isinstance(expression, ast.Call):
        return _base_identifier(expression.func)
    return None


def _resolve_call(
    text: str,
    context: _ScopeContext,
    class_declarations: Mapping[str, str] | None,
) -> str | None:
    head = text.partition(".")[0]
    if head in context.bound_names and head not in {"self", "cls"}:
        return None
    if "." not in text:
        target_name = context.aliases.get(text, f"{context.module_name}.{text}")
        return context.declarations.get(target_name)
    _, _, tail = text.partition(".")
    if head in {"self", "cls"} and class_declarations is not None:
        # ``self`` is tied to the class statement that owns the caller, not to
        # whichever same-named class was assigned to the module name last.
        # Resolve through that statement's method table so repeated method
        # names remain last-wins locally without leaking across class objects.
        return class_declarations.get(tail)
    alias_target_name = context.aliases.get(head)
    if alias_target_name is not None:
        return context.declarations.get(f"{alias_target_name}.{tail}")
    return context.declarations.get(f"{context.module_name}.{text}")


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
    if resolve_relative(tuple(parts), level) is None:
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


def _parse_python(source: str, relative: str) -> ast.Module:
    """Parse Python source and normalize syntax failures for the reader."""
    try:
        return ast.parse(source, filename=relative)
    except SyntaxError as error:
        raise ParseFailure(error.msg, _syntax_location(relative, error)) from error


def _expression_text(expression: ast.expr) -> str:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return f"{_expression_text(expression.value)}.{expression.attr}"
    return ast.unparse(expression)
