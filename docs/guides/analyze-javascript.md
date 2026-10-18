# Current JavaScript analysis

Minotaur provides a bounded, source-only analyzer for JavaScript. The
language-neutral `minotaur analyze` command selects registered extensions, but
this interpreter supports only a pure `.js` selection. Select JavaScript files
or directories containing JavaScript files; do not mix `.js` and `.py` files in
one invocation because graph composition across interpreters is not defined.
Unsupported files encountered during a directory scan are ignored, while an
explicitly selected unsupported file is rejected by the shared selector.

Analysis records selected targets and exact source bytes for later freshness
checks. See [Graph freshness and snapshot order](../concepts/freshness.md) for
the registry-aware refresh, no-refresh, clean-skip, and graph-integrity
contract. The shared command and selected-file API are described in [Create a
language interpreter](create-a-language-interpreter.md).

For the cross-language case matrix and exact source/proof links, see the
[interpreter edge-case catalog](../concepts/interpreter-edge-cases.md).

## Analyze selected paths

Use the language-neutral command with a root, output graph, and one or more
JavaScript targets:

```text
minotaur analyze --root ROOT --output GRAPH.json [--force] TARGET [TARGET ...]
```

`ROOT` must exist, and targets are resolved from the current working directory.
Every selected target must resolve beneath `ROOT`; directory targets are
scanned recursively using the shared exclusion, symlink, and extension rules.
The root-relative POSIX path is used for graph paths and module labels. An
existing graph can be reused when its recorded selection and source bytes are
current, its analyzer semantics version is current, and it records no errors;
Git commit and branch metadata do not gate reuse. Recorded errors cause re-analysis and current diagnostic reporting.
A missing or different analyzer version on a graph with a recorded, non-empty
selection causes re-analysis and prints
`minotaur: analyzer version changed (<old> -> <new>), refreshing` to stderr
(`none` means no recorded version), before re-analysis. It does not
print the up-to-date skip message.
Use `--force` to request a new snapshot. With an explicit `--output`, an existing
loadable graph with a different or missing recorded selection, or with every
recorded file missing under the current root, exits `2` with
`pass --force to replace it`. Partial deletion or content drift with the same
selection still refreshes. Config- and scope-derived outputs reconcile changed
selections without `--force`, after validating the current root and targets.
The command writes canonical JSON atomically and returns `1` when a valid
partial graph also carries parse or source-read diagnostics.

## Supported declarations

For each readable, syntactically valid `.js` file, the analyzer emits a file
node and one module symbol. It emits top-level `function` declarations,
top-level `class` declarations, and class methods including `constructor`.
Function and arrow expressions assigned to top-level `const`, `let`, or `var`
are emitted as function symbols. These symbols are connected to their owner
with `contains` relationships.

Direct named exports retain the declaration node and carry scalar metadata
`extensions["minotaur-javascript"]["export_kind"] == "named"`. A named
declaration under `export default` remains a local declaration with
`export_kind == "default"`; anonymous default declarations and default
expressions do not invent a symbol. Their bodies are walked as expression
positions, and their uses are owned by the module symbol: the call in
`export default function () { helper(); }`, an arrow body such as
`export default () => missing();`, and the bare `export default helper;` are
all facts of the module. Nested function and class declarations do
not become symbols or `contains` relationships: uses in their bodies, including
methods of nested classes, retain the nearest enclosing emitted symbol as owner.

A class method declared as a `get` or `set` accessor also carries
`extensions["minotaur-javascript"]["declaration_role"]` with the value `"get"`
or `"set"`. `get level()` and `set level(value)` are two method symbols with the
same label, and the role keeps them distinguishable when `query diff --systems`
pairs two revisions. Other class methods, including `constructor`, carry no
role. `static` is not distinguished: `static get level()` records `get` exactly
as an instance `get level()` does, so a static and an instance accessor of the
same name and kind in one class still share one identity, and
`diff --systems` refuses them as ambiguous (exit `2`). The role does not
change node IDs, labels, or symbol kinds.

A class method whose key is computed, such as `[pick()]() {}`, emits no method
symbol, and never a method labelled with the key's identifier text. Its key and
body are walked as expressions under the nearest emitted owner: the class
symbol for a class declaration, and the enclosing owner for a class expression.
Field and static-field definitions (`field = ...` and `static field = ...`,
computed or not) are the class-member exception: they emit no key fact and no
initializer fact for either class form, matching the exclusion below. The
committed sample
[`tests/fixtures/analyzer_semantics/javascript/expressions.js`](../../tests/fixtures/analyzer_semantics/javascript/expressions.js)
pins the class-expression fields and the class-declaration regression pin,
beside `accessors.js` for the accessor roles.

## Supported module imports

The supported ESM binding form is a named import from an exactly relative
selected `.js` file:

```javascript
import { helper as localHelper } from './lib.js';
localHelper();
```

Resolution requires a matching direct named export (`function`, `class`, or a
top-level variable whose initializer is a function or arrow expression). A
resolved module dependency has one module-to-module `imports` relationship,
and calls or non-call references through the local binding resolve to the
exported declaration. An unresolved named binding still records the real
module dependency and an explicit module-owned unresolved reference.

## Calls, references, and scope

In executable expression positions, bare identifiers that resolve to supported
declarations or supported named imports produce `calls` for calls and
`references` for non-call uses. An unbound bare identifier produces an
unresolved-reference node and a `references` relationship, never a `calls`
relationship. A member expression rooted in an unshadowed identifier adds one
unresolved-reference fact labelled with the full member text, beside the base
identifier fact. This applies in call and non-call positions, including
computed chains such as `table[key].run`; member dispatch remains runtime-
sensitive and never produces a `calls` relationship. Declaration names,
object-property keys, `this`, labels, class-expression names, class method
keys, `MetaProperty`, and IIFE callee forms do not produce these expression
facts; a computed key such as `[pick()]` is an expression position and is
walked as one. The callee of `new X()` is an ordinary expression position:
`new helper()` produces a `references` edge to the resolved declaration, and
`new unres()` produces an unresolved-reference node, never a `calls`
relationship. Parameters and lexical bindings shadow module
bindings; the later supported top-level binding wins in source order.

Defaults inside binding patterns execute. A default such as
`const { a = helper() } = obj;` produces its facts under the nearest enclosing
emitted owner in every position it occurs: top-level, nested, and loop-header
declarations and catch parameters all reach it through the same pattern walker
as function parameters. A declaration pattern's computed key, such as `[key]`
in `const { [key]: value } = obj;`, is walked as an expression, while the
names a pattern binds are not uses.

A destructuring assignment, such as `[cache[key()], y = x()] = values;` or
`({ v } = obj);`, goes through the same pattern walker but introduces no
bindings. The names it assigns (`y` and `v`) are not uses. Its defaults,
computed keys, and member-expression targets are walked as expressions:
`cache[key()]` produces the `cache` and `cache[key()]` facts and the call to
`key`, just as the plain assignment `cache[key()] = value` does. An assigned
name never shadows a later default or key in the same pattern, so `y = x()`
still records a `calls` relationship to `x`. The same rules apply to a
`for ... of` or `for ... in` target pattern. A plain assignment target such as
`x = value` remains an expression position and records a use of `x`.

## Unsupported module syntax

Default, namespace, side-effect, bare, extensionless, re-export, dynamic, and
missing-target imports are not resolved. Each unsupported import/export
operation is represented by an explicit unresolved reference owned by the
importing module. The local name that binds an unsupported import is neither a
binding nor a shadow, so a later use is an ordinary unbound name that produces
its own unresolved reference instead of being suppressed. A local export list
emits one fact per specifier; its stable text is `export#<exported-name>`, so
`export { helper as publicName }` emits `export#publicName`. Other stable forms
include `./lib.js#default`, `package#*`, `./lib.js#side-effect`, and
`./lib.js#dynamic`. Unsupported syntax never creates a `calls` edge or an
unresolved-node `imports` edge. CommonJS is not resolved: `require` and
`module` have no special handling, so `require('./lib.js')` and
`module.exports` produce unresolved references and never an `imports`
relationship.

## Malformed files and diagnostics

Parsing is all-or-nothing per file. A malformed JavaScript file produces a
`PARSE_ERROR` diagnostic and contributes no nodes or relationships from a
recovered parser fragment. A source-read failure has the same per-file
exclusion. Other selected, readable, valid files continue to produce their
facts, and the command reports the diagnostic alongside the partial graph. A
file whose analysis makes the analyzer itself fail unexpectedly is excluded
the same way: it is reported as one error-severity `interpreter-error`
diagnostic with no location, naming the exception class and its text, and
contributes no facts, while the other files are analyzed together, the partial
graph is written, and `analyze` exits `1`. A failure that no single selected
file explains is a command error that exits `2` and writes no graph; see
[Current Python analysis](analyze-python.md#diagnostics-and-unresolved-references).

## Explicit exclusions

The first slice deliberately excludes object-literal methods because they have
no stable owning symbol without property-chain resolution. It excludes `this`
because dispatch requires runtime flow analysis, and class fields and static
fields because their initialization semantics are ES2022 runtime behavior;
that exclusion covers both class declarations and class expressions, whose
field and static-field definitions emit no key or initializer facts. The
analyzer does not execute or import the analyzed source, infer dynamic
dispatch, or compose JavaScript and Python graph documents.

## Freshness metadata

Each JavaScript file node carries the lowercase SHA-256 digest of its exact
source bytes in
`extensions["minotaur-javascript"]["content_sha256"]`. The CLI records the
sorted root-relative selection in the shared
`extensions["minotaur"]["selection"]` document extension. When the root is in
a Git work tree, `source_control` records the commit and branch at which the
graph was last actually generated. That stamp is provenance, never a
freshness gate or substitute for content digests, and may lag `HEAD`. These
values are metadata, not node-identity inputs; use the freshness guide for the
complete observable refresh contract.

Run the [two-file JavaScript walkthrough](../../examples/javascript-workflow/README.md)
for complete setup and real query output.
