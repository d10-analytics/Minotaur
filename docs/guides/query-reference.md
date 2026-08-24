# Graph queries for agents

Minotaur's fixed query commands answer common source-navigation questions from
an analyzed graph. They produce compact, grep-style text by default and a
stable JSON object with `--json`. The graph remains the backing store; query
results contain source paths and locations, not opaque graph node IDs.

Start with an analysis of the source selection you want to query:

```bash
minotaur analyze --root ROOT --output GRAPH.json TARGET [TARGET ...]
```

The analyzer records the selected root-relative targets and a SHA-256 digest
for every selected file. Query commands that take `--root` use those values
to detect drift before answering.

Pick `ROOT` as the directory imports resolve from (usually `src` for a
`src/` layout), and pass the same root to every query. Module labels are
derived from it, and with the wrong root cross-module calls stay unresolved,
so `callers`, `impact`, and `unreferenced` answer from a graph that is mostly
edges to `[unresolved]` placeholders. `analyze` warns when at least 5% of the
imports would resolve under a different root; see the
[Python analysis guide](analyze-python.md) for details.

For a step-by-step tour of these commands with real output, see the
[query walkthrough](../../examples/query-walkthrough/).

## Common freshness behavior

`callers`, `definitions`, `impact`, and `unreferenced` accept the following
common options:

```text
--graph GRAPH --root ROOT [--no-refresh] [--json]
```

`--no-refresh` answers from the existing graph and reports drift without
rewriting it; omit it when current source facts are required. `--json` exposes
the `refreshed` and `stale` fields alongside query results. `context` reads
current source without refreshing, while `diff` compares graph files and does
not inspect a source root. For the complete order-of-operations contract,
including detected and intentionally undetected changes, see
[Graph freshness and snapshot order](../concepts/freshness.md).

The `--validate` option on graph-reading commands forces full schema and
node-ID validation even when a matching sidecar would authorize the trusted
load path. Use it after external graph or sidecar edits; the concept page
documents the accepted trusted-sidecar risk and the `analyze --force` escape
hatch for source-selection changes.

## Query commands

### Find callers

Use a fully qualified symbol name:

```bash
minotaur query callers pkg.mod.target --graph GRAPH.json --root ROOT
```

Text output has one line per resolved call site:

```text
use.py:3:5  use.caller
```

Matching unresolved references whose text ends in the target's bare name are
included after resolved calls and marked explicitly:

```text
use.py:5:5  unknown.target [unresolved]
```

An unknown qualified name exits `2` and lists up to five nearest graph labels
when close matches exist; zero callers is a successful result and prints
`no callers`.

A name that matches more than one symbol — a function defined twice in one
module, for example — is ambiguous rather than empty. The query refuses to pick
a definition, lists every candidate site, and exits `2`:

```text
minotaur: error: ambiguous symbol: mod.dup; candidates: mod.py:1, mod.py:5
```

Re-run against a single definition once the duplicate is resolved, or use
`context` on a candidate site to see which one you mean.

JSON uses the same records:

```json
{"query":"callers","refreshed":false,"results":[{"caller":"use.caller","column":5,"line":3,"path":"use.py","unresolved":false}],"stale":[]}
```

Unresolved records additionally contain a `reference` field.

### Find definitions

Pass a bare name to find every symbol whose qualified label ends with it:

```bash
minotaur query definitions parse --graph GRAPH.json --root ROOT
```

The text form is `path:line  qualified.name  kind`. If more than one
definition has that bare name, every matching line is marked
`[duplicate-name]`. JSON result objects contain `path`, `line`, `symbol`,
`kind`, and the boolean `duplicate`.

### Trace inbound impact

`impact` follows inbound `calls` and `imports` relationships:

```bash
minotaur query impact package.api.handle --depth 2 \
  --graph GRAPH.json --root ROOT
```

The result is grouped by shortest traversal depth:

```text
depth 0: package.api.handle
depth 1: package.routes.dispatch
[boundary] depth 2: package.cli.main
```

With `--depth N`, symbols one step beyond the limit are shown as boundary
records. JSON records contain `depth`, `symbol`, `kind`, and `boundary`.

`impact` resolves its symbol exactly as `callers` does: an unknown name exits
`2` with nearest labels, and a name shared by two definitions exits `2` listing
the candidate `path:line` sites. Neither is ever reported as `no impact`.
Containment and callback-only `references` edges are intentionally not part of
this call-chain impact query.

Module-level callers appear as `module` symbols: if a target is called at
module scope (`handle()` written directly in a module body, not inside a
function), the importing module is a real inbound dependant and shows up as
`depth N: package.module`. This differs from `diff`, which excludes module
symbols entirely (see below) — `impact` keeps them because a module-scope
call site is a change an agent needs to know about, even though `diff` treats
the same node class as unstable scaffolding.

### Find unreferenced symbols

Find graph-clean functions, methods, and classes, optionally narrowing the
source paths:

```bash
minotaur query unreferenced src/package tests \
  --exclude generated_helper --exclude-file exclusions.json \
  --exclude-pattern '\.Test\w*(\.|$)' --exclude-pattern 'Event$' \
  --graph GRAPH.json --root ROOT
```

The query excludes dunder names and `test_*` names. `--exclude` may be
repeated; `--exclude-file` accepts a JSON list/object of names or one name per
line. Both match a symbol's bare name exactly. `--exclude-pattern` takes a
regular expression searched against the qualified label and may be repeated;
an invalid expression exits `2`. Patterns are how a caller encodes framework
conventions Minotaur does not know about — pytest's `Test*` classes, Qt or
other overrides that are called by a framework rather than by analyzed code,
generated modules — without Minotaur hard-coding any language or framework. By default only graph relationships count: a symbol is reported when the
only inbound call or reference comes from the symbol itself (its own decorators
or a recursive call). Use recorded anywhere else keeps it out of the result,
including module-scope use such as `app = create_app()` or `register(handler)`,
which the graph attributes to the module. Add `--text-fallback` for a
conservative hygiene pass that retains a suspect when its bare name appears
elsewhere in source text (including strings or comments), marking it
`[text-mention]`:

```text
src/package/helpers.py:18  package.helpers.orphan  function [text-mention]
```

The fallback counts occurrences of the bare name and subtracts the definitions
of that name in the scanned files, so definitions of the same name no longer
vouch for each other. It is keyed by the bare name, not by the symbol: when two
classes both define `render` and `'render'` appears once in a string, both
`A.render` and `B.render` are marked `[text-mention]`, because source text
cannot say which one was meant.

JSON records contain `path`, `line`, `symbol`, `kind`, and `text_mention`.
An empty result prints `no unreferenced symbols` and still exits `0`.

### Compare snapshots

Compare two analyzed graph files without a source root:

```bash
minotaur query diff OLD.json NEW.json
minotaur query diff OLD.json NEW.json --json
```

Symbols are keyed by `(symbol kind, qualified label)`, so inserting lines is
reported as relocation rather than as an unrelated removal and addition.
Module symbols are excluded from `added`/`removed`/`relocated` entirely:
their source range is the whole file, so any unrelated edit would otherwise
register as a spurious relocation. This is the opposite of `impact`, which
keeps module symbols as genuine inbound dependants — see "Trace inbound
impact" above.
Text output is grouped into additions, removals, relocations, then relationship
changes:

```text
+ package.api.new_handler
- package.api.old_handler
~ package.api.handle (relocated package/api.py:12→18)
+ calls package.routes.dispatch → package.api.handle
```

The relocation suffix carries the same `from`/`to` locations as JSON, using
1-based start lines; a relocated symbol whose old or new location is unknown
falls back to the bare `~ symbol (relocated)` form.

The JSON object has `added`, `removed`, `relocated`,
`relationships_added`, and `relationships_removed` arrays. A relocation entry
contains `kind`, `symbol`, `from`, and `to` location objects. Relationships are
keyed semantically by endpoint labels and kind, never by node ID.

### Show source context

Display a bounded excerpt around a one-based, root-relative source location:

```bash
minotaur query context --site src/package/api.py:42 \
  --before 3 --after 3 --graph GRAPH.json --root ROOT
```

The target line is prefixed with `>` and the other lines with a space:

```text
src/package/api.py:39-45
  39: def handle(request):
  40:     validate(request)
> 42:     return dispatch(request)
```

If the file's current bytes differ from the analyzed hash, output starts with
`[file changed since analysis]`; the excerpt still comes from the current
source so an agent can inspect the edit without confusing it with analyzed
evidence. A pre-extension graph reports `[file hash unavailable]`. JSON has one
result with `path`, `lines`, `stale`, and `hash_available`; each line contains
`line`, `target`, and `text`.

## Output and exit status

Every query supports `--json` where it is shown above. JSON is deterministic,
uses the same records as text, and contains no node IDs, SHA-256 node digests,
or evidence/provenance blocks. Empty result sets are represented by an empty
`results` array (or the corresponding empty diff arrays).

`callers`, `definitions`, `impact`, and `unreferenced` also report the
freshness of the answer alongside it, so an agent reading only stdout learns
what stderr would have told it:

```json
{"query":"definitions","refreshed":true,"results":[],"stale":["src/example.py"]}
```

`refreshed` is `true` when this invocation rewrote `GRAPH`, and `stale` lists
the drifted root-relative paths that caused it — sorted, and reported whether
or not the refresh happened, so `--no-refresh` names the paths its answer may
be wrong about. A clean graph reports `false` and `[]`. `diff` and `context`
refresh nothing and keep their own envelopes; `context` reports per-file
staleness in its result instead.

Exit statuses are:

* `0` — results were printed, including an empty result set (`--help` on any
  `query` subcommand also exits `0`, matching every other argparse-based
  program);
* `1` — a graph refresh completed but source diagnostics were reported; and
* `2` — argument, graph-load, selection, unknown-symbol, or ambiguous-symbol
  error (a symbol name that matches several definitions is never answered
  from an arbitrary one of them).

The commands never execute or import the analyzed source. Dynamic dispatch,
reflection, generated code, and configuration-dependent behavior remain
outside the graph's static claims. For Python-specific selection, nested-scope
attribution, hashes, and reference limits, see
[`analyze-python.md`](analyze-python.md).
