# Equivalence fixture root

A deterministic, committed Python substrate for `scripts/check_equivalence.py`.
It includes `root_star.py`, a minimal unexecuted source case that keeps the
harness's module-only root-star analysis non-vacuous, and
`workflow/declarations.py`, the analyzer-semantics sample for declaration
roles: a property with explicit getter, setter and deleter accessors, and
`@overload`/`@typing.overload` stubs beside their implementation. It defines no
`main` and calls none of the queried symbols.
`workflow/declared_encoding.py`, the source-encoding sample, declares
`# -*- coding: latin-1 -*-` on a first line that also holds a latin-1 byte, so
the analyzer-semantics fingerprint covers a declaration found beside bytes that
are not UTF-8. It is committed as latin-1 bytes that are not valid UTF-8 and
calls its one function at module scope after a non-ASCII string literal, so it
adds no `unreferenced` hit; `pyproject.toml` excludes it from Ruff, which
rejects any source file that is not valid UTF-8.

`workflow/binding_lifecycle.py`, the binding-lifecycle sample, defines
top-level functions that a later statement rebinds (an assignment, a `del`, a
conditional assignment, a `try` import, and a star import); the module calls
them wherever the rebinding leaves the name bound. It also has a
`from workflow.helper import fixture_helper` import beside a same-module `def
helper()`, a class body with an annotation-only name, a function with an
annotation-only local, a nested class whose header and body call and load a
module name, string annotations beside `Literal[...]` and `Annotated[...]`
arguments, a quoted annotation whose name the module imports again after the
function, and a nested class whose body uses a function-local import that
differs from the module's import of the same name. The sample also carries one
case for each binding behavior: a function that writes `Holder` through `global`
inside a loop before using it, a class whose `if`-guarded import precedes the
class-body use, a function-nested class whose import shadows the module-level
`Forward` import, a function-nested class that imports a name it declares
`global`, and a module-level import of the `workflow.declared_encoding.banner`
constant, whose name stays unresolved while the import links the analyzed
module. Three class-body cases follow: a function-nested class whose method
default holds a comprehension that resolves `Forward` through the module rather
than the class's own import, a class whose function defined in an `if` suite
resolves its names without the class's imports, and, after a module-level
import that binds `workflow` to `workflow.util`, a class whose own
`import workflow` decides its body's use. Two more cases follow: a function-nested
class whose own import decides its body's call although the function rebinds the
same name conditionally, and a class whose method builds a class with a method
calling `self.declared()`, which stays unresolved rather than reaching the outer
class's `declared`. It defines no `main` and calls neither `workflow.util.unused_helper`
nor `workflow.service.Service.run`; its star import carries inline `# noqa`
codes for Ruff.

The tree is small on purpose: every query class in
`scripts/equivalence_queries.json` has a real, non-empty hit here — several
definitions of `main`, a symbol with more than one caller, a symbol with no
callers at all (`workflow.util.unused_helper`), an import graph for `impact`,
and a stable line for `context`. `minotaur query unreferenced workflow` reports
thirty-two symbols: the uncalled `workflow.util.unused_helper`, the other
fixture-only definitions (`workflow.package_scope_probe`, `workflow.cli.main`,
`workflow.service.main`, `workflow.service.Service.total`,
`workflow.declarations.Gauge`, `workflow.declarations.scale`, and the
`workflow.util` scope probes `fixture_outer`, `FixtureScopes`,
`generic_scope_probe`, `DeletedBindingScope`, and `ImportedOuterScope`), and
from the binding-lifecycle sample `replaced`, `deleted`, `conditional`,
`guarded`, `starred`, `Holder.label_text`, `local_annotation`,
`string_annotations`, `forward_reference`, `local_import_class`,
`loop_global`, `nested_class_import`, `directive_class_import`,
`header_comprehension`, `NestedFunctionScope`, `PlainImport`,
`import_over_uncertain_binding`, `DeferredOwner`, `DeferredOwner.declared`, and
`DeferredOwner.build`. Under `--text-fallback` the five rebound declarations
(`replaced`, `deleted`, `conditional`, `guarded`, `starred`) and the older
`workflow.cli.main`, `workflow.service.main`, and
`workflow.service.Service.total` gain a `[text-mention]` marker, because their
names still appear as text.

`naming/`, the module-naming sample, holds `dotted.name.py` (a dotted
component), `Upper.PY` (a non-exact suffix), and `shadow.py` beside
`shadow/__init__.py` (a sibling module shadowed by its package). Each file
defines one uniquely named function and no `main`, and `scripts/equivalence_queries.json`
queries none of them. The three files that cannot be modules keep a file node
and one `module-name-unavailable` warning each, so the sample analyzes with no
errors; only `naming.shadow`, from `naming/shadow/__init__.py`, becomes a
module.

Nothing outside the harness imports this package; it is source material to be
analyzed, not code to be executed.

## Per-language default roots

A run with no `--root` compares this root beside the baseline `src` and the
committed JavaScript and SQL samples,
`tests/fixtures/analyzer_semantics/javascript` and
`tests/fixtures/analyzer_semantics/sql`, read-only. Each root is analyzed one
language at a time: the workload `selection` names a file whose extension is
the language boundary, so only the root's files of that language reach a
single `analyze` call. The `src` root also carries JavaScript assets, so it is
analyzed by its Python files alone and a mixed tree is never passed to one
interpreter.

A selection that names a directory or module instead of a file has no
extension and no single language to expand, so it stays a whole-root workload
analyzed with the default `.` target. The committed `Onyx` optional-root
workload selects the `instrument_reader` package this way, and the documented
`--optional-root` comparison is unchanged by the per-language split.

## Expectations and the seed cell

A workload `expect` is `ok` (exit 0 with an answer), `empty` (exit 0 with the
command's exact literal, such as `no changes`), `error` (the argument-
validation path), or `difference` (exit 1 with output, the valid exit status
of `query diff`). The committed `diff-selection` entries for this root, for
`src`, and for the JavaScript root declare `difference`, because a whole-root
selection against a narrower one removes symbols. The SQL root's `diff` is the
unchanged-graph control: its single file makes `diff` print exactly `no
changes`, so it keeps `empty`.

After the distinct-HEAD and provenance guards pass, each compared root also
runs the seed-determinism cell: the branch side's own query arguments are
rerun under a second `PYTHONHASHSEED` and compared byte for byte, so output
whose order leaks out of `hash()` fails the run rather than hiding behind the
pinned seed of ordinary comparisons.
