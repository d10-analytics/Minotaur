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

## Try it on the bundled example

The repository ships an analyzed graph of one real module,
[`examples/python-workflow/minotaur-graph.json`](../../examples/python-workflow/minotaur-graph.json),
covering `src/minotaur/language_interpreter/selection.py`. Every command below
runs from the repository root against that checked-in graph, and every output
block is its real output.

`--no-refresh` appears throughout so the walkthrough never rewrites the
checked-in artifact: without it, a query whose selected file has drifted
re-analyzes the recorded selection and atomically rewrites the graph file in
place. On an unmodified checkout the graph is already current and both forms
print the same thing; `--no-refresh` keeps that true after you edit
`selection.py`. (If you do rewrite the example, restore it with
`git checkout examples/python-workflow/minotaur-graph.json`.)

Locate a symbol by its bare name — the qualified label is what every other
query wants:

```console
$ minotaur query definitions select_sources \
    --graph examples/python-workflow/minotaur-graph.json --root . --no-refresh
src/minotaur/language_interpreter/selection.py:36  src.minotaur.language_interpreter.selection.select_sources  function
```

Ask who calls a symbol. `select_sources` is this selection's entry point, so
nothing inside the analyzed file calls it — an empty result, not an error:

```console
$ minotaur query callers src.minotaur.language_interpreter.selection.select_sources \
    --graph examples/python-workflow/minotaur-graph.json --root . --no-refresh
no callers
```

This is the boundary of the analyzed selection, not a claim about the whole
repository: the graph contains one file, so its callers elsewhere in
`src/minotaur/` were never analyzed. Widen the `analyze` selection to widen the
answer. A symbol with an in-selection caller reports the exact call site:

```console
$ minotaur query callers src.minotaur.language_interpreter.selection._resolve_target \
    --graph examples/python-workflow/minotaur-graph.json --root . --no-refresh
src/minotaur/language_interpreter/selection.py:48:20  src.minotaur.language_interpreter.selection.select_sources
```

Trace what a change would reach, one hop out:

```console
$ minotaur query impact src.minotaur.language_interpreter.selection._resolve_target \
    --depth 1 --graph examples/python-workflow/minotaur-graph.json --root . --no-refresh
depth 0: src.minotaur.language_interpreter.selection._resolve_target
depth 1: src.minotaur.language_interpreter.selection.select_sources
```

List symbols nothing in the graph references, narrowed to one path:

```console
$ minotaur query unreferenced src/minotaur/language_interpreter/selection.py \
    --graph examples/python-workflow/minotaur-graph.json --root . --no-refresh
src/minotaur/language_interpreter/selection.py:36  src.minotaur.language_interpreter.selection.select_sources  function
```

The same boundary applies: `select_sources` is unreferenced *within this
selection* because its callers live in files that were not analyzed. Treat
`unreferenced` as a candidate list over the selection you analyzed, and add
`--text-fallback` for a more conservative pass.

Read the source around a reported site without leaving the tool:

```console
$ minotaur query context --site src/minotaur/language_interpreter/selection.py:48 \
    --before 1 --after 1 --graph examples/python-workflow/minotaur-graph.json --root .
src/minotaur/language_interpreter/selection.py:47-49
  47:     for target in targets:
> 48:         resolved = _resolve_target(target, workspace.root)
  49:         if resolved.is_file():
```

Finally, compare the checked-in snapshot against a fresh analysis written to a
scratch file, which leaves the example untouched:

```console
$ minotaur analyze --root . --output /tmp/current.json --force \
    src/minotaur/language_interpreter/selection.py
$ minotaur query diff examples/python-workflow/minotaur-graph.json /tmp/current.json
no changes
```

`no changes` on an unmodified checkout is the expected result and confirms the
checked-in example still matches the current source. Edit `selection.py` and
re-run the last two commands to see additions, removals, and relocations.

Add `--json` to any of these for the machine-readable form, which also reports
the freshness of the answer:

```console
$ minotaur query callers src.minotaur.language_interpreter.selection._resolve_target \
    --graph examples/python-workflow/minotaur-graph.json --root . --no-refresh --json
{"query":"callers","refreshed":false,"results":[{"caller":"src.minotaur.language_interpreter.selection.select_sources","column":20,"line":48,"path":"src/minotaur/language_interpreter/selection.py","unresolved":false}],"stale":[]}
```

## Common freshness behavior

`callers`, `definitions`, `impact`, and `unreferenced` accept the following
common options:

```text
--graph GRAPH --root ROOT [--no-refresh] [--json]
```

If a selected file changes, disappears, or a new supported file appears below
a recorded directory target, Minotaur performs a full re-analysis of the
recorded selection and atomically rewrites `GRAPH`. It then answers from the
new snapshot. A refresh that produced source diagnostics returns exit status
`1`, just like `analyze`; a successful refresh returns `0`.

A refresh is never silent. Before rewriting `GRAPH`, Minotaur announces it on
stderr and lists every drifted root-relative path:

```text
minotaur: refreshed graph (2 drifted paths)
minotaur: stale: src/example.py
minotaur: stale: src/removed.py
```

If every recorded target has been deleted, the refresh still runs and rewrites
`GRAPH` as an empty graph, so queries report an empty result at exit `0` rather
than answering from the prior snapshot; the recorded selection is kept so the
paths are picked up again if the files return.

`--no-refresh` answers from the existing graph and prints the same one warning
per drifted path, without the `refreshed graph` line:

```text
minotaur: stale: src/example.py
```

This is useful when an agent deliberately needs the prior snapshot. It does
not make stale facts current. Graphs made before selection metadata was
recorded cannot be refreshed automatically; analyze them again first.

For a stale `unreferenced --text-fallback --no-refresh` query, Minotaur keeps
the result graph-only: it does not scan current source text or require selected
source paths to remain readable. The stale warnings identify why optional
current-source text mentions are unavailable.

`context` always reads the current source without refreshing the graph. It
compares the requested file's recorded hash and labels the excerpt when the
file changed. `diff` compares two graph files and therefore has no source-root
freshness check.

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
