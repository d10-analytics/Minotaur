# Structural analysis contract

Minotaur records source facts that can be established without executing the
workspace. This contract describes the shared graph meanings and the current
Python dotted-import slice. It is a static-analysis contract: a relationship
is evidence in the analyzed source, not a claim about what an interpreter will
dispatch at runtime.

The Python guide explains how to run the analyzer. The behavioral proof
references below name natural public tests; each test builds a temporary
workspace, analyzes it through the normal public path, and asserts the graph
relationship or query result that would fail if the behavior were removed.

## Shared graph meanings

An `IMPORTS` relationship records a syntactic import statement. A `CALLS`
relationship records a call whose static target was resolved. A `REFERENCES`
relationship records a resolved non-call load. An unresolved expression is
retained as an explicit unresolved-reference node with source evidence. An
`IMPORTS` relationship by itself never creates a `CALLS` or `REFERENCES`
relationship to every member of the imported module.

Graph locations use zero-based line and character coordinates. Public query
text uses one-based line and column coordinates. For example, a graph
location beginning at `Range(1, 0)` is printed by `query callers` as line 2,
column 1 when that relationship is a caller result. Node IDs remain distinct
when two declarations have the same label; a lookup may select one exact ID
for a use without merging the declarations.

The contract is language-specific where syntax and binding rules differ.
Python import assignment rules do not define JavaScript ESM behavior, and this
page makes no JavaScript semantic claim.

## Python binding and source positions

The interpreter records one source fact at the expression's source position.
Each direct named, aliased, relative-named, or plain dotted import establishes
a route for the names it binds. The route applies to both calls and non-call
loads, and the shared emitter preserves the owner, exact target identity, and
location. The separate `IMPORTS` relationship remains evidence of the import
statement.

Module and class execution are walked in source order. An immediate expression
therefore sees the binding state at its own position. A function or method body
is deferred: it uses the final direct module state. A nested function, lambda,
or nested method uses the final state of its owning enclosing function, with
nearer lexical routes and its own callable-local overlay taking precedence.
This is a structural final-state policy; the analyzer does not simulate
invocation or propagate callable side effects. Defaults, decorators, class
headers, and annotations are evaluated in their enclosing scope at definition
time, so they use the source-position state there. A lambda body is deferred
but its defaults and parameters are not.

In a function, a direct import is reportable and can resolve after the import:
`from library import named`, `from .library import named`, `import pkg.sub`,
and their aliases support calls and loads. Before that import, or after a
managed imported name is assigned, deleted, or made uncertain, the use is one
explicit unresolved site and does not fall through to an outer route. A later
direct reimport restores its new route. A callable-local import changes that
callable's overlay only; it does not mutate an enclosing or sibling callable.

The concrete collision behavior is the current qualified-declaration lookup's
compatibility behavior. In the lowercase case, both `pkg.sub.child.go`
declarations remain distinct and the selected function is
`pkg/sub/child.py`, `Range(0, 0)-(0, 14)`. In the uppercase case, both
`pkg.sub.Child.go` declarations remain distinct and the selected package
method is `pkg/sub/__init__.py`, `Range(1, 4)-(1, 22)`. When there is no
module declaration, the class-only package method remains the target at
`pkg/sub/__init__.py`, `Range(1, 4)-(1, 22)`. These path-dependent outcomes
do not define an owner precedence or a declaration-kind precedence rule.
The lowercase and uppercase identity cases are proved by
`test_plain_dotted_imports_preserve_existing_declaration_lookup_identity`,
`test_plain_dotted_imports_preserve_lowercase_and_uppercase_collision_identity`,
and `test_plain_dotted_import_class_only_fallback_preserves_method_identity`
in `tests/language_interpreter/python/test_interpreter.py`.

## Python lexical and class boundaries

Ordinary dynamic locals suppress unresolved member facts in function and method
bodies. This includes parameters, assignments, loop and context-manager
targets, match captures, and comprehension targets. Walrus targets bind the
enclosing function. `global` names remain eligible for module routes;
`nonlocal` names are bound to their enclosing lexical scope. A receiver-shaped
`self` or `cls` can resolve members through its owning class, while a member
that cannot resolve remains unresolved rather than being silently dropped.

A class header runs in the enclosing scope. Its body runs in a separate,
sequential class namespace: earlier assignments and imports can affect later
class headers and method headers, and deleting one can reveal an enclosing
route. Ordinary class locals and class imports are hidden from method bodies;
method bodies retain their own imports and enclosing function or module routes.
The existing class behavior can refine an already-visible plain package route,
but a class-only plain import remains syntactic evidence. This is not direct
class-import support. Nested class methods are attributed to the nearest
emitted owner, and nested class declarations themselves are outside the
emitted declaration slice.

Assignment effects follow expression order: an ordinary assignment reads its
right-hand side before writing targets; augmented assignment reads its target
before its right-hand side and write; a walrus commits after its value and
before later siblings. Attribute and subscript stores preserve the root load;
annotation-only statements do not invent a binding. Structural sites after a
direct `return` or `raise` are retained, but the unreachable tail cannot
advance binding state.

### Supported behavior and proof

The AC-01 through AC-04 rows below are natural public proofs. “Owner” is the
graph source node that receives the relationship. Relationship locations are
graph coordinates unless a row explicitly gives public query output.

| Acceptance area | Observed target and boundary | Natural proof |
| --- | --- | --- |
| AC-01 | Named, aliased, relative-named, and plain dotted imports in a function resolve both calls and non-call loads after the import, preserving one owner, target identity, and location. | `test_function_local_import_routes_bind_calls_and_loads_at_source_positions` and `test_source_position_routes_prove_owner_location_and_syntactic_imports` in `tests/language_interpreter/python/test_interpreter.py` |
| AC-02 | Assignment or deletion after an import emits one unresolved site without stale target or outer fallback; a later direct reimport resolves its new route. | `test_function_import_loss_and_reimport_emit_one_unresolved_site`, `test_nested_global_mutation_invalidates_only_current_callable_overlay`, and `test_plain_route_history_preserves_source_position_and_final_route` |
| AC-03 | Deferred module bodies use final module state; nested deferred functions, lambdas, and methods use final enclosing-function state. Defaults and parameters retain immediate or lexical behavior, and callable-local imports do not mutate siblings. Within a compound statement, an incoming route remains available to ordered reads until a direct write; affected roots are uncertain at continuation. A `try` success path supplies its `else` state, handler types read before their `as` targets, and match-guard writes affect later guards and continuation. | `test_module_default_before_import_and_deferred_body_use_final_state`, `test_nested_callables_use_final_enclosing_function_import_state`, `test_module_lambda_body_uses_final_state_with_immediate_defaults_and_parameters`, `test_nested_callable_import_does_not_mutate_enclosing_or_sibling_routes`, `test_compound_body_preserves_incoming_call_and_reference_before_mutation`, `test_try_handler_starts_after_possible_try_body_write_but_before_handler_write`, `test_try_else_preserves_success_body_state_and_post_try_uncertainty`, `test_try_handler_type_reads_import_before_same_named_exception_target`, `test_try_body_write_still_blocks_same_named_handler_type`, and `test_match_guard_walrus_blocks_later_guards_and_continuation` |
| An eligible imported parent resolves an exact call and a non-call load. | Owner `app`; `CALLS` targets `pkg.sub.child.go` at call-site `Range(1, 0)`, and `REFERENCES` targets the same node at load-site `Range(2, 11)`. There is no unresolved `pkg.sub.child.go` and no repeated-segment decoy edge. | `test_plain_dotted_imports_resolve_exact_call_and_load_without_decoys` |
| Selected sibling prefixes accumulate only within component boundaries. | Owner `app` has `CALLS` edges to `pkg.sub.go`, `pkg.other.go`, and `pkg.deep.go`; unrelated textual prefixes are not admitted. Overlapping eligible prefixes use the selected descendant target. | `test_plain_dotted_imports_accumulate_selected_component_prefixes` and `test_plain_dotted_overlapping_prefixes_keep_component_boundary_identity` |
| An admitted parent recalls descendants and preserves exact declaration identity. | Owner `parent` and owner `parent_plus_child` both target the same `pkg.sub.child.go` node for call and load. Same-labelled nodes remain separate; lowercase selects the function path, uppercase selects the package-method path, and class-only fallback selects the package method. `pkg.other.go` and `pkg.submarine.go` remain unresolved when only `pkg.sub` is imported. | `test_plain_dotted_imports_preserve_existing_declaration_lookup_identity`, `test_plain_parent_and_parent_plus_child_imports_keep_one_target_identity`, `test_plain_dotted_import_rejects_out_of_prefix_textual_and_same_module_decoys`, and the collision tests named above |
| Final route composition preserves a real route and rejects a stale prefix. | Immediate uses retain the route active at their own position; a later conflicting plain route cannot restore a stale alias, and final unresolved routes do not fall through to a secondary target. | `test_dotted_import_final_binding_orders_preserve_real_routes`, `test_plain_route_history_preserves_source_position_and_final_route`, and `test_invalidated_plain_route_blocks_secondary_root_decoys_for_call_and_load` |
| Module binders invalidate every affected prefix while lexical descriptors retain nearest routes. | A module binder leaves an affected module use unresolved; a direct function-local route resolves in that function, while class and control-flow nested plain imports remain syntactic. | `test_dotted_import_module_binders_invalidate_every_prefix_use`, `test_dotted_import_module_binders_include_legal_definition_headers`, `test_dotted_import_lexical_descriptors_preserve_nearest_routes`, and `test_nested_plain_dotted_imports_are_syntactic_only_in_each_container` |
| Fallback and boundary cases stay explicit. | A valid imported `list` can call `list.go`; a missing `missing.sub` keeps `missing.sub.go` and bare `missing` unresolved. Missing members retain `IMPORTS` evidence without a synthetic member edge, and a bare root or unimported sibling does not fall through to a same-module or synthetic target. | `test_dotted_import_fallback_builtin_and_missing_boundaries`, `test_plain_dotted_missing_module_and_missing_member_keep_import_evidence_without_fallback`, and `test_plain_dotted_root_load_and_unimported_sibling_remain_unresolved` |
| AC-04 | Direct function-body imports resolve. Class-body plain imports may refine an already-visible plain package route, while a class-only route remains syntactic; control-flow imports remain syntactic and do not establish a route for contained or later use. | `test_function_local_import_routes_bind_calls_and_loads_at_source_positions`, `test_lexical_class_route_installation_deletion_and_method_restoration`, `test_nested_plain_dotted_imports_remain_syntactic_only`, `test_nested_plain_dotted_imports_are_syntactic_only_in_each_container`, and `test_nested_plain_dotted_import_does_not_block_same_module_declaration` |
| AC-04 query meaning | Generated graph evidence is a `CALLS` edge from `caller.owner` to `pkg.sub.go`; `query callers` prints `caller.py:3:12  caller.owner`, and `query impact --depth 1` prints `depth 0: pkg.sub.go` followed by `depth 1: caller.owner`. | `test_dotted_import_call_graph_drives_exact_callers` and `test_dotted_import_call_graph_drives_inbound_impact` in `tests/query/test_symbols.py` and `tests/query/test_impact.py` |
| Resolved non-call loads count as uses independently of calls. | A load-only owner produces a `REFERENCES` edge and keeps `pkg.sub.go` out of `unreferenced`, while it produces no caller and no inbound impact. | `test_dotted_import_relations_independently_count_as_uses` and `test_dotted_import_load_graph_does_not_create_impact` in `tests/query/test_unreferenced.py` and `tests/query/test_impact.py` |

The proof rows distinguish graph coordinates from the one-based query
coordinate shown by the CLI. They also distinguish a resolved member use from
the separate `IMPORTS` fact: importing `pkg.sub` does not imply a use of every
declaration below `pkg.sub`.

## Explicit limits

The following boundaries are intentional. They describe cases where the
analyzer remains conservative or where runtime meaning is outside a structural
graph contract.

| Example meaning | Status and current boundary |
| --- | --- |
| A separate nested writer does not erase an outer import dependency. | **Supported.** Nested callable imports use their own overlay and do not mutate an enclosing or sibling callable. |
| A function-local named import resolves subject to local shadowing. | **Supported.** Direct local named, aliased, relative-named, and plain dotted imports resolve after their source position; dynamic locals still shadow them. |
| Direct dynamic reassignment prevents the old imported target. | **Supported.** Assignment, deletion, and uncertainty invalidate the managed route and emit one unresolved site; a direct reimport restores it. |
| Both control-flow branches bind the same target. | **Outside this slice.** Compound imports remain syntactic facts and do not create branch joins. |
| Different branch targets leave the final call unresolved. | **Known-limited.** The existing last-wins alias behavior is not certified as a correct branch analysis. |
| One branch may leave the name unbound. | **Known-limited.** Reachability and control-flow binding analysis are outside this slice. |
| A call inside an importing branch can resolve to that branch's target. | **Outside this slice.** Control-flow-local import production is not modeled. |
| A deferred function body may use an import written later. | **Supported.** Deferred module bodies use final module state; nested deferred callables use final state of their owning enclosing function. |
| An immediate expression before its import stays unresolved. | **Supported.** Immediate module, class-header, default, and annotation expressions use the route active at their own source position. |
| An unreachable source use remains structural evidence. | **Supported.** Structural sites after `return` or `raise` remain visible, while the unreachable tail cannot advance binding state. |
| A loop may replace an imported name. | **Outside this slice.** Comprehensive loop and branch reasoning is not modeled; possibly changed roots become uncertain. |
| A called function's global side effect is not propagated. | **Outside this slice.** Invocation timing and interprocedural callable side effects are not modeled. |

These limits are deliberate. The current slice does not add control-flow joins,
control-flow-local import producers, invocation timing, interprocedural side
effects, a second resolver, a declaration-owner or declaration-kind precedence
rule, runtime dispatch, graph-schema or persistent-state changes, or activation
of unrelated dormant flow machinery. It makes no JavaScript-specific claim.

## Query consequences

A resolved `CALLS` edge supplies a caller and an inbound impact path. A
resolved `REFERENCES` edge is an independent structural use and is considered
by `unreferenced`, but the current inbound-impact traversal does not treat a
load-only owner as a caller. Unresolved text remains available for
recall-oriented caller matching without becoming a resolved use of a candidate
target. These meanings are tested through the public CLI and generated graph
documents in the query proofs above.
