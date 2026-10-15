# Interpreter edge-case catalog

This is the sole cross-language catalog for interpreter edge cases that have
been established by Minotaur's current static-analysis implementations and
their natural public tests. Case identifiers are stable, category-local, and
append-only. A new invariant receives a new identifier; existing identifiers
are never renamed or reused.

For the bounded SQL language boundary, see the [T-SQL analysis guide](../guides/analyze-sql.md).

## Status vocabulary

Every registered language has exactly one status for every case. The closed
vocabulary is:

- `SUPPORTED` — the documented behavior is implemented and proved.
- `PARTIAL` — the documented subset is implemented and proved, with the exact
  exclusion stated in the row.
- `UNSUPPORTED` — the language deliberately does not provide this behavior;
  the row records the conservative boundary and its proof.
- `NOT_APPLICABLE` — the case has no corresponding operation in this language;
  the row gives a reason and does not fabricate an example or proof.

## Cases

### EDGE-BIND-001 — Direct function-body import source positions

Question: When a direct named import appears in a function body, do calls and non-call loads after the import resolve at their own source positions?

#### Python — minotaur-python

Status: SUPPORTED
Example: `def caller(): from library import helper; helper(); value = helper`
Expected graph facts: Owner `app.caller` has `CALLS` and `REFERENCES` edges to `library.helper`; each edge keeps the expression source position, and the import statement remains an `IMPORTS` fact.
Owner: [`_ScopeCallVisitor.visit_ImportFrom`](../../src/minotaur/language_interpreter/python/interpreter.py); the direct import visitor installs the function-body route.
Marker: # EDGE-BIND-001: supports direct function-body named-import source-position resolution.
Proof: [`test_source_position_routes_prove_owner_location_and_syntactic_imports`](../../tests/language_interpreter/python/test_interpreter.py); the natural fixture reaches the public Python analyzer and asserts each resolved call/load source position, exact target, and syntactic import evidence.

#### JavaScript — minotaur-javascript

Status: NOT_APPLICABLE
Reason: JavaScript ESM imports are module-level syntax and have no equivalent conditional function-body import operation.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no corresponding function-body import operation.

### EDGE-BIND-002 — Agreeing direct conditional import joins

Question: When every direct `if` arm establishes the same import route, is that route available after the structural join?

#### Python — minotaur-python

Status: SUPPORTED
Example: `if flag:\n    from library import helper\nelse:\n    from library import helper\nhelper()`
Expected graph facts: The post-join `helper()` resolves to one exact `library.helper` target, while each import statement contributes its own `IMPORTS` evidence.
Owner: [`_join_flow_states`](../../src/minotaur/language_interpreter/python/interpreter.py); the structural join retains only agreeing routes.
Marker: # EDGE-BIND-002: supports agreeing conditional import routes after a flow join.
Proof: [`test_conditional_named_alias_and_relative_imports_join_exact_routes`](../../tests/language_interpreter/python/test_interpreter.py); the natural fixture asserts exact calls, loads, targets, and import evidence.

#### JavaScript — minotaur-javascript

Status: NOT_APPLICABLE
Reason: JavaScript ESM imports are module-level syntax and have no equivalent conditional import join operation.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no corresponding conditional import join operation.

### EDGE-BIND-003 — Divergent or omitted conditional import routes

Question: When conditional import arms disagree or an arm omits the import, does the analyzer retain one unresolved focal use instead of a stale edge?

#### Python — minotaur-python

Status: SUPPORTED
Example: `if flag:\n    from library import helper\nelse:\n    from other import helper\nhelper()`
Expected graph facts: The focal `helper` use is one `UNRESOLVED_REFERENCE` with its source text and no stale `CALLS` or `REFERENCES` edge to either candidate target.
Owner: [`_join_flow_states`](../../src/minotaur/language_interpreter/python/interpreter.py); the structural join conservatively excludes divergent or omitted routes.
Marker: # EDGE-BIND-003: supports conservative unresolved divergent or omitted conditional routes.
Proof: [`test_conditional_divergent_and_omitted_routes_are_one_unresolved_focal_use`](../../tests/language_interpreter/python/test_interpreter.py); the natural fixture asserts unresolved sites and excludes resolved candidate edges.

#### JavaScript — minotaur-javascript

Status: NOT_APPLICABLE
Reason: JavaScript ESM imports are module-level syntax and have no equivalent conditional binding operation.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no corresponding conditional binding operation.

### EDGE-BIND-004 — Eligible plain-dotted parent resolution

Question: Does an eligible plain-dotted import parent resolve a later call or load to the exact declaration without textual-prefix decoys?

#### Python — minotaur-python

Status: SUPPORTED
Example: `import pkg.sub; pkg.sub.child.go()`
Expected graph facts: The owner has `CALLS` and `REFERENCES` edges to the exact `pkg.sub.child.go` declaration, with no unresolved repeated-segment or same-prefix decoy edge.
Owner: [`_resolve_call`](../../src/minotaur/language_interpreter/python/interpreter.py); the plain-dotted resolver selects the exact qualified declaration target.
Marker: # EDGE-BIND-004: supports eligible plain-dotted imports resolving to exact declaration targets.
Proof: [`test_plain_dotted_imports_resolve_exact_call_and_load_without_decoys`](../../tests/language_interpreter/python/test_interpreter.py); the natural fixture asserts exact call/load target identity and negative decoy facts.

#### JavaScript — minotaur-javascript

Status: NOT_APPLICABLE
Reason: JavaScript uses relative named ESM imports rather than Python's plain-dotted parent import operation.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no corresponding plain-dotted import operation.

### EDGE-DECL-001 — Repeated direct function declarations

Question: When a language has repeated direct function declarations, does a later bare use resolve to the last direct binding while preserving lexical shadowing?

#### Python — minotaur-python

Status: SUPPORTED
Example: `def helper():\n    return 1\n\ndef helper():\n    return 2\n\nhelper()`
Expected graph facts: Both declaration nodes remain distinct, and the bare `helper()` call targets only the later declaration.
Owner: [`_declarations`](../../src/minotaur/language_interpreter/python/interpreter.py); direct declaration collection preserves identity while updating the final binding.
Marker: # EDGE-DECL-001: supports repeated direct declarations with last-binding bare-use resolution.
Proof: [`test_plain_shadowed_definition_body_and_containment_use_statement_identity`](../../tests/language_interpreter/python/test_interpreter.py); the natural fixture asserts repeated direct declarations retain distinct containment/body identities and that each bare use targets only its final binding.

#### JavaScript — minotaur-javascript

Status: SUPPORTED
Example: `function target() {}; function target() {}; target()`
Expected graph facts: Both declaration nodes remain distinct, and the bare `target()` call targets only the later declaration; a lexical parameter shadows the module binding.
Owner: [`_collect_declarations`](../../src/minotaur/language_interpreter/javascript/interpreter.py); direct declaration collection preserves repeated identities and the later binding.
Marker: # EDGE-DECL-001: supports repeated direct functions; last binding wins; lexical shadowing.
Proof: [`test_later_binding_wins_and_lexical_shadow_suppresses_use`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts declaration count, final target identity, and lexical suppression.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no corresponding direct function declaration operation.

### EDGE-DECL-002 — Conditional function declaration identity

Question: When a function declaration is conditional, does the analyzer exclude uncertain declaration identity and keep later use unresolved?

#### Python — minotaur-python

Status: UNSUPPORTED
Example: `if flag:\n    def choose():\n        return 1\nelse:\n    def choose():\n        return 2\nchoose()`
Expected graph facts: No `app.choose` declaration or containment is emitted, and the later `choose` use is one unresolved reference owned by the caller.
Owner: [`_declarations`](../../src/minotaur/language_interpreter/python/interpreter.py); conditional function declarations are excluded from emitted identity.
Marker: # EDGE-DECL-002: excludes conditional function declarations from emitted identity.
Proof: [`test_conditional_function_redefinitions_remain_unemitted_and_unresolved`](../../tests/language_interpreter/python/test_interpreter.py); the natural fixture asserts exact declaration absence, unresolved identity, and caller ownership.

#### JavaScript — minotaur-javascript

Status: UNSUPPORTED
Example: `if (flag) { function choose() {} } else { function choose() {} } choose()`
Expected graph facts: No `app.choose` declaration or containment is emitted, and the later `choose` use is one unresolved reference with no `CALLS` edge.
Owner: [`_collect_declarations`](../../src/minotaur/language_interpreter/javascript/interpreter.py); conditional function identity is deliberately excluded.
Marker: # EDGE-DECL-002: excludes conditional function identity; later uses remain unresolved.
Proof: [`test_conditional_function_redefinitions_remain_unemitted_and_unresolved`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts parser acceptance, declaration absence, unresolved identity, and caller ownership.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no corresponding conditional function declaration operation.

### EDGE-DECL-003 — Same-name accessor and overload declarations carry a declaration role

Question: When several definitions legitimately share one qualified name as property accessors or overload declarations, does each emitted declaration carry a declaration role that tells it apart without changing its node identity?

#### Python — minotaur-python

Status: PARTIAL
Example: `from typing import overload\n\nclass Gauge:\n    @property\n    def level(self): ...\n    @level.setter\n    def level(self, value): ...\n    @overload\n    def convert(self, value: int) -> int: ...\n    def convert(self, value): ...`
Expected graph facts: Both `app.Gauge.level` methods are emitted with extensions `{"minotaur-python": {"declaration_role": "property"}}` and `{"minotaur-python": {"declaration_role": "setter"}}`; `getter`, `deleter` and `overload` (including the `typing.overload`, `t.overload` and `typing_extensions.overload` spellings and `async` stubs) are recorded the same way, while implementations, plain methods, classes and other decorators carry no extensions, and node IDs, labels, symbol kinds and locations are identical to analysis without roles. Exclusion: recognition is syntactic on the decorator's final name, so an aliased decorator name such as `from typing import overload as ov` with `@ov` records no role.
Owner: [`_declaration_role`](../../src/minotaur/language_interpreter/python/interpreter.py); the first recognised decorator, scanned top to bottom, decides the role.
Marker: # EDGE-DECL-003: supports syntactic property, accessor and overload declaration roles.
Proof: [`test_decorated_definitions_record_syntactic_declaration_roles`](../../tests/language_interpreter/python/test_interpreter.py); the natural fixture asserts the exact role or absence of extensions for every symbol, the `@ov` exclusion, and identity unchanged against a roleless analysis.

#### JavaScript — minotaur-javascript

Status: PARTIAL
Example: `export class Gauge {\n  get level() { return this._level; }\n  set level(value) { this._level = value; }\n  reset() {}\n}`
Expected graph facts: The two `lib.Gauge.level` methods are emitted with extensions `{"minotaur-javascript": {"declaration_role": "get"}}` and `{"minotaur-javascript": {"declaration_role": "set"}}`, so whole-graph correspondence indexes each under its own key; constructors and ordinary methods carry no extensions, and object-literal accessors emit no declaration. Exclusion: `static` is not encoded in the role, so a `static get v` beside an instance `get v` both record role `get` and remain an ambiguous correspondence.
Owner: [`_collect_declarations`](../../src/minotaur/language_interpreter/javascript/interpreter.py); class `MethodDefinition` members of kind `get` or `set` record that kind as the role.
Marker: # EDGE-DECL-003: supports class get/set accessor roles; static is not encoded.
Proof: [`test_class_accessors_record_declaration_roles_for_correspondence`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts exact accessor roles, distinct correspondence keys for the get/set pair, and refusal for twin and static-beside-instance accessors.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no property accessor or overload declarations that share one qualified name.

### EDGE-EXPR-001 — Naming identifiers that emit no reference facts

Question: When an identifier appears only as a label, a class-expression name, an object-property key, `this`, or a `MetaProperty`, does it stay out of the reference facts?

#### Python — minotaur-python

Status: NOT_APPLICABLE
Reason: Python has no labeled statement, class-expression, `this`, or `MetaProperty` identifier form; the corresponding names are declarations or attribute keys rather than a distinct naming-only syntax.

#### JavaScript — minotaur-javascript

Status: SUPPORTED
Example: `function main() { const View = class { render() {} }; outer: for (const item of items) { break outer; } const marker = new.target; }` beside `function render() {}`, `function outer() {}`, and `function target() {}`.
Expected graph facts: No `references` or `calls` edge from `a.main` targets `a.render`, `a.outer`, or `a.target`, and the `new.target` form emits no `new` or `target` fact; `import.meta` is equally silent, so the module analyzes beside a valid sibling with both file nodes present.
Owner: [`_walk`](../../src/minotaur/language_interpreter/javascript/interpreter.py); naming identifier positions are never walked as expression uses.
Marker: # EDGE-EXPR-001: supports non-use identifiers emitting no reference edges.
Proof: [`test_non_use_identifier_forms_emit_no_reference_edges`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts parser acceptance for `import.meta`, absence of every label, class-expression-name, and `MetaProperty` reference, and the `new.target` negative facts.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no labeled statement, class expression, property key, or `MetaProperty` operation.

### EDGE-EXPR-002 — Anonymous default-export bodies owned by the module

Question: When an anonymous default export carries a function body, an arrow body, or a bare expression, are its uses owned by the module symbol without inventing a declaration?

#### Python — minotaur-python

Status: NOT_APPLICABLE
Reason: Python has no default-export operation; a module-level name is always an ordinary declaration.

#### JavaScript — minotaur-javascript

Status: SUPPORTED
Example: `import { helper } from './lib.js';` then `export default function () { helper(); }`, `export default () => missingInDefault();`, and `export default helper;`.
Expected graph facts: The anonymous function body emits `calls app -> lib.helper`, the arrow body emits `references app -> ?missingInDefault`, and the bare `export default helper;` emits `references app -> lib.helper`; every fact is owned by the `app` module symbol and no declaration symbol is invented for the anonymous forms.
Owner: [`_expressions`](../../src/minotaur/language_interpreter/javascript/interpreter.py); an anonymous default export's declaration is walked as an expression position under the module symbol.
Marker: # EDGE-EXPR-002: supports anonymous default-export bodies owned by the module symbol.
Proof: [`test_anonymous_default_exports_walk_bodies_with_module_owner`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts the resolved call, the unresolved arrow reference, the bare-expression reference, and module ownership of every fact.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no module export operation.

### EDGE-EXPR-003 — Declaration and catch pattern defaults through the shared walker

Question: When a declaration, loop-header, or catch pattern carries a default initializer or a computed key, does the shared pattern walker still execute its expressions?

#### Python — minotaur-python

Status: NOT_APPLICABLE
Reason: Python assignment targets have no default-value or computed-key pattern syntax; its analogous parameter defaults are a different operation.

#### JavaScript — minotaur-javascript

Status: SUPPORTED
Example: `const { a = topHelper() } = obj;` beside `for (const { c = loopHelper() } of items) {}` and `try {} catch ({ message = catchTarget() }) {}`, with `const { [key]: v } = obj;` for the computed key.
Expected graph facts: Each default initializer emits `calls` from the nearest emitted owner to its helper (`app` for the top-level, loop-header, and caught patterns, the containing function for a nested declaration), and a computed pattern key emits `references app -> app.key` while the bound name itself emits no fact.
Owner: [`_walk_parameter_defaults`](../../src/minotaur/language_interpreter/javascript/interpreter.py); declaration, loop-header, catch, and parameter patterns share one executable-default walker.
Marker: # EDGE-EXPR-003: supports declaration and catch pattern defaults through the shared walker.
Proof: [`test_declaration_and_catch_pattern_defaults_emit_calls`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts each default's owner and target call plus the computed-key reference and the bound-name absence.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no declaration, loop-header, or catch binding-pattern operation.

### EDGE-EXPR-004 — Unsupported import locals and `new` callees as references

Question: When an unsupported import local or a `new` callee is used, does the ordinary lookup record a reference instead of suppressing it or emitting a call?

#### Python — minotaur-python

Status: NOT_APPLICABLE
Reason: Python imports always bind a name, so there is no unsupported-import local, and Python has no `new` callee operation.

#### JavaScript — minotaur-javascript

Status: SUPPORTED
Example: `import * as utils from './lib.js'; utils.fmt();` beside `function Widget() {} new Widget();` and `new unres();`.
Expected graph facts: The namespace-import local emits unresolved references `utils` and `utils.fmt` beside the `./lib.js#*` import fact; `new Widget()` emits `references app -> app.Widget` and `new unres()` emits an unresolved `unres` reference, and neither `new` form emits a `calls` relationship.
Owner: [`_walk`](../../src/minotaur/language_interpreter/javascript/interpreter.py); identifier use falls through the ordinary lookup, and a `new` callee is classified as an expression.
Marker: # EDGE-EXPR-004: supports unsupported import locals and new callees as references.
Proof: [`test_unsupported_import_locals_and_new_callees_emit_references`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts the exact unresolved texts, the resolved and unresolved `new` references, and the absence of `calls` edges.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no import local or constructor-call operation.

### EDGE-EXPR-005 — Computed method keys without a key-text method label

Question: When a class method key is computed, does the analyzer walk the key expression without emitting a method symbol named for its identifier text?

#### Python — minotaur-python

Status: NOT_APPLICABLE
Reason: Python class bodies declare methods with fixed `def` names and have no computed method key syntax.

#### JavaScript — minotaur-javascript

Status: SUPPORTED
Example: `function pick() {} function helper() {} class Box { [pick()]() { helper(); } }` beside the class-expression form `function main() { const Box = class { [pick()]() { helper(); } }; }`.
Expected graph facts: The computed key emits `calls a.Box -> a.pick` (or `calls a.main -> a.pick` for the class expression) and the method body emits `calls` to `a.helper` under the nearest emitted owner; no `class` or `method` symbol is emitted for the computed-key form, and no method is labelled with the key's identifier text.
Owner: [`_collect_declarations`](../../src/minotaur/language_interpreter/javascript/interpreter.py); a computed class member is skipped as a named method so its key and body are walked under the enclosing owner.
Marker: # EDGE-EXPR-005: supports computed method keys without a key-text method label.
Proof: [`test_computed_method_keys_walk_without_method_label`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts the class-owned and enclosing-owner calls, the nested and class-expression forms, and the absence of method symbols.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no class or method definition operation.

### EDGE-EXPR-006 — Class field and static-field exclusion for both class forms

Question: Do class field and static-field definitions produce key or initializer facts for either class form?

#### Python — minotaur-python

Status: NOT_APPLICABLE
Reason: Python class fields are annotated assignments or `__init__` attributes and have no class-expression form.

#### JavaScript — minotaur-javascript

Status: SUPPORTED
Example: `function helper() {} const View = class { field = helper(); static s = helper(); };`
Expected graph facts: Neither the instance field nor the static field emits a key or initializer fact: no `calls` from `helper()` and no `field` or `s` reference; the class-declaration regression pin `class View { field = helper(); }` emits neither, while a non-computed method body `m() { helper(); }` still emits `calls` under the enclosing owner.
Owner: [`_walk`](../../src/minotaur/language_interpreter/javascript/interpreter.py); the class-declaration member loop skips field and static-field definitions, and a class expression's members reach their empty `FieldDefinition` and `PropertyDefinition` dispatch-table entries, so neither class form walks a field's key or initializer.
Marker: # EDGE-EXPR-006: supports class field and static-field exclusion for both class forms.
Proof: [`test_class_expression_fields_and_static_fields_emit_no_facts`](../../tests/language_interpreter/javascript/test_javascript_interpreter.py); the natural fixture asserts the initializer and key absence for the class expression, the class-declaration pin, and the still-executing method body.

#### SQL — minotaur-sql

Status: NOT_APPLICABLE
Reason: The bounded SQL slice has no class or field definition operation.
