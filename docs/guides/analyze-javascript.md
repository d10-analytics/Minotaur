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
existing graph can be reused when its recorded selection, source bytes, and
Git snapshot context are current; use `--force` to request a new snapshot.
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
expressions do not invent a symbol. Nested function and class declarations do
not become symbols or `contains` relationships: uses in their bodies retain
the enclosing emitted function or method as owner.

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
relationship. Declaration names, object-property keys, member or computed
dispatch, `this`, `new`, labels, and IIFE callee forms do not produce these
expression facts. Parameters and lexical bindings shadow module bindings;
the later supported top-level binding wins in source order.

## Unsupported module syntax

Default, namespace, side-effect, bare, extensionless, re-export, dynamic, and
missing-target imports are not resolved. Unsupported import/export syntax is
still represented by one explicit unresolved reference owned by the importing
module. Its stable text uses forms such as `./lib.js#default`,
`package#*`, `./lib.js#side-effect`, or `./lib.js#dynamic`. Unsupported syntax
never creates a `calls` edge or an unresolved-node `imports` edge.

## Malformed files and diagnostics

Parsing is all-or-nothing per file. A malformed JavaScript file produces a
`PARSE_ERROR` diagnostic and contributes no nodes or relationships from a
recovered parser fragment. A source-read failure has the same per-file
exclusion. Other selected, readable, valid files continue to produce their
facts, and the command reports the diagnostic alongside the partial graph.

## Explicit exclusions

The first slice deliberately excludes object-literal methods because they have
no stable owning symbol without property-chain resolution. It excludes `this`
because dispatch requires runtime flow analysis, and class fields and static
fields because their initialization semantics are ES2022 runtime behavior.
The analyzer does not execute or import the analyzed source, infer dynamic
dispatch, or compose JavaScript and Python graph documents.

## Freshness metadata

Each JavaScript file node carries the lowercase SHA-256 digest of its exact
source bytes in
`extensions["minotaur-javascript"]["content_sha256"]`. The CLI records the
sorted root-relative selection in the shared
`extensions["minotaur"]["selection"]` document extension. These are metadata,
not node-identity inputs; use the freshness guide for the complete observable
refresh contract.

## Transitional Python scope note

The Python interpreter does not yet suppress locally bound names; the follow-up
spec `minotaur_python_scope_resolution` removes this sentence.
