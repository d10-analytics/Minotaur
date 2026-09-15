# Interpreter edge-case catalog

This is the sole cross-language catalog for interpreter edge cases that have
been established by Minotaur's current static-analysis implementations and
their natural public tests. Case identifiers are stable, category-local, and
append-only. A new invariant receives a new identifier; existing identifiers
are never renamed or reused.

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
