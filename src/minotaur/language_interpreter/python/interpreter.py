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
from collections.abc import Iterable, Mapping
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
    # Name -> dotted target for every import binding visible here. Module,
    # class-body and function-local imports all land in this one table so that
    # a local rebinding can be told apart from the module alias it shadows.
    import_targets: Mapping[str, str] = field(default_factory=dict)
    # When analyzing a direct class body, retain the class-only contribution
    # separately so a nested class method can discard every enclosing class
    # namespace while restoring the module/function imports beneath it.
    class_scope_bound_names: frozenset[str] = frozenset()
    # PEP 695 type parameters are lexical even though ordinary class locals
    # are not; nested classes and methods retain these names.
    class_scope_type_param_names: frozenset[str] = frozenset()
    class_scope_outer_import_targets: Mapping[str, str] | None = None
    is_package: bool = False
    receiver_name: str | None = None
    receiver_parameter: str | None = None


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

    def __init__(
        self,
        module_name: str = "",
        is_package: bool = False,
        receiver_name: str | None = None,
        receiver_parameter: str | None = None,
    ) -> None:
        self._module_name = module_name
        self._is_package = is_package
        self.calls: list[ast.Call] = []
        self.references: list[ast.Name | ast.Attribute] = []
        self._scope_bound_names: list[frozenset[str]] = []
        self._scope_global_names: list[frozenset[str]] = []
        self._scope_shadow_names: list[frozenset[str]] = []
        self._scope_import_targets: list[Mapping[str, str]] = []
        self._scope_receiver_overrides: list[tuple[str | None, str | None] | None] = []
        self._scope_excludes_enclosing_class: list[bool] = []
        self._scope_is_class: list[bool] = []
        self._scope_type_param_names: list[frozenset[str]] = []
        self._receiver_name = receiver_name
        self._receiver_parameter = receiver_parameter
        self.call_bound_names: dict[ast.Call, frozenset[str]] = {}
        self.call_global_names: dict[ast.Call, frozenset[str]] = {}
        self.call_shadow_names: dict[ast.Call, frozenset[str]] = {}
        self.call_import_targets: dict[ast.Call, Mapping[str, str]] = {}
        self.call_import_bound: dict[ast.Call, frozenset[str]] = {}
        self.call_receiver_names: dict[ast.Call, str | None] = {}
        self.call_receiver_parameters: dict[ast.Call, str | None] = {}
        self.call_excludes_enclosing_class: dict[ast.Call, bool] = {}
        self.reference_bound_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_global_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_shadow_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_import_targets: dict[ast.Name | ast.Attribute, Mapping[str, str]] = {}
        self.reference_import_bound: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_receiver_names: dict[ast.Name | ast.Attribute, str | None] = {}
        self.reference_receiver_parameters: dict[ast.Name | ast.Attribute, str | None] = {}
        self.reference_excludes_enclosing_class: dict[ast.Name | ast.Attribute, bool] = {}

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        bound_names, global_names, shadow_names, import_bound = self._scope_names()
        self.call_bound_names[node] = bound_names
        self.call_global_names[node] = global_names
        self.call_shadow_names[node] = shadow_names
        self.call_import_targets[node] = self._scope_imports()
        self.call_import_bound[node] = import_bound
        self.call_receiver_names[node], self.call_receiver_parameters[node] = (
            self._scope_receivers()
        )
        self.call_excludes_enclosing_class[node] = any(self._scope_excludes_enclosing_class)
        # The callable expression is represented by the calls relationship;
        # suppress only its immediate head. Interior expressions (subscript
        # keys, conditionals, and f-string values) remain ordinary loads.
        self._visit_chain_interiors(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def _visit_chain_interiors(self, node: ast.expr) -> None:
        """Visit a member chain's interiors while omitting its head identifiers.

        One expression contributes one fact, labelled with its full text, so
        neither a callee (``a.b().c.d()``) nor a load (``a.b().c.d``) records
        the names it is built from. Everything that is not part of that head --
        a nested call, a subscript key, a conditional -- remains an ordinary
        expression and must still be traversed, which is why both paths share
        this descent instead of stopping at the first non-attribute base.
        """
        if isinstance(node, ast.Name):
            return
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, (ast.Attribute, ast.Name)):
                self._visit_chain_interiors(node.value)
            else:
                # A complex member base is an ordinary expression: retain its
                # base identifier and interior names (obj[key].method()).
                self.visit(node.value)
            return
        if isinstance(node, ast.Subscript):
            # A direct subscript callee suppresses only its table head while
            # retaining keys such as ``handler`` as ordinary references.
            self._visit_chain_interiors(node.value)
            self.visit(node.slice)
            return
        self.visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        bound_names, global_names, shadow_names, import_bound = self._scope_names()
        if isinstance(node.ctx, ast.Load) and node.id not in bound_names:
            self.references.append(node)
            self.reference_bound_names[node] = bound_names
            self.reference_global_names[node] = global_names
            self.reference_shadow_names[node] = shadow_names
            self.reference_import_targets[node] = self._scope_imports()
            self.reference_import_bound[node] = import_bound
            self.reference_receiver_names[node], self.reference_receiver_parameters[node] = (
                self._scope_receivers()
            )
            self.reference_excludes_enclosing_class[node] = any(
                self._scope_excludes_enclosing_class
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        nested_class_method: bool = False,
    ) -> None:
        self._push_scope(
            frozenset(_type_param_names(node)),
            frozenset(),
            frozenset(),
            exclude_enclosing_class=nested_class_method,
        )
        self._visit_definition_header(node)
        self._pop_scope()
        bound_names, import_targets = _scope_binders(node, self._module_name, self._is_package)
        receiver_override = None
        shadow_names = bound_names
        if nested_class_method:
            receiver_parameter = _receiver_parameter_name(node)
            receiver_override = (
                _eligible_receiver_name(node, receiver_parameter),
                receiver_parameter,
            )
            # The receiver parameter is the source of the method's receiver
            # context, not a nested rebinding of it. Other binders in this
            # frame still shadow a receiver inherited from an outer scope.
            if receiver_parameter is not None:
                shadow_names -= frozenset((receiver_parameter,))

        # A method header is evaluated in the class namespace, but its body is
        # not. Temporarily remove every class frame so class-local assignments
        # and imports cannot leak into body resolution; enclosing function (or
        # module) frames remain visible for the method body. Restore the frames
        # after the walk so later class statements see the namespace built by
        # earlier statements in source order.
        class_scopes = []
        if nested_class_method:
            # A method body cannot close over any class namespace, including
            # classes that contain the class declaring the method. Function
            # frames remain in place because they are legitimate lexical
            # scopes. Keep the removed frames indexed so they can be restored
            # before the enclosing class continues in source order.
            class_scopes = self._remove_class_scopes()
        self._push_scope(
            bound_names,
            _global_names(node),
            shadow_names,
            import_targets,
            receiver_override=receiver_override,
            exclude_enclosing_class=nested_class_method,
        )
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()
        self._restore_class_scopes(class_scopes)

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
        self._push_scope(bound_names, frozenset(), bound_names)
        self.visit(node.body)
        self._pop_scope()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        result_expressions: tuple[ast.expr, ...],
    ) -> None:
        if not generators:
            for expression in result_expressions:
                self.visit(expression)
            return
        # The first iterable is evaluated in the enclosing scope. Subsequent
        # iterables, filters, and the result expression see comprehension
        # targets, which are local to the comprehension.
        self.visit(generators[0].iter)
        first_target_names = _target_names(generators[0].target)
        self._push_scope(first_target_names, frozenset(), first_target_names)
        for condition in generators[0].ifs:
            self.visit(condition)
        for generator in generators[1:]:
            self.visit(generator.iter)
            self._scope_bound_names[-1] |= _target_names(generator.target)
            self._scope_shadow_names[-1] |= _target_names(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in result_expressions:
            self.visit(expression)
        self._pop_scope()

    def _push_scope(
        self,
        bound_names: frozenset[str],
        global_names: frozenset[str],
        shadow_names: frozenset[str],
        import_targets: Mapping[str, str] | None = None,
        receiver_override: tuple[str | None, str | None] | None = None,
        exclude_enclosing_class: bool = False,
        class_scope: bool = False,
        type_param_names: frozenset[str] = frozenset(),
    ) -> None:
        self._scope_bound_names.append(bound_names)
        self._scope_global_names.append(global_names)
        self._scope_shadow_names.append(shadow_names)
        self._scope_import_targets.append(import_targets or {})
        self._scope_receiver_overrides.append(receiver_override)
        self._scope_excludes_enclosing_class.append(exclude_enclosing_class)
        self._scope_is_class.append(class_scope)
        self._scope_type_param_names.append(type_param_names)

    def _pop_scope(self) -> None:
        self._scope_bound_names.pop()
        self._scope_global_names.pop()
        self._scope_shadow_names.pop()
        self._scope_import_targets.pop()
        self._scope_receiver_overrides.pop()
        self._scope_excludes_enclosing_class.pop()
        self._scope_is_class.pop()
        self._scope_type_param_names.pop()

    def _remove_class_scopes(
        self,
    ) -> list[
        tuple[
            int,
            tuple[
                frozenset[str],
                frozenset[str],
                frozenset[str],
                Mapping[str, str],
                tuple[str | None, str | None] | None,
                bool,
                bool,
                frozenset[str],
            ],
        ]
    ]:
        """Temporarily hide every class namespace from a nested scope."""
        removed: list[
            tuple[
                int,
                tuple[
                    frozenset[str],
                    frozenset[str],
                    frozenset[str],
                    Mapping[str, str],
                    tuple[str | None, str | None] | None,
                    bool,
                    bool,
                    frozenset[str],
                ],
            ]
        ] = []
        for index in reversed(range(len(self._scope_is_class))):
            if not self._scope_is_class[index]:
                continue
            removed.append(
                (
                    index,
                    (
                        self._scope_bound_names[index],
                        self._scope_global_names[index],
                        self._scope_shadow_names[index],
                        self._scope_import_targets[index],
                        self._scope_receiver_overrides[index],
                        self._scope_excludes_enclosing_class[index],
                        self._scope_is_class[index],
                        self._scope_type_param_names[index],
                    ),
                )
            )
            type_param_names = self._scope_type_param_names[index]
            if type_param_names:
                # A class namespace is not lexical, but PEP 695 type
                # parameters are. Keep only those bindings visible while a
                # nested method body is analyzed.
                self._scope_bound_names[index] = type_param_names
                self._scope_global_names[index] = frozenset()
                self._scope_shadow_names[index] = type_param_names
                self._scope_import_targets[index] = {}
                self._scope_receiver_overrides[index] = None
                self._scope_excludes_enclosing_class[index] = False
                self._scope_is_class[index] = False
            else:
                del self._scope_bound_names[index]
                del self._scope_global_names[index]
                del self._scope_shadow_names[index]
                del self._scope_import_targets[index]
                del self._scope_receiver_overrides[index]
                del self._scope_excludes_enclosing_class[index]
                del self._scope_is_class[index]
                del self._scope_type_param_names[index]
        return removed

    def _restore_class_scopes(
        self,
        removed: list[
            tuple[
                int,
                tuple[
                    frozenset[str],
                    frozenset[str],
                    frozenset[str],
                    Mapping[str, str],
                    tuple[str | None, str | None] | None,
                    bool,
                    bool,
                    frozenset[str],
                ],
            ]
        ],
    ) -> None:
        for _, frame in sorted(removed, reverse=True):
            if not frame[-1]:
                continue
            marker_index = next(
                index
                for index in reversed(range(len(self._scope_type_param_names)))
                if self._scope_type_param_names[index] and not self._scope_is_class[index]
            )
            del self._scope_bound_names[marker_index]
            del self._scope_global_names[marker_index]
            del self._scope_shadow_names[marker_index]
            del self._scope_import_targets[marker_index]
            del self._scope_receiver_overrides[marker_index]
            del self._scope_excludes_enclosing_class[marker_index]
            del self._scope_is_class[marker_index]
            del self._scope_type_param_names[marker_index]

        for index, frame in sorted(removed):
            (
                restored_bound_names,
                restored_global_names,
                restored_shadow_names,
                restored_import_targets,
                restored_receiver_override,
                restored_excludes_enclosing_class,
                restored_is_class,
                restored_type_param_names,
            ) = frame
            self._scope_bound_names.insert(index, restored_bound_names)
            self._scope_global_names.insert(index, restored_global_names)
            self._scope_shadow_names.insert(index, restored_shadow_names)
            self._scope_import_targets.insert(index, restored_import_targets)
            self._scope_receiver_overrides.insert(index, restored_receiver_override)
            self._scope_excludes_enclosing_class.insert(index, restored_excludes_enclosing_class)
            self._scope_is_class.insert(index, restored_is_class)
            self._scope_type_param_names.insert(index, restored_type_param_names)

    def _scope_receivers(self) -> tuple[str | None, str | None]:
        for override in reversed(self._scope_receiver_overrides):
            if override is not None:
                return override
        return self._receiver_name, self._receiver_parameter

    def _scope_imports(self) -> Mapping[str, str]:
        """Return the import binding each visible name resolves to.

        The nearest frame wins, matching how a name is bound at run time: an
        inner ``from other import helper`` governs the inner scope even where
        an enclosing scope imported the same name from elsewhere.
        """
        merged: dict[str, str] = {}
        for frame in self._scope_import_targets:
            merged.update(frame)
        return merged

    def _scope_names(
        self,
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
        bound_names: set[str] = set()
        global_names: set[str] = set()
        import_bound_names: set[str] = set()
        seen_names: set[str] = set()
        for bound_frame, global_frame, import_frame in reversed(
            tuple(
                zip(
                    self._scope_bound_names,
                    self._scope_global_names,
                    self._scope_import_targets,
                    strict=True,
                )
            )
        ):
            for name in bound_frame:
                if name not in seen_names:
                    bound_names.add(name)
                    seen_names.add(name)
            # An import binds the name in this frame just as an assignment
            # does. It is not a dynamic local, so claiming the name here is
            # what stops an enclosing scope's assignment from silencing it,
            # while an assignment in this same frame still wins above.
            for name in import_frame:
                if name not in seen_names:
                    import_bound_names.add(name)
                    seen_names.add(name)
            for name in global_frame:
                if name not in seen_names:
                    global_names.add(name)
                    seen_names.add(name)
        shadow_names = frozenset().union(*self._scope_shadow_names)
        return (
            frozenset(bound_names),
            frozenset(global_names),
            shadow_names | global_names,
            frozenset(import_bound_names),
        )

    def visit_TypeAlias(self, node: ast.AST) -> None:
        # PEP 695 type parameters are scoped to the alias expression only;
        # they must not suppress a same-named load later in the module.
        value = getattr(node, "value", None)
        if isinstance(value, ast.AST):
            self._push_scope(frozenset(_type_param_names(node)), frozenset(), frozenset())
            for expression in _type_param_expressions(node):
                self.visit(expression)
            self.visit(value)
            self._pop_scope()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # A nested class statement has two distinct execution contexts. Its
        # decorators, bases and keywords execute in the immediately enclosing
        # body, including an enclosing class namespace. The new class body
        # then executes without any enclosing class namespace, because class
        # scopes are not lexical. Keep the type parameters in both phases;
        # unlike ordinary class locals, they remain visible lexically.
        type_param_names = frozenset(_type_param_names(node))
        self._push_scope(
            type_param_names,
            frozenset(),
            frozenset(),
            type_param_names=type_param_names,
        )
        for header in _class_header_nodes(node):
            self.visit(header)
        self._pop_scope()

        enclosing_class_scopes = self._remove_class_scopes()
        self._push_scope(
            type_param_names,
            frozenset(),
            frozenset(),
            exclude_enclosing_class=True,
            class_scope=True,
            type_param_names=type_param_names,
        )
        self._scope_global_names[-1] = _global_names_in_statements(
            statement
            for statement in node.body
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._visit_function(statement, nested_class_method=True)
            else:
                self.visit(statement)
            self._record_class_bindings(statement)
        self._pop_scope()
        self._restore_class_scopes(enclosing_class_scopes)

    def _record_class_bindings(self, statement: ast.stmt) -> None:
        """Add one class statement's bindings for later headers.

        Class locals are sequential: a preceding assignment suppresses an
        outer name in a later decorator/default, a preceding import supplies
        the nearest import binding, and deleting either reveals the enclosing
        name again. These bindings belong only to the class frame and are
        therefore hidden while a method body executes.
        """
        collector = _BindingCollector(self._module_name, self._is_package)
        collector.visit(statement)
        global_names = self._scope_global_names[-1]
        self._scope_bound_names[-1] = _class_dynamic_names_after_statement(
            self._scope_bound_names[-1],
            collector,
            global_names,
            self._scope_type_param_names[-1],
        )
        self._scope_shadow_names[-1] = _class_dynamic_names_after_statement(
            self._scope_shadow_names[-1],
            collector,
            global_names,
            self._scope_type_param_names[-1],
        )
        self._scope_import_targets[-1] = _class_import_targets_after_statement(
            self._scope_import_targets[-1], collector, global_names
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not isinstance(node.ctx, ast.Load):
            # ``store.registry = {}`` and ``del store.registry`` record no
            # member fact, but they still load the base object, which is an
            # ordinary reference to it.
            self.visit(node.value)
            return
        bound_names, global_names, shadow_names, import_bound = self._scope_names()
        # Record only the outermost attribute of a chain; whether it is
        # reportable at all is decided once, on its root identifier, where the
        # facts are emitted. Interiors are traversed without being recorded.
        self.references.append(node)
        self.reference_bound_names[node] = bound_names
        self.reference_global_names[node] = global_names
        self.reference_shadow_names[node] = shadow_names
        self.reference_import_targets[node] = self._scope_imports()
        self.reference_import_bound[node] = import_bound
        self.reference_receiver_names[node], self.reference_receiver_parameters[node] = (
            self._scope_receivers()
        )
        self.reference_excludes_enclosing_class[node] = any(self._scope_excludes_enclosing_class)
        self._visit_chain_interiors(node)


class _BindingCollector(ast.NodeVisitor):
    """Collect lexical binders without crossing nested execution scopes.

    ``module_name`` and ``is_package`` are only needed to give a relative
    ``from . import x`` the same dotted target the module-level alias table
    records for it; callers that do not read ``import_targets`` may omit them.
    """

    def __init__(self, module_name: str = "", is_package: bool = False) -> None:
        self.names: set[str] = set()
        self.deleted_names: set[str] = set()
        self.import_targets: dict[str, str] = {}
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()
        self._module_name = module_name
        self._is_package = is_package

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        # Import bindings are static, not dynamic locals: they are collected
        # apart from other binders so that a function-local import stays
        # reportable instead of silencing every use of the imported name. The
        # target is recorded exactly as ``_imports`` records an alias, so the
        # two can be compared.
        for alias in node.names:
            self.import_targets[alias.asname or alias.name.partition(".")[0]] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _relative_module(self._module_name, self._is_package, node.module, node.level)
        for alias in node.names:
            if alias.name != "*":
                self.import_targets[alias.asname or alias.name] = (
                    f"{base}.{alias.name}" if base else alias.name
                )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Del):
            self.deleted_names.add(node.id)
            self.names.add(node.id)
        elif isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.names.update(_pattern_capture_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        result_expressions: tuple[ast.expr, ...],
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        # Comprehension targets belong to the implicit comprehension scope.
        # Assignment expressions are visited above and therefore retain their
        # enclosing-function binding behavior.
        for expression in result_expressions:
            self.visit(expression)

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
        # Lambda parameters and body belong to the lambda scope, but its
        # defaults are evaluated in the enclosing one and can bind names there
        # through an assignment expression.
        for default in node.args.defaults:
            self.visit(default)
        for kw_default in node.args.kw_defaults:
            if kw_default is not None:
                self.visit(kw_default)

    def _visit_definition_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Definition decorators, defaults, and annotations execute in the
        # enclosing scope; they can contain stores in unusual expressions.
        for expression in _signature_nodes(node):
            self.visit(expression)


def _class_dynamic_names_after_statement(
    current_names: frozenset[str],
    collector: _BindingCollector,
    global_names: Iterable[str],
    protected_names: Iterable[str] = (),
) -> frozenset[str]:
    """Apply one class statement's binding category in source order.

    Dynamic assignments suppress static facts, while imports remain
    reportable. A later class import therefore replaces an earlier dynamic
    binding of the same name, and a deletion clears the class-local binding.
    If one compound statement contains both binding kinds, retain the dynamic
    possibility because its control flow is not statically known here.
    """
    global_name_set = frozenset(global_names)
    deleted_names = (
        frozenset(collector.deleted_names) - global_name_set - frozenset(protected_names)
    )
    imported_names = frozenset(collector.import_targets)
    dynamic_names = frozenset(collector.names) - global_name_set - deleted_names
    return (current_names - imported_names - deleted_names) | dynamic_names


def _class_import_targets_after_statement(
    current_targets: Mapping[str, str],
    collector: _BindingCollector,
    global_names: Iterable[str],
    outer_targets: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Apply one class statement's imports and deletions in source order."""
    deleted_names = frozenset(collector.deleted_names) - frozenset(global_names)
    import_targets = {
        name: target for name, target in current_targets.items() if name not in deleted_names
    }
    if outer_targets is not None:
        import_targets.update(
            (name, outer_targets[name]) for name in deleted_names if name in outer_targets
        )
    import_targets.update(
        (name, target)
        for name, target in collector.import_targets.items()
        if name not in deleted_names
    )
    return import_targets


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


def _scope_binders(
    statement: ast.FunctionDef | ast.AsyncFunctionDef,
    module_name: str,
    is_package: bool,
) -> tuple[frozenset[str], dict[str, str]]:
    """Return one function's dynamic binders and its import bindings.

    A name bound only by an ``import`` statement is a static binding whose
    target a later slice can resolve, so it is reported like a module-level
    alias rather than suppressed as a dynamic local. A name that is both
    imported and assigned needs no special case here: it is a binder, and the
    dynamic-local guard runs before anything consults these import bindings.
    """
    collector = _BindingCollector(module_name, is_package)
    collector.names.update(_argument_names(statement.args))
    collector.names.update(_type_param_names(statement))
    for body_statement in statement.body:
        collector.visit(body_statement)
    collector.names.difference_update(collector.global_names)
    collector.names.update(collector.nonlocal_names)
    return frozenset(collector.names), collector.import_targets


def _import_targets(
    statements: Iterable[ast.stmt], module_name: str, is_package: bool
) -> dict[str, str]:
    """Return the import bindings one module or class body makes directly.

    A class body executes in its own scope, so its imports are visible to the
    body and to method headers evaluated there, but never inside a method.
    """
    collector = _BindingCollector(module_name, is_package)
    for statement in statements:
        collector.visit(statement)
    return collector.import_targets


def _class_context_after_statement(
    context: _ScopeContext,
    statement: ast.stmt,
    module_name: str,
    is_package: bool,
) -> _ScopeContext:
    """Add one class-body statement's bindings for later method headers.

    Class locals are sequential. A preceding assignment suppresses an
    enclosing name in a later method header, a preceding import supplies the
    nearest import binding, and deleting either reveals the enclosing name
    again. These bindings stay in this class-only context and are therefore
    never visible to method bodies.
    """
    collector = _BindingCollector(module_name, is_package)
    collector.visit(statement)
    class_bound_names = _class_dynamic_names_after_statement(
        context.class_scope_bound_names,
        collector,
        collector.global_names,
        context.class_scope_type_param_names,
    )
    return replace(
        context,
        bound_names=(context.bound_names - context.class_scope_bound_names) | class_bound_names,
        import_targets=_class_import_targets_after_statement(
            context.import_targets,
            collector,
            collector.global_names,
            context.class_scope_outer_import_targets,
        ),
        class_scope_bound_names=class_bound_names,
    )


def _global_names(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Collect names declared global by one function or nested function."""
    return _global_names_in_statements(statement.body)


def _global_names_in_statements(statements: Iterable[ast.stmt]) -> frozenset[str]:
    collector = _BindingCollector()
    for statement in statements:
        collector.visit(statement)
    return frozenset(collector.global_names)


def _receiver_parameter_name(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Return a method's receiver-shaped first parameter while it stays bound.

    ``self`` and ``cls`` name a receiver even where they are not the *eligible*
    receiver for the method's kind: a metaclass ``__call__(cls, ...)`` and a
    ``@staticmethod`` taking ``self`` cannot resolve through a class, but the
    parameter is not a dynamic local either, so its member expressions are
    reported as unresolved rather than dropped. A parameter reassigned in the
    body is an ordinary local again and yields ``None``.
    """
    positional = (*statement.args.posonlyargs, *statement.args.args)
    if not positional or positional[0].arg not in _RECEIVER_NAMES:
        return None
    receiver = positional[0].arg
    collector = _BindingCollector()
    for body_statement in statement.body:
        collector.visit(body_statement)
    if (
        receiver in collector.names
        or receiver in collector.import_targets
        or receiver in collector.global_names
    ):
        return None
    return receiver


def _eligible_receiver_name(
    statement: ast.FunctionDef | ast.AsyncFunctionDef, receiver_parameter: str | None
) -> str | None:
    """Return the receiver that resolves through the owning class, if any."""
    if receiver_parameter is None or _is_staticmethod(statement):
        return None
    expected = "cls" if _is_classmethod(statement) else "self"
    return receiver_parameter if receiver_parameter == expected else None


def _is_classmethod(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a method receives its class instead of an instance.

    ``__new__``, ``__init_subclass__`` and ``__class_getitem__`` receive ``cls``
    implicitly, so an undecorated definition of one is a class receiver too.
    """
    if statement.name in _IMPLICIT_CLASSMETHODS:
        return True
    return any(
        isinstance(decorator, ast.Name)
        and decorator.id == "classmethod"
        or isinstance(decorator, ast.Attribute)
        and decorator.attr == "classmethod"
        for decorator in statement.decorator_list
    )


def _is_staticmethod(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Name)
        and decorator.id == "staticmethod"
        or isinstance(decorator, ast.Attribute)
        and decorator.attr == "staticmethod"
        for decorator in statement.decorator_list
    )


_RECEIVER_NAMES = frozenset({"self", "cls"})
_IMPLICIT_CLASSMETHODS = frozenset({"__new__", "__init_subclass__", "__class_getitem__"})


def _target_names(target: ast.expr) -> frozenset[str]:
    """Return names bound by a comprehension target."""
    names: set[str] = set()
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return frozenset(names)


def _pattern_capture_names(pattern: ast.pattern) -> frozenset[str]:
    """Return all names captured by a structural-pattern case."""
    names: set[str] = set()
    if isinstance(pattern, ast.MatchAs):
        if pattern.name is not None:
            names.add(pattern.name)
        if pattern.pattern is not None:
            names.update(_pattern_capture_names(pattern.pattern))
    elif isinstance(pattern, ast.MatchStar):
        if pattern.name is not None:
            names.add(pattern.name)
    elif isinstance(pattern, ast.MatchMapping):
        if pattern.rest is not None:
            names.add(pattern.rest)
        for nested in pattern.patterns:
            names.update(_pattern_capture_names(nested))
    elif isinstance(pattern, ast.MatchSequence):
        for nested in pattern.patterns:
            names.update(_pattern_capture_names(nested))
    elif isinstance(pattern, ast.MatchClass):
        for nested in (*pattern.patterns, *pattern.kwd_patterns):
            names.update(_pattern_capture_names(nested))
    elif isinstance(pattern, ast.MatchOr):
        for nested in pattern.patterns:
            names.update(_pattern_capture_names(nested))
    return frozenset(names)


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
    module_imports = _import_targets(module.tree.body, module.name, module.is_package)
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
        module_imports,
        is_package=module.is_package,
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
            binders, local_imports = _scope_binders(statement, module.name, module.is_package)
            _calls(
                replace(
                    context,
                    bound_names=binders,
                    import_targets={**module_imports, **local_imports},
                ),
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
            # A class statement's header runs in the enclosing scope, before
            # the class namespace exists. Its body then executes sequentially
            # in a new namespace: preceding assignments and imports are
            # visible to later method headers, but class scope is invisible to
            # method bodies.
            class_context = replace(
                context,
                bound_names=frozenset(_type_param_names(statement)),
                class_scope_bound_names=frozenset(_type_param_names(statement)),
                class_scope_type_param_names=frozenset(_type_param_names(statement)),
                class_scope_outer_import_targets=context.import_targets,
            )
            _calls(
                class_context,
                [],
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
                    binders, local_imports = _scope_binders(member, module.name, module.is_package)
                    receiver_parameter = _receiver_parameter_name(member)
                    method_context = replace(
                        context,
                        bound_names=binders | frozenset(_type_param_names(statement)),
                        import_targets={**module_imports, **local_imports},
                        receiver_name=_eligible_receiver_name(member, receiver_parameter),
                        receiver_parameter=receiver_parameter,
                    )
                    # Method headers execute in the class's enclosing scope,
                    # while method bodies use their own lexical binders.
                    _calls(
                        replace(
                            class_context,
                            bound_names=class_context.bound_names
                            | frozenset(_type_param_names(member)),
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
                else:
                    _calls(
                        class_context,
                        [member],
                        symbols[statement].node_id,
                        symbols[statement].class_declarations,
                    )
                class_context = _class_context_after_statement(
                    class_context, member, module.name, module.is_package
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
    # Syntactic import facts are collected at every nesting depth.  The
    # returned aliases remain module-scoped, however: nested bindings are
    # owned by their lexical scope collectors and must not leak into callers
    # outside that scope.
    module_statements = frozenset(module.tree.body)
    for statement in ast.walk(module.tree):
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
                    if statement in module_statements:
                        aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(statement, ast.ImportFrom):
            # Future imports are compiler directives, not workspace imports.
            if statement.module == "__future__":
                continue
            base = _relative_module(
                module.name, module.is_package, statement.module, statement.level
            )
            root_escape = statement.level > 0 and base is None
            target_module = modules.get(base) if not root_escape and base is not None else None
            if (
                statement not in module_statements
                and target_module is not None
                and not any(alias.name == "*" for alias in statement.names)
            ):
                # A nested named import has both a module dependency and a
                # declaration dependency. The module edge is emitted once
                # before the per-name edges below.
                relationships.add(
                    module.module_id,
                    target_module.module_id,
                    RelationshipKind.IMPORTS.value,
                    _location(module.path, statement),
                )
            for alias in statement.names:
                if alias.name == "*":
                    # Star imports depend on the module as a whole; they do
                    # not name a declaration or a synthetic ``.*`` member.
                    if base is not None:
                        reference = base
                    elif root_escape:
                        reference = "." * statement.level + (statement.module or "")
                    else:
                        reference = statement.module or ""
                    if target_module is None:
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
                            target_module.module_id,
                            RelationshipKind.IMPORTS.value,
                            _location(module.path, statement),
                        )
                    continue
                if base is not None:
                    reference = f"{base}.{alias.name}" if base else alias.name
                elif root_escape:
                    reference = (
                        "." * statement.level
                        + (f"{statement.module}." if statement.module else "")
                        + alias.name
                    )
                else:
                    reference = (
                        f"{statement.module}.{alias.name}" if statement.module else alias.name
                    )
                if root_escape:
                    tally.note_unresolved(reference)
                    emitter.unresolved(
                        module.module_id,
                        reference,
                        _location(module.path, statement),
                        nodes,
                        relationships,
                    )
                    continue
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
                    if statement in module_statements:
                        aliases[alias.asname or alias.name] = reference
            if statement in module_statements and target_module is not None and base is not None:
                aliases.setdefault(base.rsplit(".", 1)[-1], base)
    return aliases


def _calls(
    context: _ScopeContext,
    statements: list[ast.stmt],
    caller: str,
    class_declarations: Mapping[str, str] | None = None,
    prefix_nodes: tuple[ast.AST, ...] = (),
) -> None:
    visitor = _ScopeCallVisitor(
        context.module_name,
        context.is_package,
        context.receiver_name,
        context.receiver_parameter,
    )
    for node in prefix_nodes:
        visitor.visit(node)
    for statement in statements:
        visitor.visit(statement)
    for candidate in visitor.calls:
        expression_context = context
        if visitor.call_excludes_enclosing_class[candidate]:
            expression_context = _without_enclosing_class_scope(context)
        _emit_expression_facts(
            _scoped_context(
                replace(
                    expression_context,
                    receiver_name=visitor.call_receiver_names[candidate],
                    receiver_parameter=visitor.call_receiver_parameters[candidate],
                ),
                expression_context.bound_names | visitor.call_bound_names[candidate],
                visitor.call_global_names[candidate],
                visitor.call_shadow_names[candidate],
                {**expression_context.import_targets, **visitor.call_import_targets[candidate]},
                visitor.call_import_bound[candidate],
            ),
            caller,
            candidate.func,
            _location(context.path, candidate.func),
            None if visitor.call_excludes_enclosing_class[candidate] else class_declarations,
            RelationshipKind.CALLS.value,
        )
    for reference in visitor.references:
        expression_context = context
        if visitor.reference_excludes_enclosing_class[reference]:
            expression_context = _without_enclosing_class_scope(context)
        _emit_expression_facts(
            _scoped_context(
                replace(
                    expression_context,
                    receiver_name=visitor.reference_receiver_names[reference],
                    receiver_parameter=visitor.reference_receiver_parameters[reference],
                ),
                expression_context.bound_names | visitor.reference_bound_names[reference],
                visitor.reference_global_names[reference],
                visitor.reference_shadow_names[reference],
                {
                    **expression_context.import_targets,
                    **visitor.reference_import_targets[reference],
                },
                visitor.reference_import_bound[reference],
            ),
            caller,
            reference,
            _location(context.path, reference),
            (None if visitor.reference_excludes_enclosing_class[reference] else class_declarations),
            RelationshipKind.REFERENCES.value,
        )


def _without_enclosing_class_scope(context: _ScopeContext) -> _ScopeContext:
    """Hide class-only context while retaining enclosing lexical scopes."""
    outer_import_targets = context.class_scope_outer_import_targets
    if outer_import_targets is None:
        return context
    type_param_names = context.class_scope_type_param_names
    return replace(
        context,
        bound_names=(context.bound_names - context.class_scope_bound_names) | type_param_names,
        import_targets=outer_import_targets,
        class_scope_bound_names=type_param_names,
        class_scope_type_param_names=type_param_names,
        class_scope_outer_import_targets=None,
    )


def _scoped_context(
    context: _ScopeContext,
    bound_names: frozenset[str],
    global_names: frozenset[str],
    shadow_names: frozenset[str],
    import_targets: Mapping[str, str],
    import_bound_names: frozenset[str],
) -> _ScopeContext:
    """Narrow one scope's context to the bindings visible at one expression.

    ``import_bound_names`` are the names a nested scope binds by importing
    them. They are removed from the binders the enclosing scope contributes:
    the nearest binding governs, and an import is not a dynamic local.
    """
    return replace(
        context,
        bound_names=bound_names - global_names - import_bound_names,
        import_targets=import_targets,
        receiver_name=None if context.receiver_name in shadow_names else context.receiver_name,
        receiver_parameter=(
            None if context.receiver_parameter in shadow_names else context.receiver_parameter
        ),
    )


def _emit_expression_facts(
    context: _ScopeContext,
    caller: str,
    expression: ast.expr,
    location: Location,
    class_declarations: Mapping[str, str] | None,
    resolved_kind: str,
) -> None:
    """Emit the facts one callee or load expression contributes.

    Calls and non-call loads state the same thing about a name, so both go
    through this one function and cannot drift apart: ``self.on_click`` and
    ``self.on_click()`` are either both resolved or both unresolved.

    An expression that resolves is exactly one edge. Otherwise the decision is
    made on the expression's root identifier -- never on the text before its
    first dot, which for ``items[0].name`` is ``items[0]`` -- and a dynamic
    local or a builtin yields nothing. What remains is reported as one
    unresolved node labelled with the expression's full text, preceded by a
    resolved reference to the root when the root itself is known: ``Cfg.DEFAULT``
    is both a use of the imported ``Cfg`` and an unknown member of it. The root
    is guard input only and is never the label of an emitted node.

    That trailing reference is limited to a pure name-and-attribute chain. Once
    a chain passes through a call or a subscript (``build(make).c.d``), the
    descent has already recorded how the identifier was used -- as a call, or as
    an ordinary load inside the subscript -- and asserting a second, different
    use of it here would invent a fact the source never states.
    """
    text = _expression_text(expression)
    target = _resolve_call(text, context, class_declarations)
    if target is not None:
        context.relationships.add(caller, target, resolved_kind, location)
        return
    root = _base_identifier(expression)
    if root is not None:
        if _is_dynamic_local(root, expression, context):
            return
        if _suppress_builtin(expression, context):
            return
        chain_root = _attribute_root_name(expression)
        if chain_root is not None and chain_root != text:
            root_target = _resolve_call(chain_root, context, class_declarations)
            if root_target is not None:
                context.relationships.add(
                    caller, root_target, RelationshipKind.REFERENCES.value, location
                )
    context.emitter.unresolved(caller, text, location, context.nodes, context.relationships)


def _is_dynamic_local(root: str, expression: ast.expr, context: _ScopeContext) -> bool:
    """Whether an unresolved expression is rooted in a local binding.

    A call or load through a local name says nothing static about the
    workspace. The exception is a member expression rooted in this scope's
    receiver-shaped parameter, whether or not that parameter is the eligible
    receiver: ``self.on_click`` in a class without that method, a
    ``@staticmethod`` taking ``self``, and a metaclass ``__call__(cls, ...)``
    all name a member that cannot be resolved yet remains worth reporting.
    The bare parameter itself (``self``, ``cls()``) is an ordinary local.
    """
    if root not in context.bound_names:
        return False
    if isinstance(expression, ast.Name):
        return True
    return root not in (context.receiver_name, context.receiver_parameter)


def _suppress_builtin(expression: ast.expr, context: _ScopeContext) -> bool:
    """Suppress builtin uses while retaining meaningful member bases.

    ``str.foo(x)`` is a builtin member expression, but ``super().run()``
    retains its outer unresolved fact because its base is a nested call, and an
    imported name that happens to shadow a builtin (``from externallib import
    list``) is a workspace dependency rather than a builtin.

    Only real bindings count. ``aliases`` also holds resolution shorthands the
    module never bound -- ``from pkg.list import helper`` records ``list`` so
    that ``list.helper`` resolves -- and treating those as bindings would let
    the builtin escape suppression in every such module.
    """
    root = _attribute_root_name(expression)
    return root is not None and root in context.builtins and root not in context.import_targets


def _attribute_root_name(expression: ast.expr) -> str | None:
    """Return the root identifier of a pure name-and-attribute chain.

    Unlike ``_base_identifier`` this stops at a call or subscript, because
    ``super().run`` is headed by a call result rather than by ``super``.
    """
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return _attribute_root_name(expression.value)
    return None


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
    local_target = context.import_targets.get(head)
    if (
        local_target is not None
        and head in context.aliases
        and local_target != context.aliases[head]
    ):
        # A local import rebound this name to something else. Resolving through
        # the module alias would attribute the call to a module the scope
        # cannot see; local imports themselves are not resolved yet, so the
        # expression stays unresolved.
        return None
    if head in context.bound_names and head != context.receiver_name:
        return None
    if "." not in text:
        target_name = context.aliases.get(text, f"{context.module_name}.{text}")
        return context.declarations.get(target_name)
    _, _, tail = text.partition(".")
    if head == context.receiver_name and class_declarations is not None:
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
        if isinstance(expression.value, (ast.Name, ast.Attribute)):
            return f"{_expression_text(expression.value)}.{expression.attr}"
        return ast.unparse(expression)
    return ast.unparse(expression)
