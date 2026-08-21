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

`--no-refresh` answers from the existing graph and prints one warning to stderr
for each drifted root-relative path, for example:

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

An unknown qualified name exits `2` and lists up to five nearest graph labels;
zero callers is a successful result and prints `no callers`.

JSON uses the same records:

```json
{"query":"callers","results":[{"caller":"use.caller","column":5,"line":3,"path":"use.py","unresolved":false}]}
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
Containment and callback-only `references` edges are intentionally not part of
this call-chain impact query.

### Find unreferenced symbols

Find graph-clean functions, methods, and classes, optionally narrowing the
source paths:

```bash
minotaur query unreferenced src/package tests \
  --exclude generated_helper --exclude-file exclusions.json \
  --graph GRAPH.json --root ROOT
```

The query excludes dunder names and `test_*` names. `--exclude` may be
repeated; `--exclude-file` accepts a JSON list/object of names or one name per
line. By default only graph relationships count. Add `--text-fallback` for a
conservative hygiene pass that retains a suspect when its bare name appears
elsewhere in source text (including strings or comments), marking it
`[text-mention]`:

```text
src/package/helpers.py:18  package.helpers.orphan  function [text-mention]
```

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
Text output is grouped into additions, removals, relocations, then relationship
changes:

```text
+ package.api.new_handler
- package.api.old_handler
~ package.api.handle (relocated)
+ calls package.routes.dispatch → package.api.handle
```

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

Exit statuses are:

* `0` — results were printed, including an empty result set;
* `1` — a graph refresh completed but source diagnostics were reported; and
* `2` — argument, graph-load, selection, or unknown-symbol error.

The commands never execute or import the analyzed source. Dynamic dispatch,
reflection, generated code, and configuration-dependent behavior remain
outside the graph's static claims. For Python-specific selection, nested-scope
attribution, hashes, and reference limits, see
[`analyze-python.md`](analyze-python.md).
