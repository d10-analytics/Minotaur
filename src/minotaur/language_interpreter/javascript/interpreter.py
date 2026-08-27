"""Bounded, source-only JavaScript-to-Minotaur interpretation.

The first slice intentionally emits only top-level declarations, direct class
methods, and evidence for bare identifier uses.  Object-literal methods are
excluded because they have no stable owning symbol without property-chain
resolution; ``this`` and class fields are excluded because their dispatch and
initialization are runtime-sensitive.  Nested declarations similarly retain
the enclosing emitted owner rather than inventing a second identity grain.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# BSD-2-Clause vendoring backup: esprima2 is pure Python (~3k lines), so it can
# be vendored if this dependency ever becomes unavailable or unmaintained.
import esprima  # type: ignore[import-untyped]

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
from minotaur.language_interpreter.source_text import LineIndex
from minotaur.language_interpreter.workspace import Workspace

NAMESPACE = "minotaur-javascript"
_PRODUCER = Producer(name="minotaur-javascript")


@dataclass(frozen=True, slots=True)
class _Module:
    path: str
    source: str
    tree: Any
    line_index: LineIndex
    file_id: str
    module_id: str
    digest: str
    bindings: dict[str, _Binding]
    exports: dict[str, _Binding]
    declaration_nodes: tuple[Node, ...]
    method_containments: tuple[tuple[str, str], ...]
    unsupported_import_locals: set[str]


@dataclass(frozen=True, slots=True)
class _Binding:
    name: str
    node_id: str
    position: int


def analyze_javascript_files(workspace: Workspace, files: tuple[Path, ...]) -> AnalysisResult:
    """Analyze selected JavaScript files, retaining valid sibling results."""
    diagnostics: list[Diagnostic] = []
    modules: list[_Module] = []
    for path in sorted(files, key=lambda p: p.relative_to(workspace.root).as_posix()):
        relative = path.relative_to(workspace.root).as_posix()
        try:
            content = path.read_bytes()
            source = content.decode("utf-8")
        except (OSError, UnicodeError) as error:
            diagnostics.append(Diagnostic(DiagnosticCode.SOURCE_READ_ERROR, relative, str(error)))
            continue
        line_index = LineIndex(source)
        try:
            tree = esprima.parseModule(
                source, options={"loc": True, "range": True, "tolerant": True}
            )
        except Exception as error:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.PARSE_ERROR,
                    relative,
                    str(error),
                    _error_location(relative, line_index, error),
                )
            )
            continue
        errors = getattr(tree, "errors", ()) or ()
        if errors:
            parse_error = errors[0]
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.PARSE_ERROR,
                    relative,
                    str(parse_error),
                    _error_location(relative, line_index, parse_error),
                )
            )
            continue
        modules.append(
            _make_module(relative, source, tree, hashlib.sha256(content).hexdigest(), line_index)
        )

    by_path = {module.path: module for module in modules}
    nodes: list[Node] = []
    for module in modules:
        nodes.extend((_file_node(module), _module_node(module), *module.declaration_nodes))
    relationships: dict[tuple[str, str, str], list[Location]] = defaultdict(list)
    seen_unresolved: set[str] = set()
    for module in modules:
        _append(
            relationships, module.file_id, module.module_id, RelationshipKind.CONTAINS.value, None
        )
        for declaration_node in module.declaration_nodes:
            if declaration_node.symbol_kind != SymbolKind.METHOD.value:
                _append(
                    relationships,
                    module.module_id,
                    declaration_node.id,
                    RelationshipKind.CONTAINS.value,
                    None,
                )
        for class_id, method_id in module.method_containments:
            _append(relationships, class_id, method_id, RelationshipKind.CONTAINS.value, None)
        _imports(module, by_path, relationships, nodes, seen_unresolved)
    for module in modules:
        _expressions(module, by_path, relationships, nodes, seen_unresolved)

    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=tuple(nodes),
        relationships=tuple(
            _relationship(key, locations) for key, locations in relationships.items()
        ),
        generated_by=_PRODUCER,
    )
    return AnalysisResult(document, tuple(diagnostics))


def _make_module(path: str, source: str, tree: Any, digest: str, line_index: LineIndex) -> _Module:
    file_identity = NodeIdentity(IdentityBasis.FILE_PATH, NAMESPACE)
    file_id = compute_node_id(file_identity, node_class=NodeClass.FILE.value, path=path)
    module_location = _full_location(path, line_index)
    module_identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    module_id = compute_node_id(
        module_identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=SymbolKind.MODULE.value,
        location=module_location,
    )
    bindings: dict[str, _Binding] = {}
    exports: dict[str, _Binding] = {}
    declaration_nodes: list[Node] = []
    method_containments: list[tuple[str, str]] = []
    for statement in getattr(tree, "body", ()):
        declaration = getattr(statement, "declaration", None)
        export_kind = None
        if getattr(statement, "type", None) == "ExportNamedDeclaration":
            if declaration is not None:
                export_kind = "named"
                _collect_declarations(
                    path,
                    module_id,
                    declaration,
                    bindings,
                    exports,
                    export_kind,
                    declaration_nodes,
                    method_containments,
                    line_index,
                )
            continue
        if getattr(statement, "type", None) == "ExportDefaultDeclaration":
            if (
                declaration is not None
                and getattr(declaration, "type", None)
                in {
                    "FunctionDeclaration",
                    "ClassDeclaration",
                }
                and getattr(declaration, "id", None) is not None
            ):
                _collect_declarations(
                    path,
                    module_id,
                    declaration,
                    bindings,
                    exports,
                    "default",
                    declaration_nodes,
                    method_containments,
                    line_index,
                )
            continue
        _collect_declarations(
            path,
            module_id,
            statement,
            bindings,
            exports,
            export_kind,
            declaration_nodes,
            method_containments,
            line_index,
        )
    return _Module(
        path,
        source,
        tree,
        line_index,
        file_id,
        module_id,
        digest,
        bindings,
        exports,
        tuple(declaration_nodes),
        tuple(method_containments),
        set(),
    )


def _collect_declarations(
    path: str,
    module_id: str,
    statement: Any,
    bindings: dict[str, _Binding],
    exports: dict[str, _Binding],
    export_kind: str | None,
    declaration_nodes: list[Node],
    method_containments: list[tuple[str, str]],
    line_index: LineIndex,
) -> None:
    typ = getattr(statement, "type", None)
    if (
        typ in {"FunctionDeclaration", "ClassDeclaration"}
        and getattr(statement, "id", None) is not None
    ):
        name = statement.id.name
        kind = SymbolKind.FUNCTION if typ == "FunctionDeclaration" else SymbolKind.CLASS
        node = _symbol_node(
            path, statement, f"{_without_js_suffix(path)}.{name}", kind, export_kind, line_index
        )
        binding = _Binding(name, node.id, _start(statement))
        bindings[name] = binding
        declaration_nodes.append(node)
        if export_kind == "named":
            exports[name] = binding
        # Methods are only direct children of an emitted class.
        if typ == "ClassDeclaration":
            class_label = node.label
            for member in getattr(getattr(statement, "body", None), "body", ()):
                if getattr(member, "type", None) != "MethodDefinition":
                    continue
                key = getattr(member, "key", None)
                method_name = _property_name(key)
                if method_name is None:
                    continue
                method = _symbol_node(
                    path,
                    member,
                    f"{class_label}.{method_name}",
                    SymbolKind.METHOD,
                    None,
                    line_index,
                )
                # Class methods are contained by the class, never by module.
                bindings.setdefault(
                    f"\x00method:{method.id}", _Binding(method_name, method.id, _start(member))
                )
                declaration_nodes.append(method)
                method_containments.append((node.id, method.id))
                member._minotaur_node = method
            statement._minotaur_node = node
        else:
            statement._minotaur_node = node
        return
    if typ == "VariableDeclaration":
        for declarator in getattr(statement, "declarations", ()):
            init = getattr(declarator, "init", None)
            if getattr(init, "type", None) not in {"FunctionExpression", "ArrowFunctionExpression"}:
                continue
            identifier = getattr(declarator, "id", None)
            if getattr(identifier, "type", None) != "Identifier":
                continue
            assert identifier is not None
            name = str(identifier.name)
            node = _symbol_node(
                path,
                declarator,
                f"{_without_js_suffix(path)}.{name}",
                SymbolKind.FUNCTION,
                export_kind,
                line_index,
            )
            binding = _Binding(name, node.id, _start(declarator))
            bindings[name] = binding
            if export_kind == "named":
                exports[name] = binding
            declaration_nodes.append(node)
            declarator._minotaur_node = node


def _file_node(module: _Module) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, NAMESPACE)
    return Node(
        id=module.file_id,
        identity=identity,
        node_class=NodeClass.FILE,
        label=module.path,
        path=module.path,
        language="javascript",
        extensions={NAMESPACE: {"content_sha256": module.digest}},
    )


def _module_node(module: _Module) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    return Node(
        id=module.module_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=_without_js_suffix(module.path),
        symbol_kind=SymbolKind.MODULE.value,
        language="javascript",
        location=_full_location(module.path, module.line_index),
    )


def _symbol_node(
    path: str,
    statement: Any,
    label: str,
    kind: SymbolKind,
    export_kind: str | None,
    line_index: LineIndex,
) -> Node:
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, NAMESPACE)
    location = _node_location(path, statement, line_index)
    extensions = {NAMESPACE: {"export_kind": export_kind}} if export_kind else None
    return Node(
        id=compute_node_id(
            identity, node_class=NodeClass.SYMBOL.value, symbol_kind=kind.value, location=location
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=kind.value,
        language="javascript",
        location=location,
        extensions=extensions,
    )


def _imports(
    module: _Module,
    modules: dict[str, _Module],
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
    seen: set[str],
) -> None:
    for statement in getattr(module.tree, "body", ()):
        typ = getattr(statement, "type", None)
        if typ == "ImportDeclaration":
            source = _literal_text(getattr(statement, "source", None))
            specifiers = getattr(statement, "specifiers", ())
            if not specifiers:
                _unsupported(
                    module, f"{source}#side-effect", statement.source, relationships, nodes, seen
                )
                continue
            target = _relative_target(module.path, source)
            target_module = modules.get(target) if target else None
            if target_module is not None:
                _append(
                    relationships,
                    module.module_id,
                    target_module.module_id,
                    RelationshipKind.IMPORTS.value,
                    _node_location(module.path, statement, module.line_index),
                )
            for specifier in specifiers:
                st = getattr(specifier, "type", None)
                imported = (
                    "default"
                    if st == "ImportDefaultSpecifier"
                    else "*"
                    if st == "ImportNamespaceSpecifier"
                    else _property_name(getattr(specifier, "imported", None))
                )
                local = _property_name(getattr(specifier, "local", None))
                if (
                    st == "ImportSpecifier"
                    and target_module is not None
                    and imported in target_module.exports
                ):
                    if local:
                        imported_binding = _Binding(
                            local, target_module.exports[imported].node_id, _start(specifier)
                        )
                        previous = module.bindings.get(local)
                        if previous is None or imported_binding.position >= previous.position:
                            module.bindings[local] = imported_binding
                else:
                    if local:
                        module.unsupported_import_locals.add(local)
                    _unsupported(
                        module,
                        f"{source}#{imported}",
                        getattr(specifier, "imported", None)
                        or getattr(specifier, "local", None)
                        or statement.source,
                        relationships,
                        nodes,
                        seen,
                    )
        elif typ == "ExportAllDeclaration":
            source = _literal_text(getattr(statement, "source", None))
            _unsupported(module, f"{source}#*", statement.source, relationships, nodes, seen)
        elif typ == "ExportNamedDeclaration" and getattr(statement, "source", None) is not None:
            source = _literal_text(statement.source)
            for specifier in getattr(statement, "specifiers", ()):
                name = _property_name(getattr(specifier, "exported", None)) or "*"
                _unsupported(
                    module,
                    f"{source}#{name}",
                    getattr(specifier, "exported", None) or statement.source,
                    relationships,
                    nodes,
                    seen,
                )
        elif typ == "ExportNamedDeclaration":
            # Local export lists are intentionally unsupported.  Keep the
            # unsupported operation visible at its exported identifier rather
            # than silently dropping it, just as source-backed export lists
            # are represented by ``<source>#<name>`` facts above.
            for specifier in getattr(statement, "specifiers", ()):
                exported = _property_name(getattr(specifier, "exported", None))
                local = _property_name(getattr(specifier, "local", None))
                name = exported or local
                anchor = getattr(specifier, "exported", None) or getattr(specifier, "local", None)
                if name is not None and anchor is not None:
                    _unsupported(module, f"export#{name}", anchor, relationships, nodes, seen)


def _expressions(
    module: _Module,
    modules: dict[str, _Module],
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
    seen: set[str],
) -> None:
    top_bindings = {
        name: binding
        for name, binding in module.bindings.items()
        if not name.startswith("\x00method:")
    }
    program_shadows = (
        _scope_shadows(module.tree) | module.unsupported_import_locals
    ) - module.bindings.keys()
    for statement in getattr(module.tree, "body", ()):
        owner = module.module_id
        declaration = (
            getattr(statement, "declaration", None)
            if getattr(statement, "type", None)
            in {"ExportNamedDeclaration", "ExportDefaultDeclaration"}
            else statement
        )
        node = getattr(declaration, "_minotaur_node", None)
        if node is not None:
            owner = node.id
        if getattr(declaration, "type", None) == "ClassDeclaration":
            # Class headers and non-method executable class body are class-owned.
            _walk(
                declaration,
                owner,
                module,
                top_bindings,
                relationships,
                nodes,
                seen,
                shadows=program_shadows,
                skip_declaration=True,
            )
            for member in getattr(getattr(declaration, "body", None), "body", ()):
                method_node = getattr(member, "_minotaur_node", None)
                if method_node is not None:
                    _walk(
                        getattr(member, "value", member),
                        method_node.id,
                        module,
                        top_bindings,
                        relationships,
                        nodes,
                        seen,
                        shadows=program_shadows,
                        skip_declaration=True,
                    )
        elif node is not None and getattr(declaration, "type", None) in {
            "FunctionDeclaration",
            "VariableDeclaration",
        }:
            body = getattr(declaration, "body", None)
            if body is None and getattr(declaration, "type", None) == "VariableDeclaration":
                for dec in getattr(declaration, "declarations", ()):
                    _walk(
                        getattr(dec, "init", None),
                        owner,
                        module,
                        top_bindings,
                        relationships,
                        nodes,
                        seen,
                        skip_declaration=True,
                    )
            else:
                _walk(
                    declaration,
                    owner,
                    module,
                    top_bindings,
                    relationships,
                    nodes,
                    seen,
                    shadows=program_shadows,
                    skip_declaration=True,
                )
        elif getattr(statement, "type", None) not in {
            "ImportDeclaration",
            "ExportAllDeclaration",
            "ExportNamedDeclaration",
            "ExportDefaultDeclaration",
        }:
            _walk(
                statement,
                owner,
                module,
                top_bindings,
                relationships,
                nodes,
                seen,
                shadows=program_shadows,
            )


def _walk(
    node: Any,
    owner: str,
    module: _Module,
    bindings: dict[str, _Binding],
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
    seen: set[str],
    shadows: set[str] | None = None,
    skip_declaration: bool = False,
) -> None:
    if node is None or not hasattr(node, "type"):
        return
    typ = node.type
    shadows = shadows or set()
    if typ == "Identifier":
        if node.name in shadows:
            return
        target = bindings.get(node.name)
        location = _node_location(module.path, node, module.line_index)
        if target is not None:
            _append(
                relationships, owner, target.node_id, RelationshipKind.REFERENCES.value, location
            )
        else:
            _unresolved(owner, node.name, location, relationships, nodes, seen)
        return
    if typ in {
        "FunctionDeclaration",
        "ClassDeclaration",
        "FunctionExpression",
        "ArrowFunctionExpression",
    }:
        # Declaration identifiers are not uses. Their executable bodies remain
        # attributed to the enclosing emitted owner for nested declarations.
        if typ == "ClassDeclaration":
            # A superclass expression executes while the class is evaluated,
            # so its uses belong to the emitted class symbol.
            _walk(
                getattr(node, "superClass", None),
                owner,
                module,
                bindings,
                relationships,
                nodes,
                seen,
                shadows,
            )
            body = getattr(getattr(node, "body", None), "body", ())
            for child in body:
                if getattr(child, "type", None) == "MethodDefinition":
                    if getattr(node, "_minotaur_node", None) is not None:
                        continue
                    value = getattr(child, "value", None)
                    _walk(
                        value,
                        owner,
                        module,
                        bindings,
                        relationships,
                        nodes,
                        seen,
                        set(shadows) | _scope_shadows(value),
                    )
                elif getattr(child, "type", None) == "StaticBlock":
                    # Static blocks execute, but class-field and static-field
                    # initializers are intentionally excluded from this slice.
                    _walk(child, owner, module, bindings, relationships, nodes, seen, shadows)
                else:
                    # PropertyDefinition/FieldDefinition nodes are excluded:
                    # field initialization and dispatch need runtime semantics.
                    continue
        else:
            # Parameter initializers execute under the function owner before
            # the body.  Only parameters preceding an initializer are in
            # scope there; the body receives the complete function scope.
            parameter_shadows = set(shadows)
            for parameter in getattr(node, "params", ()) or ():
                if getattr(parameter, "type", None) == "AssignmentPattern":
                    _walk(
                        getattr(parameter, "right", None),
                        owner,
                        module,
                        bindings,
                        relationships,
                        nodes,
                        seen,
                        parameter_shadows,
                    )
                parameter_shadows.update(_bound_names(parameter))
            nested_shadows = set(shadows) | _scope_shadows(node)
            if typ == "FunctionExpression" and getattr(node, "id", None) is not None:
                # A named function expression's name is a private recursive
                # binding visible in its body, not a module-level reference.
                nested_shadows.add(str(node.id.name))
            _walk(
                getattr(node, "body", None),
                owner,
                module,
                bindings,
                relationships,
                nodes,
                seen,
                nested_shadows,
            )
        return
    if typ == "VariableDeclaration":
        for declarator in getattr(node, "declarations", ()):
            _walk(
                getattr(declarator, "init", None),
                owner,
                module,
                bindings,
                relationships,
                nodes,
                seen,
                shadows,
            )
        return
    if typ == "BlockStatement":
        block_shadows = set(shadows) | _scope_shadows(node)
        for child in getattr(node, "body", ()):
            _walk(child, owner, module, bindings, relationships, nodes, seen, block_shadows)
        return
    if typ == "CatchClause":
        catch_shadows = set(shadows) | _bound_names(getattr(node, "param", None))
        _walk(
            getattr(node, "body", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            catch_shadows,
        )
        return
    if typ == "ForStatement":
        loop_shadows = set(shadows)
        init = getattr(node, "init", None)
        if getattr(init, "type", None) == "VariableDeclaration" and getattr(init, "kind", None) in {
            "let",
            "const",
        }:
            for declarator in getattr(init, "declarations", ()):
                loop_shadows.update(_bound_names(getattr(declarator, "id", None)))
        _walk(init, owner, module, bindings, relationships, nodes, seen, loop_shadows)
        _walk(
            getattr(node, "test", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            loop_shadows,
        )
        _walk(
            getattr(node, "update", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            loop_shadows,
        )
        _walk(
            getattr(node, "body", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            loop_shadows,
        )
        return
    if typ in {"ForInStatement", "ForOfStatement"}:
        loop_shadows = set(shadows)
        left = getattr(node, "left", None)
        if getattr(left, "type", None) == "VariableDeclaration" and getattr(left, "kind", None) in {
            "let",
            "const",
        }:
            for declarator in getattr(left, "declarations", ()):
                loop_shadows.update(_bound_names(getattr(declarator, "id", None)))
        _walk(left, owner, module, bindings, relationships, nodes, seen, shadows)
        _walk(
            getattr(node, "right", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            shadows,
        )
        _walk(
            getattr(node, "body", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            loop_shadows,
        )
        return
    if typ == "SwitchStatement":
        _walk(
            getattr(node, "discriminant", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            shadows,
        )
        switch_shadows = set(shadows)
        for case in getattr(node, "cases", ()):
            switch_shadows.update(_scope_shadows(case))
        for case in getattr(node, "cases", ()):
            _walk(case, owner, module, bindings, relationships, nodes, seen, switch_shadows)
        return
    if typ == "CallExpression":
        callee = getattr(node, "callee", None)
        if getattr(callee, "type", None) == "Import":
            literal = (
                _static_literal_text(getattr(node, "arguments", [None])[0])
                if getattr(node, "arguments", ())
                else None
            )
            text = f"{literal}#dynamic" if literal is not None else "import()"
            _unsupported(module, text, node, relationships, nodes, seen, owner=module.module_id)
            return
        elif getattr(callee, "type", None) == "Identifier":
            location = _node_location(module.path, callee, module.line_index)
            assert callee is not None
            callee_name = str(callee.name)
            if callee_name not in shadows:
                target = bindings.get(callee_name)
                if target is not None:
                    _append(
                        relationships, owner, target.node_id, RelationshipKind.CALLS.value, location
                    )
                else:
                    _unresolved(owner, callee_name, location, relationships, nodes, seen)
        else:
            _walk(callee, owner, module, bindings, relationships, nodes, seen, shadows)
        # Member dispatch and IIFE callee forms are deliberately outside the
        # direct bare-identifier call contract; their bases and bodies remain
        # ordinary executable expressions.
        for argument in getattr(node, "arguments", ()):
            _walk(argument, owner, module, bindings, relationships, nodes, seen, shadows)
        return
    if typ == "NewExpression":
        # Constructor/member dispatch is runtime-sensitive and is not a CALLS
        # fact in this slice.  Constructor arguments still contain uses.
        for argument in getattr(node, "arguments", ()):
            _walk(argument, owner, module, bindings, relationships, nodes, seen, shadows)
        return
    if typ == "MemberExpression":
        _walk(
            getattr(node, "object", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            shadows,
        )
        if getattr(node, "computed", False):
            _walk(
                getattr(node, "property", None),
                owner,
                module,
                bindings,
                relationships,
                nodes,
                seen,
                shadows,
            )
        return
    if typ in {"Property", "MethodDefinition"}:
        if typ == "Property" and getattr(node, "method", False):
            return
        if typ == "Property" and getattr(getattr(node, "value", None), "type", None) in {
            "FunctionExpression",
            "ArrowFunctionExpression",
        }:
            # A function-valued object property is deferred until runtime and
            # has no stable emitted owner.  An immediately evaluated property
            # expression (for example an IIFE) is a CallExpression instead and
            # remains walkable below.
            return
        _walk(
            getattr(node, "value", None),
            owner,
            module,
            bindings,
            relationships,
            nodes,
            seen,
            shadows,
        )
        if getattr(node, "computed", False):
            _walk(
                getattr(node, "key", None),
                owner,
                module,
                bindings,
                relationships,
                nodes,
                seen,
                shadows,
            )
        return
    for value in vars(node).values():
        if isinstance(value, list):
            for item in value:
                _walk(item, owner, module, bindings, relationships, nodes, seen, shadows)
        else:
            _walk(value, owner, module, bindings, relationships, nodes, seen, shadows)


def _scope_shadows(node: Any) -> set[str]:
    names: set[str] = set()
    for parameter in getattr(node, "params", ()) or ():
        names.update(_bound_names(parameter))

    def collect(
        current: Any, *, allow_root_block: bool = False, in_for_header: bool = False
    ) -> None:
        if not hasattr(current, "type"):
            return
        typ = current.type
        if typ in {"FunctionDeclaration", "ClassDeclaration"}:
            if current is not node and getattr(current, "id", None) is not None:
                names.add(str(current.id.name))
            return  # nested scope's locals do not shadow this scope
        if typ == "CatchClause":
            return  # catch parameters belong to the catch block's scope
        if typ == "BlockStatement" and not allow_root_block:
            return  # block lexical declarations belong to that block only
        if typ == "SwitchStatement":
            return  # switch lexical declarations belong to the switch only

        if typ == "VariableDeclaration":
            if not (in_for_header and getattr(current, "kind", None) != "var"):
                for declarator in getattr(current, "declarations", ()):
                    names.update(_bound_names(getattr(declarator, "id", None)))
                    collect(getattr(declarator, "init", None))
            return

        if typ in {"ForStatement", "ForInStatement", "ForOfStatement"}:
            for field_name, value in vars(current).items():
                if field_name == "type":
                    continue
                if isinstance(value, list):
                    for child in value:
                        collect(child, in_for_header=field_name in {"init", "left"})
                else:
                    collect(value, in_for_header=field_name in {"init", "left"})
            return

        for value in vars(current).values():
            if isinstance(value, list):
                for child in value:
                    collect(child)
            else:
                collect(value)

    body = getattr(node, "body", None)
    if body is None and getattr(node, "type", None) == "SwitchCase":
        body = getattr(node, "consequent", None)
    if isinstance(body, list):
        for statement in body:
            collect(statement)
    elif body is not None:
        collect(body, allow_root_block=True)

    def collect_function_vars(current: Any) -> None:
        if not hasattr(current, "type"):
            return
        typ = current.type
        if (
            typ
            in {
                "FunctionDeclaration",
                "FunctionExpression",
                "ArrowFunctionExpression",
                "ClassDeclaration",
            }
            and current is not node
        ):
            return
        if typ == "VariableDeclaration" and getattr(current, "kind", None) == "var":
            for declarator in getattr(current, "declarations", ()):
                names.update(_bound_names(getattr(declarator, "id", None)))
        for value in vars(current).values():
            if isinstance(value, list):
                for child in value:
                    collect_function_vars(child)
            else:
                collect_function_vars(value)

    if getattr(node, "type", None) in {
        "Program",
        "FunctionDeclaration",
        "FunctionExpression",
        "ArrowFunctionExpression",
    }:
        collect_function_vars(node)
    return names


def _bound_names(pattern: Any) -> set[str]:
    """Return identifier bindings introduced by a JS binding pattern."""
    if pattern is None or not hasattr(pattern, "type"):
        return set()
    typ = pattern.type
    if typ == "Identifier":
        return {str(pattern.name)}
    if typ in {"RestElement", "AssignmentPattern"}:
        return _bound_names(getattr(pattern, "argument", None) or getattr(pattern, "left", None))
    if typ == "ArrayPattern":
        names: set[str] = set()
        for element in getattr(pattern, "elements", ()) or ():
            names.update(_bound_names(element))
        return names
    if typ == "ObjectPattern":
        names = set()
        for property_node in getattr(pattern, "properties", ()) or ():
            names.update(_bound_names(getattr(property_node, "value", None)))
        return names
    return set()


def _unsupported(
    module: _Module,
    text: str,
    anchor: Any,
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
    seen: set[str],
    owner: str | None = None,
) -> None:
    _unresolved(
        owner or module.module_id,
        text,
        _node_location(module.path, anchor, module.line_index),
        relationships,
        nodes,
        seen,
    )


def _unresolved(
    origin: str,
    text: str,
    location: Location,
    relationships: dict[tuple[str, str, str], list[Location]],
    nodes: list[Node],
    seen: set[str],
) -> None:
    identity = NodeIdentity(IdentityBasis.UNRESOLVED_REFERENCE, NAMESPACE, originating_node=origin)
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE.value,
        location=location,
        reference_text=text,
    )
    if node_id not in seen:
        seen.add(node_id)
        nodes.append(
            Node(
                id=node_id,
                identity=identity,
                node_class=NodeClass.UNRESOLVED_REFERENCE,
                label=text,
                reference_text=text,
                language="javascript",
                location=location,
            )
        )
    _append(relationships, origin, node_id, RelationshipKind.REFERENCES.value, location)


def _relationship(key: tuple[str, str, str], locations: list[Location]) -> Relationship:
    return Relationship(
        source=key[0],
        target=key[1],
        kind=key[2],
        evidence=(
            Evidence(
                Provenance.STATIC_ANALYSIS,
                producer=_PRODUCER,
                locations=tuple(dict.fromkeys(locations)),
            ),
        ),
    )


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


def _node_location(path: str, node: Any, line_index: LineIndex) -> Location:
    start, end = getattr(node, "range", (0, 0))
    return Location(
        path,
        Range(line_index.position(start), line_index.position(end)),
    )


def _full_location(path: str, line_index: LineIndex) -> Location:
    return Location(path, Range(Position(0, 0), line_index.end_position()))


def _error_location(path: str, line_index: LineIndex, error: Any) -> Location | None:
    index = getattr(error, "index", None)
    if index is None:
        line = getattr(error, "lineNumber", None)
        column = getattr(error, "column", None)
        if line is None:
            return None
        if line > len(line_index.line_starts):
            return None
        index = line_index.line_starts[line - 1] + max((column or 1) - 1, 0)
    return Location(path, Range(line_index.position(index), line_index.position(index)))


def _literal_text(node: Any) -> str:
    value = getattr(node, "value", None)
    return str(value) if value is not None else ""


def _static_literal_text(node: Any) -> str | None:
    """Return only a literal string value; identifiers are dynamic."""
    value = getattr(node, "value", None)
    if getattr(node, "type", None) != "Literal" or not isinstance(value, str):
        return None
    return value


def _property_name(node: Any) -> str | None:
    if node is None:
        return None
    value = getattr(node, "name", None)
    if value is not None:
        return str(value)
    value = getattr(node, "value", None)
    return str(value) if isinstance(value, (str, int, float)) else None


def _start(node: Any) -> int:
    return getattr(node, "range", (0, 0))[0]


def _relative_target(path: str, specifier: str) -> str | None:
    if not specifier.startswith(("./", "../")) or not specifier.lower().endswith(".js"):
        return None
    parts = path.split("/")[:-1]
    for segment in specifier.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts)


def _without_js_suffix(path: str) -> str:
    """Strip a registered JavaScript suffix without changing path spelling."""
    return path[:-3] if path.lower().endswith(".js") else path
