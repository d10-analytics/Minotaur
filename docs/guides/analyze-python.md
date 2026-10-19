# Current Python analysis

Minotaur provides a bounded analyzer for Python source structure. The
language-neutral CLI selects files by registered extension. This page covers
the Python registration for `.py` files; the separate JavaScript guide covers
the pure `.js` selection boundary.

The graph meanings and supported Python binding boundaries are collected in
the [structural analysis contract](../concepts/structural-analysis-contract.md).
Use that contract when interpreting an import, call, reference, or unresolved
relationship; this guide focuses on running the analysis and the Python
module and callable binding behavior.

For the cross-language case matrix and exact source/proof links, see the
[interpreter edge-case catalog](../concepts/interpreter-edge-cases.md).

Analysis records source bytes and the selected targets for later freshness
checks. See [Graph freshness and snapshot order](../concepts/freshness.md) for
the exact refresh, no-refresh, clean-skip, and graph-integrity contract.

## Analyze selected paths

Run the same command through the installed console script or as a module. If
`minotaur` reports `ModuleNotFoundError: No module named 'orjson'`, your
editable install predates `orjson` becoming a required dependency; re-run
`pip install -e ".[dev]"` to pick it up.

```text
minotaur analyze --root ROOT --output GRAPH.json [--force] TARGET [TARGET ...]
python -m minotaur analyze --root ROOT --output GRAPH.json [--force] TARGET [TARGET ...]
```

`ROOT` must exist, and it determines module names: a file is named by its path
relative to the root, so `src/pkg/mod.py` becomes `src.pkg.mod` under
`--root .` but `pkg.mod` under `--root src`. Choose the directory that imports
are resolved from (the directory you would put on `PYTHONPATH`, usually `src`
for a `src/` layout). With the wrong root, `import pkg.mod` cannot be matched
to `src.pkg.mod`, every cross-module call becomes an unresolved reference, and
queries such as `callers` and `impact` silently lose most of their answers.
When at least 5% of the imports in a selection would resolve under a different
root, `analyze` prints a warning naming that root:

```text
minotaur: warning: 52% of imports (161 of 310) only resolve with a different root; pass --root /repo/src so module names match import names
```

Third-party imports and imports of files outside the selection are unresolved
too, but they do not trigger this warning.

Targets are resolved from the current working directory, like most command-line
tools, not from `--root`: run `minotaur analyze --root src src/minotaur` from
the repository root, not `--root src minotaur`. Every target must be an
existing file or directory inside the root after symlinks are resolved. Directory targets are scanned
recursively for registered extensions; unsupported files found during that
scan are ignored, while an explicitly named unsupported file is an error.
Repeated and overlapping targets are analyzed once.

Normal recursive scans exclude hidden directories, caches, and virtual
environments. Explicitly selecting such a file or directory includes it.
The output parent directory must already exist; for a symbolic-link output,
the directory the link resolves into must also exist. An output path may never
also be a selected source file. An existing graph can be reused when its
recorded selection and analyzed source content are clean, its analyzer semantics
version is current, and it records no errors: Minotaur prints
`graph is up to date, skipping analysis` and leaves the file unchanged. Use
`--force` to analyze and rewrite an existing graph regardless of freshness.
A missing or different analyzer version on a graph with a recorded, non-empty
selection causes re-analysis and prints
`minotaur: analyzer version changed (<old> -> <new>), refreshing` to stderr
(`none` means no recorded version), before re-analysis. It does not
print the up-to-date skip message.
Recorded errors cause re-analysis and current diagnostic reporting instead of
reuse. With an explicit `--output`, an existing loadable graph with a different
or missing recorded selection, or with every recorded file missing under the
current root, is refused with exit `2` and `pass --force to replace it`.
Partial deletion or content drift with the same selection still refreshes.
Config- and scope-derived outputs reconcile changed selections without
`--force`, after validating the current root and targets. A Git commit or
branch change alone
does not cause re-analysis when selected content is unchanged, so committed
graph bytes remain stable; `source_control` records the commit and branch at
which the graph was last actually generated, as provenance rather than a
freshness gate.

The command writes canonical JSON atomically. It exits `0` on a clean graph,
`1` after writing a valid partial graph with parse, source-read, or
`interpreter-error` diagnostics, and `2` for argument, selection, dispatch, or
output-preflight errors (with no output written). An analysis failure that no
single selected file explains is also a dispatch error: it exits `2`, writes
no graph or sidecar, and leaves an existing graph and sidecar unchanged (see
[Diagnostics and unresolved references](#diagnostics-and-unresolved-references)).

## Structural facts it records

For readable, syntactically valid Python files in the selected workspace, the
analyzer records file and module nodes plus top-level class and function nodes.
It also records method nodes for methods declared directly in those classes.

The resulting graph records containment between these declarations, resolvable
imports within the workspace, and direct calls whose static targets it can
resolve. Calls and resolvable non-call name loads found inside nested functions,
lambdas, and comprehensions are attributed to the enclosing top-level function,
method, or module. A class body executes at definition time, so calls and loads
in its non-method statements — a dataclass `field(default_factory=make_config)`,
`handler = staticmethod(helper)`, a class-level signal or callback table — are
attributed to the class itself; methods keep their own scope. A definition's
decorators, default arguments, and annotations are evaluated outside its body.
The decorator expressions themselves are attributed to the definition they
belong to, while each decorator also records a reference from the enclosing
module or class to the decorated symbol. This applies equally to decorated
functions, methods, and classes. Decoration therefore counts as a use: a
never-called symbol wrapped by a decorator is not reported by
`query unreferenced`. Annotations count because `def f(x: Handler)` is a real
dependency on `Handler`: an agent asking whether a symbol is still used must
see it before deleting that symbol. Class headers are treated the same way: the
decorators, bases, and keywords of `class Sub(Base, metaclass=Meta)` are
references from the class (or, for a nested class, from the enclosing scope),
so a base class used only through subclassing is not reported as unreferenced.
Nested function definitions do not become separate symbol nodes. Methods of
nested classes likewise do not become separate nodes: their bodies are analyzed
with the same scope rules and their facts are attributed to the nearest
enclosing emitted symbol. Nested class declarations remain outside this slice.

Repeated direct declarations with the same name in one scope remain separate
nodes, and each node receives its own header and body relationships. Name-based
calls, loads, and imports resolve to the last definition of that name.

A module-level function or a method declared directly in a class, including an
`async def`, also records a declaration role when its decorators name one, so
same-named declarations stay distinguishable when `query diff --systems` pairs
two revisions. Recognition is syntactic on each decorator's final name:

- `@property` or `@<expr>.property` records `property`;
- `@<expr>.getter`, `@<expr>.setter`, and `@<expr>.deleter` record `getter`,
  `setter`, and `deleter`;
- `@overload`, `@typing.overload`, `@typing_extensions.overload`, or any other
  `@<module>.overload` records `overload`.

Decorators are scanned top to bottom and the first recognised one decides the
role. Classes never carry a role, and neither do definitions whose decorators
are all unrecognised, such as `@functools.cached_property` or a called
decorator. An overload implementation has no `overload` decorator and so no
role. Recognition does not follow import aliases: after
`from typing import overload as ov`, an `@ov` stub records no role, so those
stubs keep sharing one identity with their implementation and `diff --systems`
refuses them as ambiguous (exit `2`) rather than guessing. The role is stored
as `extensions["minotaur-python"]["declaration_role"]` and does not change node
IDs, labels, or symbol kinds.

The interpreter also records resolvable non-call references as `references`
relationships. For example, passing a function as `register(handler)` or
accessing `button.clicked.connect(self.on_click)` records the resolved target
and the load's source location. The function of a `Call` is represented by the
`calls` relationship instead of an extra `references` relationship. A call and
a non-call load of the same expression state the same thing about a name and
are reported identically: `self.on_click` and `self.on_click()` are either both
resolved or both unresolved. The callable expression of a `Call` does not
produce a duplicate non-call reference, while loads in its arguments and
subexpressions remain eligible.

A member expression that resolves is exactly one relationship, so a resolved
`self.method` does not also report bare `self`. A member expression that does
not resolve contributes at most one `unresolved-reference` node, labelled with
the expression's full text rather than with its base: `obj.a.b.c.d` is one
unresolved node called `obj.a.b.c.d`, not one node per prefix. When the
expression itself does not resolve but the root of a plain name-and-attribute
chain does, that root is also recorded as a resolved `references` relationship;
`from lib import Cfg` followed by `Cfg.DEFAULT` therefore records both a
reference to `lib.Cfg` and an unresolved `Cfg.DEFAULT`. When the chain instead
passes through a call or a subscript, the descent records how the root was
actually used and the outer expression remains one unresolved fact:
`lib.helper().c.d` records a `calls` relationship to `lib.helper` and an
unresolved `lib.helper().c.d`.

For a non-atomic member base, the unresolved label uses the full AST expression
text, preserving the expression it names: `(left + right).denominator` is not
labelled `left + right.denominator`, and `items[0].name` remains
`items[0].name`.

Names bound as ordinary dynamic locals in the lexical scope that owns the load
(including parameters, assignments, loop and context-manager targets, match
captures, and comprehension targets) are suppressed in function and method
bodies. A walrus target binds the enclosing function. A direct import is a
different category: `from lib import helper`, `from .lib import helper`,
`import pkg.sub`, and their aliases are reportable and resolve calls and loads
after the import at that source position. Before the import, or after a
managed imported name is assigned or deleted, the use is one unresolved site;
it does not fall through to an outer route. A later direct reimport restores its
route. `global` names remain eligible for module routes and `nonlocal` names
are bound to their enclosing lexical scope.

References to Python builtin names are suppressed. The builtin set is
`dir(builtins)` taken at analysis time; the rationale is recorded at the point
of use in the Python interpreter module. Suppression is decided on the root of
the expression, so `str.foo(x)` records nothing, while `super().run()` keeps
its single unresolved fact because its root is a nested call rather than a
name. An imported name is a workspace dependency and escapes builtin
suppression, so `from externallib import list` followed by `list()` is
reported. The trade-off is recall: a workspace symbol named like a builtin that
is called without being imported (`def filter(...)` in one module, `filter(seq)`
in another) produces no unresolved node, so `query callers` for that symbol
reports no callers.

A receiver-shaped first parameter is treated as a receiver even where it cannot
resolve through a class. `self` and `cls` resolve member expressions through the
owning class, including in `__new__`, `__init_subclass__`, and
`__class_getitem__`, which take `cls` implicitly. Where the parameter cannot
resolve through a class — a metaclass `__call__(cls, ...)`, or a `@staticmethod`
taking a `self` parameter — the member expression is still reported as
unresolved rather than dropped, while the bare parameter itself (`self`,
`cls()`) is an ordinary local and is suppressed.

Import, call, and reference relationships include source-location evidence so
a consumer can identify the site that established the relationship.

### Module-scope dotted imports

At module scope, and after the same direct import in a function, a plain import
such as `import pkg.sub` can admit component-bounded descendants. A later
`pkg.sub.child.go()` use appends the remaining components once and asks the
existing qualified-declaration lookup for the exact target ID; a separate
`import pkg.sub.child` is not required.
`pkg.other` and `pkg.submarine` are outside the `pkg.sub` component boundary,
and an overlapping eligible prefix uses the longest component match. The
import statement still emits its own `IMPORTS` fact: it never implies a
`CALLS` or `REFERENCES` edge to every member.

The resolver aggregates direct module bindings while expression resolution uses
the source-position state. Competing module binders conservatively invalidate
every use of an affected plain prefix. A later true alias can retain its real
route, while a later plain dotted import cannot preserve an earlier corrupted
alias route. Direct function-body imports install a callable-local route;
Class-body plain imports retain conservative behavior: they may refine an
already-visible plain package route, while a class-only route remains syntactic
evidence. A direct `if` suite is the supported conditional container described
below. Imports in loops, `try`, `with`, and `match` suites remain syntactic
evidence and do not establish a route for their contained or later
continuation uses.

### Conditional binding joins

Direct imports in the current `if` suite participate in a bounded structural
join. Each explicit arm is analyzed with an isolated post-condition state; an
omitted `else` contributes the incoming state. Return and raise arms still
contribute their binding data, so a frozen tail cannot invent a route. If every
continuing arm establishes the same canonical root, route category, and
qualified prefix, the common route is available after the `if`. Missing or
differing roots and prefixes remain uncertain, and a later direct reimport
restores only the route it actually establishes. The import statements retain
independent `IMPORTS` evidence. A conditional-only replacement of a module root
also remains unresolved; it does not fall through to an outer same-module
declaration.

This behavior applies to direct named, aliased, relative-named, and plain
dotted imports and to calls and non-call loads at their source positions. A
direct `if` nested inside a loop can resolve an ordered pre-write use, while
the enclosing loop's continuation remains conservative. The same limit applies
to `try`, `with`, and `match`; this slice does not infer runtime reachability,
simulate invocation, or provide a general control-flow graph. See the
[conditional proof rows](../concepts/structural-analysis-contract.md#supported-behavior-and-proof)
for the exact public assertions and query consequences.

The lookup preserves distinct node IDs for same-labelled declarations. For
the natural collision cases, a lowercase `child` selects the function at
`pkg/sub/child.py`, graph `Range(0, 0)-(0, 14)`, while uppercase `Child`
selects the package method at `pkg/sub/__init__.py`, graph
`Range(1, 4)-(1, 22)`; a class-only package method has that latter location.
Those are current qualified-lookup outcomes, not owner or declaration-kind
precedence rules. Graph ranges are zero-based. Query output uses one-based
line and column values, such as `caller.py:3:12` for the caller proof.

These claims are proved by the named natural tests in the [structural
analysis contract](../concepts/structural-analysis-contract.md#supported-behavior-and-proof).
The slice does not provide general control-flow analysis, control-flow-local
import production in non-`if` compounds, invocation timing, interprocedural
callable side effects, or runtime dispatch; unresolved cases remain explicit.

### Binding timing and class boundaries

Module and class execution use source order. Immediate calls and loads see the
route active at their own expression. Function and method bodies are deferred
and use final module state; nested deferred functions, lambdas, and methods use
the final state of their owning enclosing function. Defaults, decorators,
unquoted annotations, and class headers execute in their enclosing scope at
definition time; a quoted annotation is evaluated later, as described under
[declared names and annotations](#declared-names-annotations-and-header-uses).
A lambda body is deferred, but its defaults and parameters are immediate or
lexical. This policy does not simulate invocation or callable side effects.

Class headers run in the enclosing scope, while the class body has a separate
sequential namespace. Earlier class assignments or imports can affect later
headers, and deletion can reveal an enclosing route. Ordinary class locals and
class imports are hidden from method bodies; methods use their own imports and
enclosing function or module routes. Existing class behavior may refine an
already-visible plain package route, while a class-only plain import remains
syntactic evidence. The analyzer does not provide direct class-import support.

Assignment effects follow expression order: ordinary assignment reads its
right-hand side before writing targets, augmented assignment reads its target
before its right-hand side and write, and a walrus commits after its value
before later siblings. Attribute and subscript stores preserve their base load;
annotation-only statements do not invent a binding. Structural sites after a
direct `return` or `raise` remain visible, but unreachable code cannot advance
binding state.

### Compound statements and comprehensions

Direct imports in an `if` body participate in the bounded structural join above.
Imports in loops, `try`, `with`, and `match` bodies remain syntactic `IMPORTS`
facts and do not establish a control-flow-local route for their continuation.
An already-established route remains available to an ordered body read until a
direct write changes it; a possibly changed root is uncertain at continuation,
while untouched routes retain their state. On a successful `try` path, `else`
sees the body state, and a handler type is read before its `as` target. A
match-guard write affects later guards and continuation. Direct imports in a
function body outside these containers remain supported.

The first iterable of a comprehension is evaluated in the enclosing scope;
later iterables, filters, and result expressions see comprehension-local
targets. List, set, and dict comprehension bodies are eager and a walrus can
update the enclosing callable overlay. A generator expression body is lazy and
does not update that overlay; the analyzer does not simulate generator
invocation.

### Declared names, annotations, and header uses

A top-level `def` or `class` name is invalidated by a later module-level
rebinding exactly as an imported name is: an assignment, a conditional
assignment, `del`, a `try` import, or a star import (`from m import *`) makes
every later use an unresolved reference rather than a `calls` edge to the
original definition. For example, after `def rebound(): ...` and `rebound =
other`, a call `rebound()` is unresolved and does not redirect to `app.other` (proof:
`test_assigning_over_a_declared_function_does_not_redirect_to_another_declaration`).
A star import marks every tracked name uncertain from that point (declared
names, assigned locals, and import roots); builtins and never-bound names are
untouched. A call written before the rebinding keeps its edge, and a function
body follows the module's final state (proof:
`test_module_call_before_rebinding_keeps_edge_and_function_body_follows_final_state`).
A repeated direct `def` still resolves to the last definition, and a
conditional `def` stays unemitted (proofs:
`test_plain_shadowed_definition_body_and_containment_use_statement_identity`
and `test_conditional_function_redefinitions_remain_unemitted_and_unresolved`).
A later top-level definition of a rebound name re-establishes it (proof:
`test_second_top_level_definition_reestablishes_a_rebound_name`).
Because such a declaration then has no resolved use, it appears in graph
`unreferenced` output and as `[text-mention]` under `--text-fallback`.
Proofs, in `tests/language_interpreter/python/test_interpreter.py`: `test_rebinding_a_declared_name_makes_every_later_use_unresolved`,
`test_conditional_rebinding_invalidates_a_declared_name_after_the_join`,
`test_conditional_definition_of_a_declared_name_makes_later_uses_unresolved`,
`test_star_import_invalidates_declared_and_imported_names_but_not_builtins`,
and `test_star_import_inside_a_conditional_invalidates_a_declared_name_after_the_join`;
in `tests/query/test_unreferenced.py`,
`test_unreferenced_lists_declarations_invalidated_by_a_later_rebinding`.

`from pkg.utils import foo` no longer creates an alias for the module name
`utils`. The removed shorthand never produced a correct resolution. It could
resolve an unbound call such as `lib.helper()` after `from lib import helper`
only when the call's head text exactly matched the imported module's name,
yielding a binding Python would not create (`lib` is unbound, so Python raises
`NameError`); and it could hide a same-module `def utils()`, binding calls to
an incorrect target. A same-module definition now resolves normally, and the
`--text-fallback` path is unchanged. Proofs:
`test_from_import_does_not_alias_its_module_name_over_a_same_module_definition`
and `test_from_import_does_not_make_its_module_name_a_prefix_root` in `tests/language_interpreter/python/test_interpreter.py`;
`test_unreferenced_does_not_list_a_function_named_like_a_from_imported_module`
in `tests/query/test_unreferenced.py`.

In a class or module body, `name: T` with no value does not bind `name`, so a
later use resolves against the enclosing scopes (or is unresolved) and is never
silently dropped. In a function body the same statement makes `name` local, as
in Python, so a call to it stays unemitted. Proofs:
`test_class_body_annotation_without_value_does_not_bind_the_name`,
`test_module_annotation_without_value_keeps_the_imported_name`, and
`test_function_body_annotation_without_value_makes_the_name_local`.

A string annotation (`-> "list[Other]"`, `x: "Handler"`) is parsed as an
expression and contributes `references` or unresolved references, located at
the string literal's span. Python evaluates a quoted annotation later, against
the module's final bindings, so its name resolves only when the binding at the
annotation's position is also the module's final binding. When a later import,
assignment, or top-level definition changes that binding, the quoted use is an
unresolved reference rather than an edge to the earlier binding. An unquoted
annotation runs at definition time and still resolves at its source position,
so the two forms record the same facts whenever the name is not rebound later.
An unparsable string, or one containing binding or scope syntax (`:=`, lambda,
comprehension, `yield`, `await`), is ignored silently, as are `Literal[...]` arguments and `Annotated[...]` metadata, which
are values rather than type uses. Proofs: `test_quoted_return_annotation_references_its_target`,
`test_quoted_annotation_records_the_same_facts_as_the_unquoted_form`,
`test_string_annotation_location_lies_inside_the_literal`,
`test_unparsable_or_binding_string_annotations_emit_nothing`,
`test_literal_arguments_are_values_not_type_uses`, and
`test_annotated_metadata_is_a_value_and_only_the_wrapped_type_is_a_use`;
for late evaluation,
`test_quoted_annotation_does_not_use_an_import_the_module_later_rebinds`,
`test_quoted_annotation_does_not_use_an_import_a_later_class_replaces`, and
`test_quoted_annotation_keeps_an_import_the_module_never_rebinds`.

A call and a load of the same name in a nested-class header, a nested-class
body, a nested-class method body, or a class defined inside a function resolve
identically. Only uncertainty introduced by an enclosing class body is
ignored, while uncertainty from the module flow state or the owning function
is kept. A class body inside a function consults that function's imports
before the module's: after module `from lib import helper`, a `from other
import helper` inside `def outer()` makes every use of `helper` in a nested
`class K` resolve to `other.helper`. Proofs:
`test_invalidated_declared_name_stays_unresolved_in_every_nested_use`,
`test_imported_name_resolves_in_every_nested_use`,
`test_enclosing_class_import_does_not_reach_nested_class_uses`,
`test_function_local_import_governs_every_use_in_a_function_nested_class`, and
`test_calls_and_loads_resolve_through_the_one_header_helper`. An import
written in a class's own body is still not a route for later uses in that
same body (see [binding timing and class boundaries](#binding-timing-and-class-boundaries)):
in a module-level class those uses stay unresolved, but in a class defined
inside a function they can resolve to the function or module binding that
the class import shadows.

### Query consequences

A resolved `CALLS` relationship appears in `query callers` and inbound
`query impact`. A resolved `REFERENCES` relationship counts as a use for
`query unreferenced`, but does not create a caller or inbound impact path.
Unresolved text is recall-only for caller matching and is not a resolved use of
a candidate target. The exact public proofs and their one-based output are
listed in the [structural analysis contract](../concepts/structural-analysis-contract.md#supported-behavior-and-proof).

The output uses the canonical Minotaur wire contract described in the
[Minotaur graph format reference](../formats/minotaur-graph-v1.md).

For freshness checks, each file node carries the lowercase SHA-256 digest of
its exact source bytes in its registered producer extension;
Python uses `extensions["minotaur-python"]["content_sha256"]`. The analyze command also
records its sorted root-relative input targets in the document extension
`extensions["minotaur"]["selection"]`. The analyzer records import resolution
counts in the document extension `extensions["minotaur-python"]`:
`imports_resolved`, `imports_unresolved`, `imports_root_mismatched` (imports
that would resolve under a different root), and, when mismatches exist,
`import_root_hint` (the root-relative directory that would resolve them). These extensions are metadata and do
not affect node identity or the graph format version. When the root is inside
a Git work tree, the document may also contain the commit and branch of the
last real generation in `source_control`; this is provenance and never a
freshness substitute. Per-file content digests are the freshness authority,
so the stamp may lag `HEAD`.

After writing the graph, `analyze` also writes a sidecar digest file beside it
(see the [format reference](../formats/minotaur-graph-v1.md#sidecar-digest-file)).
A pre-sidecar or foreign graph is fully validated and stamped by the first
user-facing graph-reading command (`query`, `diff`, or `visualize`), so later
reads are fast. An `analyze` clean-skip deliberately does not create a
sidecar. No manual migration or conversion step is needed.

`scripts/benchmark_graph_load.py --graph GRAPH.json --root ROOT [--repeats N]
[--verbose]` measures `analyze`, `query definitions --no-refresh`, the
in-process loading and query-index components, and `serialize` against one
graph, reporting median (and, with `--verbose`, min/max) wall-clock time per
step. It never modifies the `--graph` path: `analyze` and every other timed
step run against a graph in a unique, invocation-owned temporary directory
that is removed before the script exits.

## Diagnostics and unresolved references

If a source file cannot be read or parsed, the analyzer reports a diagnostic
for that file and continues with the other files in the workspace. The failed
file contributes no structural facts.

If the analyzer itself fails unexpectedly, Minotaur analyzes each selected
Python file on its own to find the files that cause the failure. Each such
file is reported as one error-severity `interpreter-error` diagnostic with no
location, whose message names the exception class and its text, for example
`deep.py: interpreter-error: RecursionError: maximum recursion depth exceeded
[severity=error]`. The code reports a Minotaur limitation, not a syntax
problem, so the file may be valid Python. That file contributes no facts; the
remaining files are analyzed together, `analyze` writes the partial graph and
exits `1`. A refreshing `query` re-analyzes the same way, answers from the
rewritten graph, and exits `1` without printing the diagnostic. When no single
selected file explains the failure, or the remaining files still fail
together, the command instead prints one `minotaur: error: analysis failed in a
way no single selected file explains: …` line, exits `2`, and writes no graph.

Python files are decoded the way Python decodes them when it compiles or
imports them, except that only ASCII-compatible text codecs are honoured. A
PEP 263 encoding declaration on one of the first two lines, such as
`# -*- coding: latin-1 -*-`, selects the codec, even when those lines also
hold bytes that are not UTF-8. Otherwise a UTF-8 byte-order mark selects
UTF-8, and a file with neither is read as UTF-8. An ASCII-compatible text
codec is one that decodes the 128 ASCII bytes to the same characters, such as
`latin-1`, `cp1252`, or `shift_jis`; codecs that Python accepts but that do
not decode ASCII text unchanged, such as `utf-7` or `shift_jis_2004`, are not
honoured. Recorded positions are always UTF-8 offsets into the decoded text,
whatever the file's encoding: in a `latin-1` file, the call in `x = "é"; f()`
starts at character 10, the UTF-8 length of `x = "é"; `, rather than at its
byte offset 9 in the file. A
`parse-error` diagnostic follows the same rule: its column is the UTF-8 length
of the decoded line text before the parser's error position, so `x = "ééééé" + )`
reports column 19, not the parser's character offset (proof:
`test_parse_error_column_is_a_utf8_byte_offset_of_the_decoded_line` in
`tests/language_interpreter/python/test_interpreter.py`).
A declaration that cannot be honoured (an unknown codec, a byte-order mark that
contradicts it, or a codec that is not an ASCII-compatible text codec), or
bytes the selected codec cannot decode, yields one `source-read-error` for
that file while analysis continues with the others. The
[source error recovery guide](source-error-recovery.md) describes the repair.

When an import is missing from the workspace, or a call cannot be resolved
statically, the analyzer records an explicit unresolved-reference node and a
reference relationship with source-location evidence. It does not guess a
relationship to a likely target. A query can use those unresolved references
to preserve recall over genuinely unbound references when searching for
callers; see the [agent-facing query guide](query-reference.md).

## Boundaries of the current slice

The analyzer does not execute source code or import the workspace it examines.
Its relationships are not runtime-dispatch claims, and it makes no support
claim for relationships beyond this current subset. In particular, dynamic or
otherwise unresolvable calls remain unresolved rather than being inferred from
runtime behavior.

Start with [small binding examples](python-binding-examples.md) for branch imports,
callbacks, and member resolution, or [source-error recovery](source-error-recovery.md)
to follow a partial graph through repair.
