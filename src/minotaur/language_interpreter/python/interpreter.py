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
class _BindingDescriptor:
    """Retain the selected target and category for one module binding."""

    target: str
    category: str


@dataclass(frozen=True, slots=True)
class _ModuleResolution:
    """Retain module-level binding categories and eligible dotted prefixes."""

    bindings: Mapping[str, _BindingDescriptor]
    prefixes: Mapping[str, str]
    plain_roots: frozenset[str]


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
    plain_import_names: frozenset[str] = frozenset()
    local_plain_import_names: frozenset[str] = frozenset()
    module_resolution: _ModuleResolution | None = None
    # When analyzing a direct class body, retain the class-only contribution
    # separately so a nested class method can discard every enclosing class
    # namespace while restoring the module/function imports beneath it.
    class_scope_bound_names: frozenset[str] = frozenset()
    # PEP 695 type parameters are lexical even though ordinary class locals
    # are not; nested classes and methods retain these names.
    class_scope_type_param_names: frozenset[str] = frozenset()
    class_scope_outer_import_targets: Mapping[str, str] | None = None
    class_scope_outer_plain_import_names: frozenset[str] | None = None
    class_scope_outer_uncertain_import_names: frozenset[str] | None = None
    class_scope_deleted_plain_names: frozenset[str] = frozenset()
    # A name whose import binding is flow-dependent remains reportable, but it
    # cannot resolve through an outer or module alias. This distinguishes an
    # uncertain import from an ordinary dynamic local, which is suppressed.
    uncertain_import_names: frozenset[str] = frozenset()
    # Function-flow imports are authoritative once executed. Class imports
    # retain their existing conservative resolution because class statements
    # execute in a separate, sequential namespace.
    authoritative_import_names: frozenset[str] = frozenset()
    is_package: bool = False
    receiver_name: str | None = None
    receiver_parameter: str | None = None
    modules: Mapping[str, _Module] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ImportFlowState:
    """Definite and uncertain import values at one source position.

    ``local_names`` is lexical: every import-bound name in a function belongs
    to that function even before its import executes. A name is either mapped
    to one definite target, uncertain, or currently an ordinary dynamic local.
    The last category is represented by absence from both ``targets`` and
    ``uncertain_names`` so existing dynamic-local suppression remains in force.
    """

    targets: Mapping[str, str] = field(default_factory=dict)
    uncertain_names: frozenset[str] = frozenset()
    local_names: frozenset[str] = frozenset()
    flow_sensitive: bool = False
    flow_frozen: bool = False
    plain_roots: frozenset[str] = frozenset()


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

    def note_unresolved(self, name: str, *, root_mismatch_eligible: bool = True) -> None:
        self.unresolved += 1
        if not root_mismatch_eligible:
            return
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
        module_import_targets: Mapping[str, str] | None = None,
        module_uncertain_import_names: frozenset[str] = frozenset(),
        module_plain_import_names: frozenset[str] = frozenset(),
        module_authoritative_import_names: frozenset[str] = frozenset(),
    ) -> None:
        self._module_name = module_name
        self._is_package = is_package
        self.calls: list[ast.Call] = []
        self.references: list[ast.Name | ast.Attribute] = []
        self._scope_bound_names: list[frozenset[str]] = []
        self._scope_global_names: list[frozenset[str]] = []
        self._scope_nonlocal_names: list[frozenset[str]] = []
        self._scope_shadow_names: list[frozenset[str]] = []
        self._scope_import_states: list[_ImportFlowState] = []
        self._scope_receiver_overrides: list[tuple[str | None, str | None] | None] = []
        self._scope_excludes_enclosing_class: list[bool] = []
        self._scope_is_class: list[bool] = []
        self._scope_type_param_names: list[frozenset[str]] = []
        self._scope_propagate_mutations: list[bool] = []
        self._scope_mutated_names: list[set[str]] = []
        self._flow_nested_depth = 0
        self._flow_conditional_depth = 0
        self._flow_mutate_targets = False
        self._receiver_name = receiver_name
        self._receiver_parameter = receiver_parameter
        self._module_import_targets = dict(module_import_targets or {})
        self._module_uncertain_import_names = module_uncertain_import_names
        self._module_plain_import_names = module_plain_import_names
        self._module_authoritative_import_names = module_authoritative_import_names
        self._defer_nested_bodies = 0
        self._deferred_callables: list[tuple[ast.AST, bool]] = []
        self._deferred_type_param_names: dict[ast.AST, frozenset[str]] = {}
        self.call_bound_names: dict[ast.Call, frozenset[str]] = {}
        self.call_global_names: dict[ast.Call, frozenset[str]] = {}
        self.call_shadow_names: dict[ast.Call, frozenset[str]] = {}
        self.call_import_targets: dict[ast.Call, Mapping[str, str]] = {}
        self.call_plain_import_names: dict[ast.Call, frozenset[str]] = {}
        self.call_import_bound: dict[ast.Call, frozenset[str]] = {}
        self.call_uncertain_import_names: dict[ast.Call, frozenset[str]] = {}
        self.call_authoritative_import_names: dict[ast.Call, frozenset[str]] = {}
        self.call_local_import_names: dict[ast.Call, frozenset[str]] = {}
        self.call_in_class_body: dict[ast.Call, bool] = {}
        self.call_receiver_names: dict[ast.Call, str | None] = {}
        self.call_receiver_parameters: dict[ast.Call, str | None] = {}
        self.call_excludes_enclosing_class: dict[ast.Call, bool] = {}
        self.class_header_expressions: set[ast.AST] = set()
        self.definition_header_expressions: set[ast.AST] = set()
        self.reference_bound_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_global_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_shadow_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_import_targets: dict[ast.Name | ast.Attribute, Mapping[str, str]] = {}
        self.reference_plain_import_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_import_bound: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_uncertain_import_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_authoritative_import_names: dict[
            ast.Name | ast.Attribute, frozenset[str]
        ] = {}
        self.reference_local_import_names: dict[ast.Name | ast.Attribute, frozenset[str]] = {}
        self.reference_in_class_body: dict[ast.Name | ast.Attribute, bool] = {}
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
        self.call_plain_import_names[node] = self._scope_plain_imports()
        self.call_import_bound[node] = import_bound
        self.call_uncertain_import_names[node] = self._scope_uncertain_imports()
        self.call_authoritative_import_names[node] = self._scope_authoritative_imports()
        self.call_local_import_names[node] = self._scope_local_import_names()
        self.call_in_class_body[node] = bool(self._scope_is_class and self._scope_is_class[-1])
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
            self.reference_plain_import_names[node] = self._scope_plain_imports()
            self.reference_import_bound[node] = import_bound
            self.reference_uncertain_import_names[node] = self._scope_uncertain_imports()
            self.reference_authoritative_import_names[node] = self._scope_authoritative_imports()
            self.reference_local_import_names[node] = self._scope_local_import_names()
            self.reference_in_class_body[node] = bool(
                self._scope_is_class and self._scope_is_class[-1]
            )
            self.reference_receiver_names[node], self.reference_receiver_parameters[node] = (
                self._scope_receivers()
            )
            self.reference_excludes_enclosing_class[node] = any(
                self._scope_excludes_enclosing_class
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._defer_nested_bodies:
            self._visit_function(node, defer_body=True)
        else:
            self._visit_function(node)
        self._record_dynamic_names(frozenset((node.name,)))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self._defer_nested_bodies:
            self._visit_function(node, defer_body=True)
        else:
            self._visit_function(node)
        self._record_dynamic_names(frozenset((node.name,)))

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        nested_class_method: bool = False,
        owner_state_override: _ImportFlowState | None = None,
        defer_body: bool = False,
        skip_header: bool = False,
        deferred_type_param_names: frozenset[str] = frozenset(),
    ) -> None:
        if self._defer_nested_bodies and owner_state_override is None:
            defer_body = True
        owner_state = self._flow_state() if owner_state_override is None else owner_state_override
        self._push_scope(
            frozenset(_type_param_names(node)),
            frozenset(),
            frozenset(),
            exclude_enclosing_class=nested_class_method,
            import_targets=owner_state.targets if owner_state is not None else None,
            import_uncertain_names=(
                owner_state.uncertain_names if owner_state is not None else frozenset()
            ),
            local_import_names=(
                owner_state.local_names if owner_state is not None else frozenset()
            ),
            import_flow_sensitive=owner_state is not None,
            propagate_mutations=owner_state is not None,
        )
        if not skip_header:
            self._visit_definition_header(node)
        header_mutations = self._pop_scope()
        if owner_state_override is None and owner_state is not None and header_mutations:
            self._record_dynamic_names(header_mutations)
        if defer_body:
            self._deferred_callables.append((node, nested_class_method))
            self._deferred_type_param_names[node] = frozenset().union(
                *(
                    type_param_names
                    for is_class, type_param_names in zip(
                        self._scope_is_class,
                        self._scope_type_param_names,
                        strict=True,
                    )
                    if is_class
                )
            )
            return
        bound_names, local_import_names, uncertain_import_names = _scope_binders(
            node, self._module_name, self._is_package
        )
        receiver_override = None
        shadow_names = bound_names | local_import_names
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
        if deferred_type_param_names:
            self._push_scope(
                deferred_type_param_names,
                frozenset(),
                deferred_type_param_names,
                type_param_names=deferred_type_param_names,
            )
        self._push_scope(
            bound_names,
            _global_names(node),
            shadow_names,
            import_targets=_without_import_roots(
                owner_state.targets if owner_state is not None else {},
                bound_names | _global_names(node),
            ),
            import_uncertain_names=(
                (owner_state.uncertain_names if owner_state is not None else frozenset())
                | uncertain_import_names
            ),
            local_import_names=local_import_names,
            import_flow_sensitive=True,
            receiver_override=receiver_override,
            exclude_enclosing_class=nested_class_method,
        )
        self._defer_nested_bodies += 1
        self._visit_block(node.body)
        final_state = self._flow_state()
        deferred_callables = self._deferred_callables
        self._deferred_callables = []
        self._defer_nested_bodies -= 1
        if final_state is not None:
            for deferred_node, deferred_class_method in deferred_callables:
                deferred_type_params = self._deferred_type_param_names.pop(
                    deferred_node, frozenset()
                )
                if isinstance(deferred_node, ast.Lambda):
                    self._visit_lambda_body(deferred_node, final_state)
                elif isinstance(deferred_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._visit_function(
                        deferred_node,
                        nested_class_method=deferred_class_method,
                        owner_state_override=final_state,
                        skip_header=True,
                        deferred_type_param_names=deferred_type_params,
                    )
        self._pop_scope()
        if deferred_type_param_names:
            self._pop_scope()
        self._restore_class_scopes(class_scopes)

    def visit_function_body(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        enclosing_import_targets: Mapping[str, str] | None = None,
        enclosing_uncertain_names: frozenset[str] = frozenset(),
        enclosing_local_import_names: frozenset[str] = frozenset(),
    ) -> None:
        """Visit a directly attributed function body with flow-sensitive imports."""
        bound_names, local_import_names, uncertain_import_names = _scope_binders(
            node, self._module_name, self._is_package
        )
        shadow_names = bound_names | local_import_names
        if self._receiver_parameter is not None:
            shadow_names -= frozenset((self._receiver_parameter,))
        owner_targets = _without_import_roots(
            enclosing_import_targets or {}, bound_names | _global_names(node)
        )
        self._push_scope(
            bound_names,
            _global_names(node),
            shadow_names,
            import_targets=owner_targets,
            import_uncertain_names=enclosing_uncertain_names | uncertain_import_names,
            local_import_names=local_import_names,
            import_flow_sensitive=enclosing_import_targets is not None,
        )
        self._defer_nested_bodies += 1
        self._visit_block(node.body)
        final_state = self._flow_state()
        deferred_callables = self._deferred_callables
        self._deferred_callables = []
        self._defer_nested_bodies -= 1
        if final_state is not None:
            for deferred_node, deferred_class_method in deferred_callables:
                deferred_type_params = self._deferred_type_param_names.pop(
                    deferred_node, frozenset()
                )
                if isinstance(deferred_node, ast.Lambda):
                    self._visit_lambda_body(deferred_node, final_state)
                elif isinstance(deferred_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._visit_function(
                        deferred_node,
                        nested_class_method=deferred_class_method,
                        owner_state_override=final_state,
                        skip_header=True,
                        deferred_type_param_names=deferred_type_params,
                    )
        self._pop_scope()

    def _visit_block(self, statements: Iterable[ast.stmt], *, nested: bool = False) -> None:
        entry_state = self._flow_state()
        if nested:
            self._flow_nested_depth += 1
        try:
            for statement in statements:
                self.visit(statement)
                state = self._flow_state()
                if state is not None and isinstance(statement, (ast.Return, ast.Raise)):
                    self._set_flow_state(replace(state, flow_frozen=True))
        finally:
            if nested:
                self._flow_nested_depth -= 1
                if entry_state is not None:
                    self._scope_import_states[-1] = entry_state

    def _flow_state(self) -> _ImportFlowState | None:
        if not self._scope_import_states:
            return None
        state = self._scope_import_states[-1]
        return state if state.flow_sensitive else None

    def _set_flow_state(self, state: _ImportFlowState) -> None:
        current = self._flow_state()
        if current is not None and (not current.flow_frozen or state.flow_frozen):
            self._scope_import_states[-1] = state

    def _record_dynamic_names(self, names: frozenset[str]) -> None:
        state = self._flow_state()
        if state is not None and state.flow_frozen:
            return
        self._propagate_class_directive_names(names)
        for index, propagates in enumerate(self._scope_propagate_mutations):
            if propagates:
                self._scope_mutated_names[index].update(names)
        if state is None:
            return
        global_names = names & self._scope_global_names[-1]
        if global_names:
            self._set_flow_state(
                replace(
                    state,
                    targets=_without_import_roots(state.targets, global_names),
                    uncertain_names=state.uncertain_names | global_names,
                    plain_roots=state.plain_roots - global_names,
                )
            )
            state = self._flow_state()
            if state is None:
                return
        affected = names & (
            state.local_names | state.uncertain_names | _import_binding_roots(state.targets)
        )
        if not affected:
            return
        self._set_flow_state(
            replace(
                state,
                targets=_without_import_roots(state.targets, affected),
                uncertain_names=state.uncertain_names | affected,
                plain_roots=state.plain_roots - affected,
            )
        )

    def _record_deleted_names(self, names: frozenset[str]) -> None:
        state = self._flow_state()
        if state is not None and state.flow_frozen:
            return
        self._propagate_class_directive_names(names)
        for index, propagates in enumerate(self._scope_propagate_mutations):
            if propagates:
                self._scope_mutated_names[index].update(names)
        if state is None:
            return
        global_names = names & self._scope_global_names[-1]
        if global_names:
            self._set_flow_state(
                replace(
                    state,
                    targets=_without_import_roots(state.targets, global_names),
                    uncertain_names=state.uncertain_names | global_names,
                    plain_roots=state.plain_roots - global_names,
                )
            )
            state = self._flow_state()
            if state is None:
                return
        affected = names & (
            state.local_names | state.uncertain_names | _import_binding_roots(state.targets)
        )
        if not affected:
            return
        self._set_flow_state(
            replace(
                state,
                targets=_without_import_roots(state.targets, affected),
                uncertain_names=state.uncertain_names | affected,
                plain_roots=state.plain_roots - affected,
            )
        )

    def _propagate_class_directive_names(self, names: frozenset[str]) -> None:
        """Apply immediate class global/nonlocal writes to the enclosing overlay."""
        if not self._scope_is_class or not self._scope_is_class[-1]:
            return
        names &= self._scope_global_names[-1] | self._scope_nonlocal_names[-1]
        if not names:
            return
        for index in range(len(self._scope_import_states) - 2, -1, -1):
            state = self._scope_import_states[index]
            if not state.flow_sensitive or state.flow_frozen:
                continue
            affected = names & (
                state.local_names | state.uncertain_names | _import_binding_roots(state.targets)
            )
            if not affected:
                return
            self._scope_import_states[index] = replace(
                state,
                targets=_without_import_roots(state.targets, affected),
                uncertain_names=state.uncertain_names | affected,
                plain_roots=state.plain_roots - affected,
            )
            return

    def _block_flow_imports(self, names: frozenset[str]) -> None:
        """Block routes introduced only on a conditional execution path."""
        state = self._flow_state()
        if state is None or state.flow_frozen or not names:
            return
        names &= state.local_names | state.uncertain_names | _import_binding_roots(state.targets)
        if not names:
            return
        self._set_flow_state(
            replace(
                state,
                targets=_without_import_roots(state.targets, names),
                uncertain_names=state.uncertain_names | names,
                plain_roots=state.plain_roots - names,
            )
        )

    def _blocked_flow_state(
        self, state: _ImportFlowState, names: frozenset[str]
    ) -> _ImportFlowState:
        """Return ``state`` with compound-uncertain roots blocked."""
        self._scope_import_states[-1] = state
        self._block_flow_imports(names)
        return self._flow_state() or state

    @staticmethod
    def _flow_state_copy(state: _ImportFlowState) -> _ImportFlowState:
        """Copy mutable flow containers before visiting one conditional arm."""
        return replace(
            state,
            targets=dict(state.targets),
            uncertain_names=frozenset(state.uncertain_names),
            local_names=frozenset(state.local_names),
            plain_roots=frozenset(state.plain_roots),
        )

    def visit_Import(self, node: ast.Import) -> None:
        state = self._flow_state()
        if state is None or state.flow_frozen:
            return
        targets = state.targets if self._flow_mutate_targets else dict(state.targets)
        if not isinstance(targets, dict):
            targets = dict(targets)
        bound_names: set[str] = set()
        uncertain_names = set(state.uncertain_names)
        plain_roots = set(state.plain_roots)
        for alias in node.names:
            if self._flow_nested_depth or (
                self._flow_conditional_depth and alias.asname is None and "." in alias.name
            ):
                root = alias.name.partition(".")[0]
                targets = _without_import_roots(targets, (root,))
                uncertain_names.add(root)
                plain_roots.discard(root)
                bound_names.add(root)
                continue
            root = alias.asname or alias.name.partition(".")[0]
            plain_roots.discard(root)
            if alias.asname is None and "." in alias.name:
                plain_roots.add(root)
            _update_import_bindings(targets, alias)
            bound_names.add(root)
        self._set_flow_state(
            replace(
                state,
                targets=targets,
                uncertain_names=frozenset(uncertain_names - bound_names),
                plain_roots=frozenset(plain_roots),
            )
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        state = self._flow_state()
        if state is None or state.flow_frozen:
            return
        targets = state.targets if self._flow_mutate_targets else dict(state.targets)
        if not isinstance(targets, dict):
            targets = dict(targets)
        bound_names: set[str] = set()
        uncertain_names = set(state.uncertain_names)
        plain_roots = set(state.plain_roots)
        base = _relative_module(self._module_name, self._is_package, node.module, node.level)
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            plain_roots.discard(name)
            if self._flow_nested_depth or (node.level > 0 and base is None):
                targets = _without_import_roots(targets, (name,))
                bound_names.add(name)
                uncertain_names.add(name)
            else:
                _update_named_import_binding(
                    targets,
                    name,
                    f"{base}.{alias.name}" if base else alias.name,
                )
                uncertain_names.discard(name)
            bound_names.add(name)
        self._set_flow_state(
            replace(
                state,
                targets=targets,
                uncertain_names=frozenset(uncertain_names),
                plain_roots=frozenset(plain_roots),
            )
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        names: set[str] = set()
        for target in node.targets:
            self.visit(target)
            names.update(_target_names(target))
        self._record_dynamic_names(frozenset(names))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            self.visit(node.target)
            self._record_dynamic_names(_target_names(node.target))
        elif isinstance(node.target, ast.Attribute):
            self.visit(node.target.value)
        elif isinstance(node.target, ast.Subscript):
            self.visit(node.target.value)
            self.visit(node.target.slice)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            target_context = node.target.ctx
            node.target.ctx = ast.Load()
            self.visit(node.target)
            node.target.ctx = target_context
        elif isinstance(node.target, ast.Attribute):
            self.visit(node.target.value)
        elif isinstance(node.target, ast.Subscript):
            self.visit(node.target.value)
            self.visit(node.target.slice)
        else:
            self.visit(node.target)
        self.visit(node.value)
        self._record_dynamic_names(_target_names(node.target))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self.visit(node.target)
        self._record_dynamic_names(_target_names(node.target))

    def visit_Delete(self, node: ast.Delete) -> None:
        names: set[str] = set()
        for target in node.targets:
            self.visit(target)
            names.update(
                child.id
                for child in ast.walk(target)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Del)
            )
        self._record_deleted_names(frozenset(names))

    def visit_If(self, node: ast.If) -> None:
        state = self._flow_state()
        if state is None:
            self.generic_visit(node)
            return
        self.visit(node.test)
        entry_state = self._flow_state() or state

        self._scope_import_states[-1] = self._flow_state_copy(entry_state)
        self._flow_conditional_depth += 1
        try:
            self._visit_block(node.body)
            body_state = self._flow_state() or entry_state
        finally:
            self._flow_conditional_depth -= 1

        self._scope_import_states[-1] = self._flow_state_copy(entry_state)
        if node.orelse:
            self._flow_conditional_depth += 1
            try:
                self._visit_block(node.orelse)
                else_state = self._flow_state() or entry_state
            finally:
                self._flow_conditional_depth -= 1
        else:
            else_state = self._flow_state_copy(entry_state)

        self._scope_import_states[-1] = _join_flow_states((body_state, else_state))

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        state = self._flow_state()
        if state is None:
            self.generic_visit(node)
            return
        self.visit(node.iter)
        self.visit(node.target)
        self._record_dynamic_names(_target_names(node.target))
        body_state = self._flow_state() or state
        body_touched = _flow_touched_names(node.body)
        self._scope_import_states[-1] = body_state
        self._visit_block(node.body, nested=True)
        body_exit_state = self._blocked_flow_state(body_state, body_touched)
        self._scope_import_states[-1] = body_exit_state
        self._visit_block(node.orelse, nested=True)
        self._blocked_flow_state(body_exit_state, _flow_touched_names(node.orelse))

    def visit_While(self, node: ast.While) -> None:
        state = self._flow_state()
        if state is None:
            self.generic_visit(node)
            return
        self.visit(node.test)
        body_state = self._flow_state() or state
        body_touched = _flow_touched_names(node.body)
        self._scope_import_states[-1] = body_state
        self._visit_block(node.body, nested=True)
        body_exit_state = self._blocked_flow_state(body_state, body_touched)
        self._scope_import_states[-1] = body_exit_state
        self._visit_block(node.orelse, nested=True)
        self._blocked_flow_state(body_exit_state, _flow_touched_names(node.orelse))

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                self._record_dynamic_names(_target_names(item.optional_vars))
        body_state = self._flow_state()
        if body_state is None:
            self._visit_block(node.body, nested=True)
            return
        self._scope_import_states[-1] = body_state
        self._visit_block(node.body, nested=True)
        self._blocked_flow_state(body_state, _flow_touched_names(node.body))

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        state = self._flow_state()
        if state is None:
            self.generic_visit(node)
            return
        handler_names = frozenset(
            handler.name for handler in node.handlers if handler.name is not None
        )
        body_state = self._flow_state() or state
        body_touched = _flow_touched_names(node.body)
        self._scope_import_states[-1] = body_state
        self._visit_block(node.body, nested=True)
        body_exit_state = self._blocked_flow_state(body_state, body_touched)
        # A handler can observe a write made before an exception in the try
        # body. Keep that uncertainty, while isolating each handler from the
        # writes made by its siblings.
        self._scope_import_states[-1] = body_exit_state
        self._visit_block(node.orelse, nested=True)
        orelse_exit_state = self._blocked_flow_state(
            body_exit_state, _flow_touched_names(node.orelse)
        )
        handler_exit_names: set[str] = set(handler_names)
        for handler in node.handlers:
            # The exception type is evaluated before the ``as`` target is
            # assigned, so an imported name may still be referenced there.
            self._scope_import_states[-1] = body_exit_state
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self._record_dynamic_names(frozenset((handler.name,)))
            handler_state = self._flow_state() or body_exit_state
            self._scope_import_states[-1] = handler_state
            self._visit_block(handler.body, nested=True)
            if handler.name is not None:
                self._record_deleted_names(frozenset((handler.name,)))
            handler_exit_names.update(_flow_touched_names(handler.body))
            if handler.type is not None:
                handler_exit_names.update(_flow_touched_node(handler.type))
        final_state = self._blocked_flow_state(orelse_exit_state, frozenset(handler_exit_names))
        self._scope_import_states[-1] = final_state
        self._visit_block(node.finalbody, nested=True)
        self._blocked_flow_state(final_state, _flow_touched_names(node.finalbody))

    def visit_Match(self, node: ast.Match) -> None:
        state = self._flow_state()
        if state is None:
            self.generic_visit(node)
            return
        self.visit(node.subject)
        case_state = self._flow_state() or state
        touched: set[str] = set()
        for case in node.cases:
            self._scope_import_states[-1] = case_state
            self.visit(case.pattern)
            captures = _pattern_capture_names(case.pattern)
            self._record_dynamic_names(captures)
            guard_state = self._flow_state() or case_state
            if case.guard is not None:
                self._scope_import_states[-1] = guard_state
                self.visit(case.guard)
                guard_state = self._flow_state() or guard_state
            # A failed guard can leave its walrus and pattern bindings in
            # place, so later cases inherit the post-guard state. Body writes
            # remain isolated to the selected alternative.
            self._scope_import_states[-1] = guard_state
            self._visit_block(case.body, nested=True)
            case_state = guard_state
            touched.update(captures)
            touched.update(_flow_touched_names(case.body))
            if case.guard is not None:
                touched.update(_flow_touched_node(case.guard))
        self._blocked_flow_state(case_state, frozenset(touched))

    def _visit_definition_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in _signature_nodes(node):
            self.definition_header_expressions.add(expression)
            self.visit(expression)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        for kw_default in node.args.kw_defaults:
            if kw_default is not None:
                self.visit(kw_default)
        if self._defer_nested_bodies:
            self._deferred_callables.append((node, False))
            return
        self._visit_lambda_body(node, self._flow_state())

    def _visit_lambda_body(self, node: ast.Lambda, owner_state: _ImportFlowState | None) -> None:
        bound_names = frozenset(_argument_names(node.args))
        self._push_scope(
            bound_names,
            frozenset(),
            bound_names,
            import_targets=_without_import_roots(
                owner_state.targets if owner_state is not None else {}, bound_names
            ),
            import_uncertain_names=(
                owner_state.uncertain_names if owner_state is not None else frozenset()
            ),
            import_flow_sensitive=owner_state is not None,
        )
        self.visit(node.body)
        self._pop_scope()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,), lazy=True)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        result_expressions: tuple[ast.expr, ...],
        *,
        lazy: bool = False,
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
        owner_state = self._flow_state()
        helper_targets = (
            _without_import_roots(owner_state.targets, first_target_names)
            if owner_state is not None
            else {}
        )
        helper_uncertain = (
            frozenset(
                name
                for name in owner_state.uncertain_names
                if name.partition(".")[0] not in first_target_names
            )
            if owner_state is not None
            else frozenset()
        )
        helper_local_names = (
            owner_state.local_names - first_target_names if owner_state is not None else frozenset()
        )
        eager = not lazy
        # Generator bodies run lazily; list/set/dict comprehension bodies run
        # while the enclosing expression is evaluated. The first iterable is
        # visited above in either case because Python evaluates it eagerly.
        self._push_scope(
            first_target_names,
            frozenset(),
            first_target_names,
            import_targets=helper_targets,
            import_uncertain_names=helper_uncertain,
            local_import_names=helper_local_names,
            import_flow_sensitive=owner_state is not None,
            propagate_mutations=owner_state is not None and eager,
        )
        if lazy:
            state = self._flow_state()
            if state is not None:
                self._set_flow_state(replace(state, flow_frozen=True))
        for condition in generators[0].ifs:
            self.visit(condition)
        for generator in generators[1:]:
            self.visit(generator.iter)
            target_names = _target_names(generator.target)
            self._scope_bound_names[-1] |= target_names
            self._scope_shadow_names[-1] |= target_names
            helper_state = self._flow_state()
            if helper_state is not None:
                self._scope_import_states[-1] = replace(
                    helper_state,
                    targets=_without_import_roots(helper_state.targets, target_names),
                    uncertain_names=frozenset(
                        name
                        for name in helper_state.uncertain_names
                        if name.partition(".")[0] not in target_names
                    ),
                    local_names=helper_state.local_names - target_names,
                )
            for condition in generator.ifs:
                self.visit(condition)
        for expression in result_expressions:
            self.visit(expression)
        mutations = self._pop_scope()
        if owner_state is not None and eager and mutations:
            self._record_dynamic_names(mutations)

    def _push_scope(
        self,
        bound_names: frozenset[str],
        global_names: frozenset[str],
        shadow_names: frozenset[str],
        import_targets: Mapping[str, str] | None = None,
        import_uncertain_names: frozenset[str] = frozenset(),
        local_import_names: frozenset[str] = frozenset(),
        import_flow_sensitive: bool = False,
        receiver_override: tuple[str | None, str | None] | None = None,
        exclude_enclosing_class: bool = False,
        class_scope: bool = False,
        type_param_names: frozenset[str] = frozenset(),
        propagate_mutations: bool = False,
    ) -> None:
        self._scope_bound_names.append(bound_names)
        self._scope_global_names.append(global_names)
        self._scope_nonlocal_names.append(frozenset())
        self._scope_shadow_names.append(shadow_names)
        self._scope_import_states.append(
            _ImportFlowState(
                import_targets or {},
                import_uncertain_names,
                local_import_names,
                import_flow_sensitive,
            )
        )
        self._scope_receiver_overrides.append(receiver_override)
        self._scope_excludes_enclosing_class.append(exclude_enclosing_class)
        self._scope_is_class.append(class_scope)
        self._scope_type_param_names.append(type_param_names)
        self._scope_propagate_mutations.append(propagate_mutations)
        self._scope_mutated_names.append(set())

    def _pop_scope(self) -> frozenset[str]:
        mutations = frozenset(self._scope_mutated_names.pop())
        self._scope_propagate_mutations.pop()
        self._scope_bound_names.pop()
        self._scope_global_names.pop()
        self._scope_nonlocal_names.pop()
        self._scope_shadow_names.pop()
        self._scope_import_states.pop()
        self._scope_receiver_overrides.pop()
        self._scope_excludes_enclosing_class.pop()
        self._scope_is_class.pop()
        self._scope_type_param_names.pop()
        return mutations

    def _remove_class_scopes(
        self,
    ) -> list[
        tuple[
            int,
            tuple[
                frozenset[str],
                frozenset[str],
                frozenset[str],
                frozenset[str],
                _ImportFlowState,
                tuple[str | None, str | None] | None,
                bool,
                bool,
                frozenset[str],
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
                    frozenset[str],
                    _ImportFlowState,
                    tuple[str | None, str | None] | None,
                    bool,
                    bool,
                    frozenset[str],
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
                        self._scope_nonlocal_names[index],
                        self._scope_shadow_names[index],
                        self._scope_import_states[index],
                        self._scope_receiver_overrides[index],
                        self._scope_excludes_enclosing_class[index],
                        self._scope_is_class[index],
                        self._scope_type_param_names[index],
                        self._scope_propagate_mutations[index],
                        frozenset(self._scope_mutated_names[index]),
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
                self._scope_nonlocal_names[index] = frozenset()
                self._scope_shadow_names[index] = type_param_names
                self._scope_import_states[index] = _ImportFlowState()
                self._scope_receiver_overrides[index] = None
                self._scope_excludes_enclosing_class[index] = False
                self._scope_is_class[index] = False
                self._scope_propagate_mutations[index] = False
                self._scope_mutated_names[index] = set()
            else:
                del self._scope_bound_names[index]
                del self._scope_global_names[index]
                del self._scope_nonlocal_names[index]
                del self._scope_shadow_names[index]
                del self._scope_import_states[index]
                del self._scope_receiver_overrides[index]
                del self._scope_excludes_enclosing_class[index]
                del self._scope_is_class[index]
                del self._scope_type_param_names[index]
                del self._scope_propagate_mutations[index]
                del self._scope_mutated_names[index]
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
                    frozenset[str],
                    _ImportFlowState,
                    tuple[str | None, str | None] | None,
                    bool,
                    bool,
                    frozenset[str],
                    bool,
                    frozenset[str],
                ],
            ]
        ],
    ) -> None:
        for _, frame in sorted(removed, reverse=True):
            if not frame[8]:
                continue
            marker_index = next(
                index
                for index in reversed(range(len(self._scope_type_param_names)))
                if self._scope_type_param_names[index] and not self._scope_is_class[index]
            )
            del self._scope_bound_names[marker_index]
            del self._scope_global_names[marker_index]
            del self._scope_nonlocal_names[marker_index]
            del self._scope_shadow_names[marker_index]
            del self._scope_import_states[marker_index]
            del self._scope_receiver_overrides[marker_index]
            del self._scope_excludes_enclosing_class[marker_index]
            del self._scope_is_class[marker_index]
            del self._scope_type_param_names[marker_index]
            del self._scope_propagate_mutations[marker_index]
            del self._scope_mutated_names[marker_index]

        for index, frame in sorted(removed):
            (
                restored_bound_names,
                restored_global_names,
                restored_nonlocal_names,
                restored_shadow_names,
                restored_import_state,
                restored_receiver_override,
                restored_excludes_enclosing_class,
                restored_is_class,
                restored_type_param_names,
                restored_propagate_mutations,
                restored_mutated_names,
            ) = frame
            self._scope_bound_names.insert(index, restored_bound_names)
            self._scope_global_names.insert(index, restored_global_names)
            self._scope_nonlocal_names.insert(index, restored_nonlocal_names)
            self._scope_shadow_names.insert(index, restored_shadow_names)
            self._scope_import_states.insert(index, restored_import_state)
            self._scope_receiver_overrides.insert(index, restored_receiver_override)
            self._scope_excludes_enclosing_class.insert(index, restored_excludes_enclosing_class)
            self._scope_is_class.insert(index, restored_is_class)
            self._scope_type_param_names.insert(index, restored_type_param_names)
            self._scope_propagate_mutations.insert(index, restored_propagate_mutations)
            self._scope_mutated_names.insert(index, set(restored_mutated_names))

    def _scope_receivers(self) -> tuple[str | None, str | None]:
        for override in reversed(self._scope_receiver_overrides):
            if override is not None:
                return override
        return self._receiver_name, self._receiver_parameter

    @property
    def _scope_import_targets(self) -> list[Mapping[str, str]]:
        """Compatibility view of the import-state stack used by diagnostics tests."""
        return [state.targets for state in self._scope_import_states]

    def _scope_imports(self) -> Mapping[str, str]:
        """Return the import binding each visible name resolves to.

        The nearest frame wins, matching how a name is bound at run time: an
        inner ``from other import helper`` governs the inner scope even where
        an enclosing scope imported the same name from elsewhere.
        """
        merged: dict[str, str] = {}
        for shadow_names, global_names, _nonlocal_names, state in zip(
            self._scope_shadow_names,
            self._scope_global_names,
            self._scope_nonlocal_names,
            self._scope_import_states,
            strict=True,
        ):
            merged = _without_import_roots(merged, shadow_names | global_names)
            merged.update(state.targets)
            if global_names:
                merged = _without_import_roots(merged, global_names)
                merged.update(
                    {
                        name: target
                        for name, target in self._module_import_targets.items()
                        if name.partition(".")[0] in global_names
                    }
                )
        return merged

    def _scope_uncertain_imports(self) -> frozenset[str]:
        """Return visible names whose import target is not flow-definite."""
        uncertain: set[str] = set()
        for shadow_names, global_names, _nonlocal_names, state in zip(
            self._scope_shadow_names,
            self._scope_global_names,
            self._scope_nonlocal_names,
            self._scope_import_states,
            strict=True,
        ):
            uncertain.difference_update(shadow_names | global_names)
            uncertain.update(state.uncertain_names)
            if global_names:
                callable_uncertain = state.uncertain_names & global_names
                uncertain.difference_update(global_names)
                uncertain.update(
                    name
                    for name in self._module_uncertain_import_names
                    if name.partition(".")[0] in global_names
                )
                uncertain.update(callable_uncertain)
        return frozenset(uncertain)

    def _scope_plain_imports(self) -> frozenset[str]:
        """Return visible roots introduced by unaliased dotted imports."""
        plain: set[str] = set()
        for shadow_names, global_names, _nonlocal_names, state in zip(
            self._scope_shadow_names,
            self._scope_global_names,
            self._scope_nonlocal_names,
            self._scope_import_states,
            strict=True,
        ):
            plain.difference_update(shadow_names | global_names)
            plain.update(state.plain_roots)
            if global_names:
                plain.difference_update(global_names)
                plain.update(
                    name
                    for name in self._module_plain_import_names
                    if name.partition(".")[0] in global_names
                )
        return frozenset(plain)

    def _scope_authoritative_imports(self) -> frozenset[str]:
        """Return definite imports established by function-flow states."""
        authoritative: set[str] = set()
        for shadow_names, global_names, _nonlocal_names, state in zip(
            self._scope_shadow_names,
            self._scope_global_names,
            self._scope_nonlocal_names,
            self._scope_import_states,
            strict=True,
        ):
            authoritative.difference_update(shadow_names | global_names)
            target_names = _import_binding_roots(state.targets)
            if state.flow_sensitive:
                authoritative.update(target_names)
            else:
                authoritative.difference_update(target_names)
            if global_names:
                authoritative.difference_update(global_names)
                authoritative.update(
                    name
                    for name in self._module_authoritative_import_names
                    if name.partition(".")[0] in global_names
                )
        return frozenset(authoritative)

    def _scope_local_import_names(self) -> frozenset[str]:
        """Return imports owned by the nearest active callable frame."""
        if not self._scope_import_states:
            return frozenset()
        return self._scope_import_states[-1].local_names

    def _scope_names(
        self,
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
        bound_names: set[str] = set()
        global_names: set[str] = set()
        import_bound_names: set[str] = set()
        seen_names: set[str] = set()
        for bound_frame, global_frame, nonlocal_frame, import_state in reversed(
            tuple(
                zip(
                    self._scope_bound_names,
                    self._scope_global_names,
                    self._scope_nonlocal_names,
                    self._scope_import_states,
                    strict=True,
                )
            )
        ):
            active_import_names = (
                _import_binding_roots(import_state.targets) | import_state.uncertain_names
            )
            for name in bound_frame | global_frame | active_import_names:
                if name in seen_names:
                    continue
                if name in nonlocal_frame:
                    continue
                if name in global_frame:
                    global_names.add(name)
                elif name in bound_frame and name not in import_state.local_names:
                    bound_names.add(name)
                elif name in active_import_names:
                    import_bound_names.add(name)
                else:
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
        name = getattr(node, "name", None)
        if isinstance(name, ast.Name):
            self._record_dynamic_names(frozenset((name.id,)))

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
            self.class_header_expressions.add(header)
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
        self._scope_nonlocal_names[-1] = _nonlocal_names_in_statements(
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
        self._record_dynamic_names(frozenset((node.name,)))

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
        import_state = self._scope_import_states[-1]
        self._scope_import_states[-1] = replace(
            import_state,
            targets=_class_import_targets_after_statement(
                import_state.targets, collector, global_names
            ),
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
        self.reference_plain_import_names[node] = self._scope_plain_imports()
        self.reference_import_bound[node] = import_bound
        self.reference_uncertain_import_names[node] = self._scope_uncertain_imports()
        self.reference_authoritative_import_names[node] = self._scope_authoritative_imports()
        self.reference_receiver_names[node], self.reference_receiver_parameters[node] = (
            self._scope_receivers()
        )
        self.reference_excludes_enclosing_class[node] = any(self._scope_excludes_enclosing_class)
        self._visit_chain_interiors(node)


def _import_bindings(alias: ast.alias) -> dict[str, str]:
    """Return the names and qualified targets introduced by one import.

    An unaliased dotted import binds its root package, while each dotted
    prefix remains useful for static member lookup (``import pkg.mod`` makes
    both ``pkg`` and ``pkg.mod`` available to the resolver). An explicit alias
    binds only that alias to the complete imported name.
    """
    if alias.asname is not None:
        return {alias.asname: alias.name}
    parts = alias.name.split(".")
    return {".".join(parts[:index]): ".".join(parts[:index]) for index in range(1, len(parts) + 1)}


def _update_import_bindings(bindings: dict[str, str], alias: ast.alias) -> None:
    """Apply one import while clearing aliases invalidated by rebinding.

    A later ``import other as package`` rebinds ``package`` and therefore
    invalidates dotted prefixes left by an earlier ``import package.module``.
    Unaliased siblings such as ``import package.util, package.models`` retain
    each other's prefixes because they bind the same root package.
    """
    imported = _import_bindings(alias)
    root = alias.asname or alias.name.partition(".")[0]
    if alias.asname is not None or bindings.get(root) not in (None, root):
        for name in tuple(bindings):
            if name == root or name.startswith(f"{root}."):
                del bindings[name]
    bindings.update(imported)


def _update_named_import_binding(bindings: dict[str, str], name: str, target: str) -> None:
    """Replace a named import and invalidate its previously bound prefixes."""
    for existing in tuple(bindings):
        if existing == name or existing.startswith(f"{name}."):
            del bindings[existing]
    bindings[name] = target


def _import_binding_roots(bindings: Mapping[str, str]) -> frozenset[str]:
    """Return the Python names bound by an import-target table."""
    return frozenset(name.partition(".")[0] for name in bindings)


def _without_import_roots(bindings: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    """Copy import targets while removing every prefix owned by ``names``."""
    roots = frozenset(names)
    return {
        name: target for name, target in bindings.items() if name.partition(".")[0] not in roots
    }


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
        self.import_names: set[str] = set()
        self.plain_import_names: set[str] = set()
        self.uncertain_import_names: set[str] = set()
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
            _update_import_bindings(self.import_targets, alias)
            name = alias.asname or alias.name.partition(".")[0]
            self.import_names.add(name)
            if alias.asname is None and "." in alias.name:
                self.plain_import_names.add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _relative_module(self._module_name, self._is_package, node.module, node.level)
        for alias in node.names:
            if alias.name != "*":
                name = alias.asname or alias.name
                self.import_names.add(name)
                if node.level > 0 and base is None:
                    self.uncertain_import_names.add(name)
                    continue
                _update_named_import_binding(
                    self.import_targets,
                    name,
                    f"{base}.{alias.name}" if base else alias.name,
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
    imported_names = frozenset(collector.import_names)
    uncertain_imported_names = frozenset(collector.uncertain_import_names)
    dynamic_names = frozenset(collector.names) - global_name_set - deleted_names
    return (
        (current_names - imported_names - deleted_names) | dynamic_names | uncertain_imported_names
    )


def _class_import_targets_after_statement(
    current_targets: Mapping[str, str],
    collector: _BindingCollector,
    global_names: Iterable[str],
    outer_targets: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Apply one class statement's imports and deletions in source order."""
    deleted_names = frozenset(collector.deleted_names) - frozenset(global_names)
    import_targets = _without_import_roots(current_targets, collector.import_names)
    import_targets = {
        name: target for name, target in import_targets.items() if name not in deleted_names
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
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return dynamic binders and initial import flow for one function.

    Import names are lexical, but their targets are not available until their
    statements execute. Parameters and type parameters already have ordinary
    dynamic values on entry; every other local import name starts uncertain so
    a pre-import use cannot fall through to an outer alias.
    """
    collector = _BindingCollector(module_name, is_package)
    entry_dynamic_names = frozenset(_argument_names(statement.args) | _type_param_names(statement))
    collector.names.update(entry_dynamic_names)
    for body_statement in statement.body:
        collector.visit(body_statement)
    collector.names.difference_update(collector.global_names | collector.nonlocal_names)
    local_import_names = (
        frozenset(collector.import_names)
        - frozenset(collector.global_names)
        - frozenset(collector.nonlocal_names)
    )
    return (
        frozenset(collector.names),
        local_import_names,
        (local_import_names - entry_dynamic_names) | frozenset(collector.uncertain_import_names),
    )


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


def _module_resolution(module: _Module, modules: Mapping[str, _Module]) -> _ModuleResolution:
    """Build the baseline declaration and dotted-prefix resolution view."""
    bindings: dict[str, _BindingDescriptor] = {}
    plain_events: dict[str, list[str]] = {}
    real_events: set[str] = set()
    direct_names: set[str] = set()

    def record(name: str, target: str, category: str) -> None:
        bindings[name] = _BindingDescriptor(target, category)
        direct_names.add(name)
        if category == "plain":
            plain_events.setdefault(name, []).append(target)
        else:
            real_events.add(name)

    for statement in module.tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                name = alias.asname or alias.name.partition(".")[0]
                category = "plain" if alias.asname is None and "." in alias.name else "real"
                record(name, alias.name, category)
        elif isinstance(statement, ast.ImportFrom) and statement.module != "__future__":
            base = _relative_module(
                module.name, module.is_package, statement.module, statement.level
            )
            if base is None:
                continue
            for alias in statement.names:
                if alias.name != "*":
                    name = alias.asname or alias.name
                    record(name, f"{base}.{alias.name}" if base else alias.name, "real")

    collector = _BindingCollector(module.name, module.is_package)
    nested_import_names: set[str] = set()
    for statement in module.tree.body:
        collector.visit(statement)
        if not isinstance(statement, (ast.Import, ast.ImportFrom)):
            nested_collector = _BindingCollector(module.name, module.is_package)
            nested_collector.visit(statement)
            nested_import_names.update(nested_collector.import_targets)
    competing_names = (collector.names | set(collector.import_targets)) - direct_names
    competing_names.update(collector.names & direct_names)
    competing_names.update(nested_import_names)
    blocked_names = real_events | competing_names
    prefixes: dict[str, str] = {}
    for name, targets in plain_events.items():
        if name in blocked_names:
            continue
        for target in targets:
            if target in modules:
                prefixes[target] = target
    plain_roots = frozenset(
        name for name, descriptor in bindings.items() if descriptor.category == "plain"
    )
    return _ModuleResolution(bindings, prefixes, plain_roots)


def _module_flow_states(
    module: _Module,
) -> tuple[dict[ast.stmt, _ImportFlowState], _ImportFlowState]:
    """Capture module binding state immediately before each top-level statement."""
    visitor = _ScopeCallVisitor(module.name, module.is_package)
    visitor._push_scope(
        frozenset(),
        frozenset(),
        frozenset(),
        # Direct imports become visible only when their statement is visited;
        # the lexical collector's final table is used solely to seed deferred
        # body analysis after this timeline has completed.
        import_targets={},
        import_flow_sensitive=True,
    )
    visitor._flow_mutate_targets = True
    states: dict[ast.stmt, _ImportFlowState] = {}
    capture_next = True
    for statement in module.tree.body:
        state = visitor._flow_state()
        is_definition = isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if state is not None and (capture_next or is_definition):
            states[statement] = replace(state, targets=dict(state.targets))
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visitor._visit_definition_header(statement)
            visitor._record_dynamic_names(frozenset((statement.name,)))
        elif isinstance(statement, ast.ClassDef):
            for expression in _class_header_nodes(statement):
                visitor.visit(expression)
            visitor._record_dynamic_names(frozenset((statement.name,)))
            _apply_class_directive_writes(visitor, statement)
        else:
            visitor.visit(statement)
        capture_next = is_definition
    final_state = visitor._flow_state() or _ImportFlowState(flow_sensitive=True)
    final_state = replace(final_state, targets=dict(final_state.targets))
    visitor._pop_scope()
    return states, final_state


def _join_flow_states(states: tuple[_ImportFlowState, ...]) -> _ImportFlowState:
    """Join conditional arms while retaining only agreeing import routes."""
    if not states:
        return _ImportFlowState(flow_sensitive=True)

    roots = set().union(
        *(
            set(_import_binding_roots(state.targets))
            | set(state.uncertain_names)
            | set(state.plain_roots)
            for state in states
        )
    )
    joined_targets: dict[str, str] = {}
    joined_uncertain: set[str] = set()
    joined_plain_roots: set[str] = set()
    for root in roots:
        target_maps = tuple(
            {
                name: target
                for name, target in state.targets.items()
                if name == root or name.startswith(f"{root}.")
            }
            for state in states
        )
        categories = tuple(root in state.plain_roots for state in states)
        uncertain = any(root in state.uncertain_names for state in states)
        if not uncertain and all(target_map == target_maps[0] for target_map in target_maps):
            joined_targets.update(target_maps[0])
        else:
            joined_uncertain.add(root)
        if all(category == categories[0] for category in categories):
            if categories[0]:
                joined_plain_roots.add(root)
        else:
            joined_uncertain.add(root)

    return _ImportFlowState(
        targets=joined_targets,
        uncertain_names=frozenset(joined_uncertain),
        local_names=states[0].local_names,
        flow_sensitive=any(state.flow_sensitive for state in states),
        flow_frozen=all(state.flow_frozen for state in states),
        plain_roots=frozenset(joined_plain_roots),
    )


def _apply_class_directive_writes(visitor: _ScopeCallVisitor, node: ast.ClassDef) -> None:
    """Apply immediate class global/nonlocal writes to the module timeline."""
    directives = _global_names_in_statements(node.body) | _nonlocal_names_in_statements(node.body)
    if not directives or not visitor._scope_import_states:
        return
    state = visitor._scope_import_states[-1]
    for statement in node.body:
        touched = _flow_touched_names((statement,)) & directives
        if not touched:
            continue
        affected = touched & (
            state.local_names | state.uncertain_names | _import_binding_roots(state.targets)
        )
        if not affected:
            continue
        state = replace(
            state,
            targets=_without_import_roots(state.targets, affected),
            uncertain_names=state.uncertain_names | affected,
            plain_roots=state.plain_roots - affected,
        )
    visitor._scope_import_states[-1] = state


def _context_for_flow_state(context: _ScopeContext, state: _ImportFlowState) -> _ScopeContext:
    """Overlay one source-position module state on an analysis context."""
    return replace(
        context,
        import_targets=state.targets,
        plain_import_names=state.plain_roots,
        uncertain_import_names=state.uncertain_names,
        authoritative_import_names=_import_binding_roots(state.targets),
    )


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
    plain_import_names = set(context.plain_import_names)
    plain_import_names.difference_update(collector.import_names)
    # Class-local dotted imports retain the established conservative class
    # behavior: they can refine an already-visible plain package route, while
    # a class-only route remains syntactic evidence.
    plain_import_names.update(collector.plain_import_names & context.plain_import_names)
    uncertain_import_names = set(context.uncertain_import_names)
    if isinstance(statement, ast.ImportFrom) and statement.level == 0:
        uncertain_import_names.update(collector.import_names)
    uncertain_import_names.difference_update(collector.deleted_names)
    deleted_plain_names = (
        context.class_scope_deleted_plain_names
        | (frozenset(collector.deleted_names) & context.plain_import_names)
    ) - frozenset(collector.import_names)
    return replace(
        context,
        bound_names=(context.bound_names - context.class_scope_bound_names) | class_bound_names,
        import_targets=_class_import_targets_after_statement(
            context.import_targets,
            collector,
            collector.global_names,
            context.class_scope_outer_import_targets,
        ),
        plain_import_names=frozenset(plain_import_names),
        uncertain_import_names=frozenset(uncertain_import_names),
        class_scope_bound_names=class_bound_names,
        class_scope_deleted_plain_names=deleted_plain_names,
    )


def _global_names(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Collect names declared global by one function or nested function."""
    return _global_names_in_statements(statement.body)


def _global_names_in_statements(statements: Iterable[ast.stmt]) -> frozenset[str]:
    collector = _BindingCollector()
    for statement in statements:
        collector.visit(statement)
    return frozenset(collector.global_names)


def _nonlocal_names_in_statements(statements: Iterable[ast.stmt]) -> frozenset[str]:
    collector = _BindingCollector()
    for statement in statements:
        collector.visit(statement)
    return frozenset(collector.nonlocal_names)


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


def _flow_touched_names(statements: Iterable[ast.stmt]) -> frozenset[str]:
    """Return names whose bindings may change in a control-flow suite."""
    collector = _BindingCollector()
    for statement in statements:
        collector.visit(statement)
    return frozenset(collector.names | collector.import_names | collector.deleted_names)


def _flow_touched_node(node: ast.AST) -> frozenset[str]:
    """Return names whose bindings may change while evaluating one node."""
    collector = _BindingCollector()
    collector.visit(node)
    return frozenset(collector.names | collector.import_names | collector.deleted_names)


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
    module_resolution = _module_resolution(module, modules)
    module_states, final_module_state = _module_flow_states(module)
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
        modules=modules,
        module_resolution=module_resolution,
        plain_import_names=module_resolution.plain_roots,
    )
    for symbol in symbols.values():
        relationships.add(
            symbol.container_id,
            symbol.node_id,
            RelationshipKind.CONTAINS.value,
            None,
        )
    pending: list[ast.stmt] = []

    def flush_pending() -> None:
        if not pending:
            return
        first_state = module_states.get(pending[0], final_module_state)
        _calls(
            _context_for_flow_state(context, first_state),
            list(pending),
            module.module_id,
            deferred_state=final_module_state,
        )
        pending.clear()

    for statement in module.tree.body:
        state = module_states.get(statement, final_module_state)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            flush_pending()
            header_binders, header_imports, _ = _scope_binders(
                statement, module.name, module.is_package
            )
            header_context = _context_for_flow_state(context, state)
            header_context = replace(
                header_context,
                bound_names=header_binders,
                import_targets=_without_import_roots(header_context.import_targets, header_imports),
                uncertain_import_names=frozenset(header_imports)
                | header_context.uncertain_import_names,
                authoritative_import_names=header_context.authoritative_import_names
                - header_imports,
            )
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
                replace(
                    header_context,
                    bound_names=frozenset(_type_param_names(statement)),
                ),
                [],
                symbols[statement].node_id,
                prefix_nodes=_signature_nodes(statement),
            )
            _calls(
                _context_for_flow_state(context, final_module_state),
                statement.body,
                symbols[statement].node_id,
                function_scope=statement,
            )
        elif isinstance(statement, ast.ClassDef):
            flush_pending()
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
                _context_for_flow_state(context, state),
                bound_names=frozenset(_type_param_names(statement)),
                class_scope_bound_names=frozenset(_type_param_names(statement)),
                class_scope_type_param_names=frozenset(_type_param_names(statement)),
                class_scope_outer_import_targets=context.import_targets,
                class_scope_outer_plain_import_names=context.plain_import_names,
                class_scope_outer_uncertain_import_names=context.uncertain_import_names,
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
                    receiver_parameter = _receiver_parameter_name(member)
                    method_context = replace(
                        _context_for_flow_state(context, final_module_state),
                        bound_names=frozenset(_type_param_names(statement)),
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
                        function_scope=member,
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
        else:
            pending.append(statement)
    flush_pending()


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
    # This traversal intentionally differs from _BindingCollector's
    # scope-bounded walk: _imports records graph edges and tally counts for
    # every syntactically unambiguous import, regardless of execution scope.
    # Conditional definitions have a separate declaration-grain concern, but
    # imports remain unambiguous at every nesting depth (O-03/M-4).
    # Keep the returned alias table module-scoped: the source-ordered scope
    # visitor supplies nested bindings at each expression, so exposing them
    # here would leak a function or class import into unrelated scopes.
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
                        _update_import_bindings(aliases, alias)
        elif isinstance(statement, ast.ImportFrom):
            # ``__future__`` imports are compile-time directives, not workspace
            # dependencies; exclude them from the graph and unresolved tally.
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
                # A nested named import depends on its containing module as
                # well as the selected declaration; emit that module edge
                # once, before the per-name symbol edges below.
                relationships.add(
                    module.module_id,
                    target_module.module_id,
                    RelationshipKind.IMPORTS.value,
                    _location(module.path, statement),
                )
            for alias in statement.names:
                if alias.name == "*":
                    # Star imports record the module dependency only. Expanding
                    # declarations according to ``__all__`` is a separate
                    # concern deferred to the declaration-grain analysis.
                    star_reference = (
                        base
                        if base is not None
                        else ("." * statement.level + (statement.module or ""))
                    )
                    if target_module is None:
                        tally.note_unresolved(
                            star_reference, root_mismatch_eligible=not root_escape
                        )
                        emitter.unresolved(
                            module.module_id,
                            star_reference,
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
                reference = _relative_import_reference(
                    base, statement.module, statement.level, alias.name
                )
                if root_escape:
                    tally.note_unresolved(reference, root_mismatch_eligible=False)
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
                        _update_named_import_binding(aliases, alias.asname or alias.name, reference)
            if statement in module_statements and target_module is not None and base is not None:
                aliases.setdefault(base.rsplit(".", 1)[-1], base)
    return aliases


def _calls(
    context: _ScopeContext,
    statements: list[ast.stmt],
    caller: str,
    class_declarations: Mapping[str, str] | None = None,
    prefix_nodes: tuple[ast.AST, ...] = (),
    function_scope: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
    deferred_state: _ImportFlowState | None = None,
) -> None:
    visitor = _ScopeCallVisitor(
        context.module_name,
        context.is_package,
        context.receiver_name,
        context.receiver_parameter,
        context.import_targets,
        context.uncertain_import_names,
        context.plain_import_names,
        context.authoritative_import_names,
    )
    for node in prefix_nodes:
        visitor.visit(node)
    if function_scope is not None:
        visitor.visit_function_body(
            function_scope,
            enclosing_import_targets=context.import_targets,
            enclosing_uncertain_names=context.uncertain_import_names,
            enclosing_local_import_names=context.authoritative_import_names,
        )
    else:
        # Module and class execution are source ordered too. A flow frame here
        # lets an import install a route before its first use and lets a later
        # direct write remove that route, while deferred function bodies keep
        # their dedicated final-state handling below.
        visitor._push_scope(
            frozenset(),
            frozenset(),
            frozenset(),
            import_targets=dict(context.import_targets),
            import_uncertain_names=context.uncertain_import_names,
            local_import_names=context.authoritative_import_names,
            import_flow_sensitive=True,
        )
        visitor._flow_mutate_targets = True
        if deferred_state is not None:
            visitor._defer_nested_bodies += 1
        for statement in statements:
            visitor.visit(statement)
        if deferred_state is not None:
            visitor._defer_nested_bodies -= 1
            pending_callables = visitor._deferred_callables
            visitor._deferred_callables = []
            for deferred_node, deferred_class_method in pending_callables:
                deferred_type_params = visitor._deferred_type_param_names.pop(
                    deferred_node, frozenset()
                )
                if isinstance(deferred_node, ast.Lambda):
                    visitor._visit_lambda_body(deferred_node, deferred_state)
                elif isinstance(deferred_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visitor._visit_function(
                        deferred_node,
                        nested_class_method=deferred_class_method,
                        owner_state_override=deferred_state,
                        skip_header=True,
                        deferred_type_param_names=deferred_type_params,
                    )
        visitor._pop_scope()
    class_header_nodes = {
        child for header in visitor.class_header_expressions for child in ast.walk(header)
    }
    definition_header_nodes = {
        child for header in visitor.definition_header_expressions for child in ast.walk(header)
    }
    outer_targets = context.class_scope_outer_import_targets or {}
    class_local_plain_names = frozenset(
        name
        for name, target in context.import_targets.items()
        if (target == name or target.startswith(f"{name}."))
        and context.import_targets.get(name) != outer_targets.get(name)
    )
    enclosing_class_target_names = frozenset(
        name
        for name, target in context.import_targets.items()
        if name not in outer_targets or outer_targets[name] != target
    )
    for candidate in visitor.calls:
        expression_context = context
        blocked_plain_names: frozenset[str] = frozenset()
        if (
            visitor.call_excludes_enclosing_class[candidate]
            and (
                context.class_scope_outer_import_targets is not None
                or visitor.call_in_class_body[candidate]
            )
            and candidate.func not in definition_header_nodes
        ):
            expression_context = _without_enclosing_class_scope(context)
        call_import_targets = visitor.call_import_targets[candidate]
        if (
            visitor.call_excludes_enclosing_class[candidate]
            and (
                context.class_scope_outer_import_targets is not None
                or visitor.call_in_class_body[candidate]
            )
            and candidate.func not in definition_header_nodes
        ):
            stripped_call_import_targets = _without_import_roots(
                call_import_targets, enclosing_class_target_names
            )
            stripped_call_import_targets.update(
                {
                    name: target
                    for name, target in call_import_targets.items()
                    if name.partition(".")[0] in visitor.call_local_import_names[candidate]
                }
            )
            call_import_targets = stripped_call_import_targets
        call_import_bound = (
            visitor.call_import_bound[candidate] - visitor.call_bound_names[candidate]
        )
        if (
            visitor.call_excludes_enclosing_class[candidate]
            and (
                context.class_scope_outer_import_targets is not None
                or visitor.call_in_class_body[candidate]
            )
            and candidate.func not in definition_header_nodes
        ):
            call_import_bound -= context.import_targets.keys()
        call_uncertain_names = visitor.call_uncertain_import_names[candidate]
        if (
            visitor.call_excludes_enclosing_class[candidate]
            and (
                context.class_scope_outer_import_targets is not None
                or visitor.call_in_class_body[candidate]
            )
            and candidate.func not in definition_header_nodes
        ):
            call_uncertain_names -= context.uncertain_import_names - (
                context.class_scope_outer_uncertain_import_names or frozenset()
            )
        call_plain_import_names = visitor.call_plain_import_names[candidate]
        if context.class_scope_outer_import_targets is not None:
            blocked_plain_names = frozenset(call_plain_import_names | class_local_plain_names)
            call_plain_import_names = frozenset(
                call_plain_import_names & context.plain_import_names
            )
            call_import_targets = _without_import_roots(call_import_targets, blocked_plain_names)
        if candidate.func in class_header_nodes:
            call_uncertain_names = frozenset(
                set(call_uncertain_names)
                | (
                    context.class_scope_deleted_plain_names
                    & frozenset((_base_identifier(candidate.func),))
                )
            )
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
                call_import_targets,
                call_plain_import_names,
                call_import_bound,
                call_uncertain_names,
                visitor.call_authoritative_import_names[candidate],
                visitor.call_bound_names[candidate] | call_import_bound | blocked_plain_names,
            ),
            caller,
            candidate.func,
            _location(context.path, candidate.func),
            None if visitor.call_excludes_enclosing_class[candidate] else class_declarations,
            RelationshipKind.CALLS.value,
        )
    for reference in visitor.references:
        expression_context = context
        blocked_plain_names = frozenset()
        if (
            visitor.reference_excludes_enclosing_class[reference]
            and (
                context.class_scope_outer_import_targets is not None
                or visitor.reference_in_class_body[reference]
            )
            and reference not in definition_header_nodes
        ):
            expression_context = _without_enclosing_class_scope(context)
        reference_import_targets = visitor.reference_import_targets[reference]
        if (
            visitor.reference_excludes_enclosing_class[reference]
            and (
                context.class_scope_outer_import_targets is not None
                or visitor.reference_in_class_body[reference]
            )
            and reference not in definition_header_nodes
        ):
            stripped_reference_import_targets = _without_import_roots(
                reference_import_targets, enclosing_class_target_names
            )
            stripped_reference_import_targets.update(
                {
                    name: target
                    for name, target in reference_import_targets.items()
                    if name.partition(".")[0] in visitor.reference_local_import_names[reference]
                }
            )
            reference_import_targets = stripped_reference_import_targets
        reference_import_bound = (
            visitor.reference_import_bound[reference] - visitor.reference_bound_names[reference]
        )
        if (
            visitor.reference_excludes_enclosing_class[reference]
            and (
                context.class_scope_outer_import_targets is not None
                or visitor.reference_in_class_body[reference]
            )
            and reference not in definition_header_nodes
        ):
            reference_import_bound -= context.import_targets.keys()
        reference_uncertain_names = set(visitor.reference_uncertain_import_names[reference])
        if (
            visitor.reference_excludes_enclosing_class[reference]
            and reference in definition_header_nodes
            and context.class_scope_outer_import_targets is not None
        ):
            expression_context = _without_enclosing_class_scope(context)
            reference_import_targets = _without_import_roots(
                reference_import_targets, enclosing_class_target_names
            )
            reference_uncertain_names -= context.uncertain_import_names - (
                context.class_scope_outer_uncertain_import_names or frozenset()
            )
        if (
            visitor.reference_excludes_enclosing_class[reference]
            and reference not in definition_header_nodes
        ):
            reference_uncertain_names -= context.uncertain_import_names - (
                context.class_scope_outer_uncertain_import_names or frozenset()
            )
            if context.class_scope_outer_import_targets is None and reference in class_header_nodes:
                reference_uncertain_names.update(
                    name
                    for name in _import_binding_roots(visitor.reference_import_targets[reference])
                    if context.import_targets.get(name)
                    != visitor.reference_import_targets[reference].get(name)
                )
        reference_plain_import_names = visitor.reference_plain_import_names[reference]
        if context.class_scope_outer_import_targets is not None:
            blocked_plain_names = frozenset(reference_plain_import_names | class_local_plain_names)
            reference_plain_import_names = frozenset(
                reference_plain_import_names & context.plain_import_names
            )
            reference_import_targets = _without_import_roots(
                reference_import_targets, blocked_plain_names
            )
        if reference in class_header_nodes:
            reference_uncertain_names.update(
                context.class_scope_deleted_plain_names & frozenset((_base_identifier(reference),))
            )
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
                reference_import_targets,
                reference_plain_import_names,
                reference_import_bound
                - (
                    context.class_scope_bound_names
                    if reference in class_header_nodes
                    else frozenset()
                ),
                frozenset(reference_uncertain_names),
                visitor.reference_authoritative_import_names[reference],
                visitor.reference_bound_names[reference]
                | reference_import_bound
                | blocked_plain_names,
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
        plain_import_names=context.class_scope_outer_plain_import_names or frozenset(),
        uncertain_import_names=(context.class_scope_outer_uncertain_import_names or frozenset()),
        class_scope_bound_names=type_param_names,
        class_scope_type_param_names=type_param_names,
        class_scope_outer_import_targets=None,
        class_scope_outer_plain_import_names=None,
        class_scope_outer_uncertain_import_names=None,
    )


def _scoped_context(
    context: _ScopeContext,
    bound_names: frozenset[str],
    global_names: frozenset[str],
    shadow_names: frozenset[str],
    import_targets: Mapping[str, str],
    plain_import_names: frozenset[str],
    import_bound_names: frozenset[str],
    uncertain_import_names: frozenset[str],
    authoritative_import_names: frozenset[str],
    import_shadow_names: frozenset[str],
) -> _ScopeContext:
    """Narrow one scope's context to the bindings visible at one expression.

    ``import_bound_names`` are the names a nested scope binds by importing
    them. They are removed from the binders the enclosing scope contributes:
    the nearest binding governs, and an import is not a dynamic local.
    """
    visible_import_targets = _without_import_roots(context.import_targets, import_shadow_names)
    visible_plain_names = set(context.plain_import_names - import_shadow_names)
    visible_uncertain_names = set(context.uncertain_import_names - import_shadow_names)
    visible_authoritative_names = set(context.authoritative_import_names - import_shadow_names)
    visitor_import_names = _import_binding_roots(import_targets) | uncertain_import_names
    visible_import_targets = _without_import_roots(visible_import_targets, visitor_import_names)
    visible_import_targets.update(import_targets)
    visible_plain_names.difference_update(import_shadow_names)
    visible_plain_names.update(plain_import_names)
    visible_uncertain_names.difference_update(visitor_import_names)
    visible_uncertain_names.update(uncertain_import_names)
    visible_authoritative_names.difference_update(visitor_import_names)
    visible_authoritative_names.update(authoritative_import_names)
    return replace(
        context,
        bound_names=bound_names - global_names - import_bound_names,
        import_targets=visible_import_targets,
        plain_import_names=frozenset(visible_plain_names),
        uncertain_import_names=frozenset(visible_uncertain_names),
        authoritative_import_names=frozenset(visible_authoritative_names),
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
    if root in context.uncertain_import_names:
        return False
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
    return (
        root is not None
        and root in context.builtins
        and root not in context.import_targets
        and root not in context.uncertain_import_names
    )


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
    if any(text == name or text.startswith(f"{name}.") for name in context.uncertain_import_names):
        return None
    if (
        local_target is not None
        and head in context.aliases
        and local_target != context.aliases[head]
        and head not in context.authoritative_import_names
    ):
        return None
    if head in context.bound_names and head != context.receiver_name:
        return None
    # Module aliases are a final-state convenience only when the source
    # position has an active direct import route. This prevents a later import
    # from leaking backward into an earlier default or class header.
    if (
        head in context.aliases
        and head not in context.authoritative_import_names
        and text not in context.import_targets.values()
    ):
        return None
    if head == context.receiver_name and class_declarations is not None:
        # ``self`` is tied to the class statement that owns the caller, not to
        # whichever same-named class was assigned to the module name last.
        # Resolve through that statement's method table so repeated method
        # names remain last-wins locally without leaking across class objects.
        if "." not in text:
            return None
        _, _, tail = text.partition(".")
        return class_declarations.get(tail)
    if head in context.plain_import_names:
        if "." not in text:
            return None
        for prefix, plain_target in sorted(
            context.import_targets.items(),
            key=lambda item: len(item[0].split(".")),
            reverse=True,
        ):
            if prefix == head:
                continue
            marker = f"{prefix}."
            if text.startswith(marker):
                suffix = text[len(marker) :]
                return context.declarations.get(f"{plain_target}.{suffix}")
        if context.module_resolution is not None:
            for prefix, target in sorted(
                context.module_resolution.prefixes.items(),
                key=lambda item: len(item[0].split(".")),
                reverse=True,
            ):
                if text == prefix:
                    return context.declarations.get(target)
                marker = f"{prefix}."
                if text.startswith(marker):
                    suffix = text[len(marker) :]
                    return context.declarations.get(f"{target}.{suffix}")
        return None
    # Analysis declarations cover the workspace as a whole, so resolve a
    # dotted call through its longest imported prefix. This preserves a
    # specific alias such as ``pkg.submodule`` when a shorter ``pkg`` alias is
    # also visible, and keeps unaliased dotted imports aligned with Python's
    # root-package binding.
    parts = text.split(".")
    for index in range(len(parts), 0, -1):
        prefix = ".".join(parts[:index])
        resolved_target: str | None = context.import_targets.get(prefix)
        if resolved_target is None:
            resolved_target = context.aliases.get(prefix)
        if resolved_target is None:
            continue
        suffix = ".".join(parts[index:])
        target_name = f"{resolved_target}.{suffix}" if suffix else resolved_target
        # Once a prefix is bound, an absent member is unresolved; falling back
        # to a shorter alias could attribute the same source expression to a
        # different workspace object.
        return context.declarations.get(target_name)
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


def _relative_import_reference(
    base: str | None, imported: str | None, level: int, name: str
) -> str:
    """Render an imported name without losing an invalid relative prefix.

    A relative import that ascends beyond the analyzed package has no resolved
    module base, but its source spelling still identifies what was requested.
    Retaining the leading dots keeps that unresolved fact distinct from a
    bare absolute import and prevents it from looking like a root-mismatch
    candidate.
    """
    if base is not None:
        return f"{base}.{name}" if base else name
    if level > 0:
        prefix = "." * level
        if imported:
            prefix += f"{imported}."
        return f"{prefix}{name}"
    return f"{imported}.{name}" if imported else name


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
