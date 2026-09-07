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

## Python dotted imports

The supported dotted form is a plain module import at module scope, such as
`import pkg.sub`. When the imported parent is an eligible workspace module,
the analyzer admits descendants whose remaining components stay below that
parent's component boundary. A use such as `pkg.sub.child.go()` can therefore
look up `pkg.sub.child.go` without a separate `import pkg.sub.child`. The
existing qualified-declaration lookup owns the final exact target ID after the
remaining components are appended; this feature does not introduce a second
resolver.

Eligibility is component-bounded. `import pkg.sub` admits `pkg.sub.go` and
`pkg.sub.deep.go`, but it does not admit `pkg.other.go` or the text-prefix
decoy `pkg.submarine.go`. If both `pkg.sub` and `pkg.sub.deep` are eligible,
the longest eligible component prefix is used. A parent-only import and a
parent-plus-child import therefore preserve one target identity for the same
use. A bare `pkg` load remains unresolved, as does a missing module or missing
member; the import fact remains separate evidence.

The final module-wide record aggregates direct bindings before expression
resolution. A later true alias or other real route can own a name. A later
plain dotted import cannot keep a corrupted earlier alias route, so competing
module binders conservatively invalidate every use of the affected prefix.
This rule is about the supported plain-prefix route and does not establish a
general source-order or alias-precedence engine.

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

### Supported behavior and proof

The following rows state the supported dotted behavior. “Owner” is the graph
source node that receives the relationship. Relationship locations are graph
coordinates unless the row explicitly gives public query output.

| Supported fact | Observed target and boundary | Natural proof |
| --- | --- | --- |
| An eligible imported parent resolves an exact call and a non-call load. | Owner `app`; `CALLS` targets `pkg.sub.child.go` at call-site `Range(1, 0)`, and `REFERENCES` targets the same node at load-site `Range(2, 11)`. There is no unresolved `pkg.sub.child.go` and no repeated-segment decoy edge. | `test_plain_dotted_imports_resolve_exact_call_and_load_without_decoys` |
| Selected sibling prefixes accumulate only within component boundaries. | Owner `app` has `CALLS` edges to `pkg.sub.go`, `pkg.other.go`, and `pkg.deep.go`; unrelated textual prefixes are not admitted. Overlapping eligible prefixes use the selected descendant target. | `test_plain_dotted_imports_accumulate_selected_component_prefixes` and `test_plain_dotted_overlapping_prefixes_keep_component_boundary_identity` |
| An admitted parent recalls descendants and preserves exact declaration identity. | Owner `parent` and owner `parent_plus_child` both target the same `pkg.sub.child.go` node for call and load. Same-labelled nodes remain separate; lowercase selects the function path, uppercase selects the package-method path, and class-only fallback selects the package method. `pkg.other.go` and `pkg.submarine.go` remain unresolved when only `pkg.sub` is imported. | `test_plain_dotted_imports_preserve_existing_declaration_lookup_identity`, `test_plain_parent_and_parent_plus_child_imports_keep_one_target_identity`, `test_plain_dotted_import_rejects_out_of_prefix_textual_and_same_module_decoys`, and the collision tests named above |
| Final route composition preserves a real route and rejects a stale prefix. | `plain_then_real` calls `alternate.go`; `real_then_plain` leaves `pkg.go` unresolved and emits no call to the alternate target. | `test_dotted_import_final_binding_orders_preserve_real_routes` |
| Module binders invalidate every affected prefix while lexical descriptors retain nearest routes. | Owner `app` leaves `pkg.sub.go` unresolved after a module binder; `app.caller` remains unresolved after its nested import, while `app.Runner.run` calls `pkg.sub.go` through the module route. | `test_dotted_import_module_binders_invalidate_every_prefix_use`, `test_dotted_import_module_binders_include_legal_definition_headers`, and `test_dotted_import_lexical_descriptors_preserve_nearest_routes` |
| Fallback and boundary cases stay explicit. | A valid imported `list` can call `list.go`; a missing `missing.sub` keeps `missing.sub.go` and bare `missing` unresolved. Missing members retain `IMPORTS` evidence without a synthetic member edge, and a bare root or unimported sibling does not fall through to a same-module or synthetic target. | `test_dotted_import_fallback_builtin_and_missing_boundaries`, `test_plain_dotted_missing_module_and_missing_member_keep_import_evidence_without_fallback`, and `test_plain_dotted_root_load_and_unimported_sibling_remain_unresolved` |
| Nested plain imports remain syntactic only. | Function, class, and control-flow nested imports produce `IMPORTS` evidence, but their nested uses remain unresolved and do not produce `CALLS` or `REFERENCES` through the module prefix. | `test_nested_plain_dotted_imports_remain_syntactic_only` and `test_nested_plain_dotted_imports_are_syntactic_only_in_each_container` |
| Resolved call edges drive caller and inbound-impact answers. | Generated graph evidence is a `CALLS` edge from `caller.owner` to `pkg.sub.go`; `query callers` prints `caller.py:4:5  caller.owner`, and `query impact --depth 1` prints `depth 1: caller.owner`. | `test_dotted_import_call_graph_drives_exact_callers` and `test_dotted_import_call_graph_drives_inbound_impact` in `tests/query/test_symbols.py` and `tests/query/test_impact.py` |
| Resolved non-call loads count as uses independently of calls. | A load-only owner produces a `REFERENCES` edge and keeps `pkg.sub.go` out of `unreferenced`, while it produces no caller and no inbound impact. | `test_dotted_import_relations_independently_count_as_uses` and `test_dotted_import_load_graph_does_not_create_impact` in `tests/query/test_unreferenced.py` and `tests/query/test_impact.py` |

The proof rows distinguish graph coordinates from the one-based query
coordinate shown by the CLI. They also distinguish a resolved member use from
the separate `IMPORTS` fact: importing `pkg.sub` does not imply a use of every
declaration below `pkg.sub`.

## Explicit limits and planned behavior

The following approved Python examples are documented so that their status is
clear. “Planned” means the example is a recognized future contract and is not
certified by the dotted-import behavior above. “Known-limited” means the
current analyzer deliberately leaves the case conservative or may retain a
structural result whose runtime meaning is narrower.

| Example meaning | Status and current boundary |
| --- | --- |
| A separate nested writer does not erase an outer import dependency. | **Planned.** The exact legacy-alias example is not certified here; the implemented deferred-body control is narrower. |
| A function-local named import resolves subject to local shadowing. | **Planned.** Function-local imports remain syntactic for this slice; no new local prefix producer or broader local-import contract is included. |
| Direct dynamic reassignment prevents the old imported target. | **Planned.** The exact legacy-alias example is deferred. Plain-prefix invalidation does not establish general alias ordering. |
| Both control-flow branches bind the same target. | **Planned.** Control-flow import joins are outside this slice. |
| Different branch targets leave the final call unresolved. | **Known-limited.** The existing last-wins alias behavior is not certified as a correct branch analysis. |
| One branch may leave the name unbound. | **Known-limited.** Reachability and control-flow binding analysis are outside this slice. |
| A call inside an importing branch can resolve to that branch's target. | **Planned.** Control-flow-local prefix production is outside this slice. |
| A deferred function body may use an import written later. | **Planned.** The exact legacy-alias and source-order example is deferred; final aggregation is not a general source-order claim. |
| An immediate expression before its import stays unresolved. | **Known-limited.** Per-expression source order is deferred. |
| An unreachable source use remains structural evidence. | **Planned.** The exact example is not certified by this change; structural evidence must not be read as execution evidence. |
| A loop may replace an imported name. | **Planned.** The exact legacy-alias example is deferred; the supported plain-prefix binder rule does not certify general alias replacement. |
| A called function's global side effect is not propagated. | **Known-limited.** The retained structural target can be stale at runtime because interprocedural side effects are not propagated. |

These limits are deliberate. The current slice does not add a per-expression
source-order engine, control-flow joins, new local/class/control-flow prefix
producers, interprocedural side-effect propagation, a second resolver, a
declaration-owner or declaration-kind precedence rule, or runtime dispatch.
It also makes no JavaScript-specific claim.

## Query consequences

A resolved `CALLS` edge supplies a caller and an inbound impact path. A
resolved `REFERENCES` edge is an independent structural use and is considered
by `unreferenced`, but the current inbound-impact traversal does not treat a
load-only owner as a caller. Unresolved text remains available for
recall-oriented caller matching without becoming a resolved use of a candidate
target. These meanings are tested through the public CLI and generated graph
documents in the query proofs above.
