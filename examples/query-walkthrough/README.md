# Query walkthrough

This is a runnable tour of Minotaur's fixed query commands. It uses the
checked-in graph for the bundled Python example,
`examples/python-workflow/minotaur-graph.json`, which was analyzed with
`--root src`. That root makes the labels match Python imports, such as
`minotaur.language_interpreter.selection`.

Run the commands below from the repository root. The query commands use
`--no-refresh` while reading the bundled graph so a walkthrough never rewrites
that checked-in file. The final freshness examples use a temporary copy of
the graph and source instead.

## 1. Analyze a source file

First, create a fresh snapshot in a scratch location. A successful `analyze`
command writes the graph and is silent on standard output.

```console
$ minotaur analyze --root src --output /tmp/current.json --force \
    src/minotaur/language_interpreter/selection.py
```

## 2. Find a definition

Definitions accepts a bare name and prints the matching source location,
qualified label, and symbol kind. The qualified label is the useful input to
the caller and impact queries that follow.

```console
$ minotaur query definitions select_sources \
    --graph examples/python-workflow/minotaur-graph.json --root src --no-refresh
minotaur/language_interpreter/selection.py:34  minotaur.language_interpreter.selection.select_sources  function
```

## 3. Find callers

`select_sources` has no caller inside this one-file graph, so `no callers` is a
successful empty result rather than an error. The graph boundary matters: it
does not claim that no other file in the repository calls the function.

```console
$ minotaur query callers minotaur.language_interpreter.selection.select_sources \
    --graph examples/python-workflow/minotaur-graph.json --root src --no-refresh
no callers
```

The private helper `_resolve_target` is called by `select_sources` in the
selected file. Its result identifies the exact call site.

```console
$ minotaur query callers minotaur.language_interpreter.selection._resolve_target \
    --graph examples/python-workflow/minotaur-graph.json --root src --no-refresh
minotaur/language_interpreter/selection.py:46:20  minotaur.language_interpreter.selection.select_sources
```

## 4. Trace impact

Impact follows inbound call and import relationships. With depth 1, the
changed helper is depth 0 and its direct caller is depth 1.

```console
$ minotaur query impact minotaur.language_interpreter.selection._resolve_target \
    --depth 1 --graph examples/python-workflow/minotaur-graph.json --root src --no-refresh
depth 0: minotaur.language_interpreter.selection._resolve_target
depth 1: minotaur.language_interpreter.selection.select_sources
```

## 5. Audit unreferenced symbols

The graph-only audit reports symbols with no graph reference. In this
single-file selection, `select_sources` is unreferenced because its callers are
outside the analyzed selection.

```console
$ minotaur query unreferenced minotaur/language_interpreter/selection.py \
    --graph examples/python-workflow/minotaur-graph.json --root src --no-refresh
minotaur/language_interpreter/selection.py:34  minotaur.language_interpreter.selection.select_sources  function
```

`--text-fallback` adds current-source token checks to the graph result. Here it
does not remove the candidate: the source contains no call to `select_sources`.

```console
$ minotaur query unreferenced minotaur/language_interpreter/selection.py \
    --graph examples/python-workflow/minotaur-graph.json --root src --no-refresh --text-fallback
minotaur/language_interpreter/selection.py:34  minotaur.language_interpreter.selection.select_sources  function
```

An exclusion pattern removes matching candidates from the audit. This pattern
matches the reported symbol, so the filtered result is empty.

```console
$ minotaur query unreferenced minotaur/language_interpreter/selection.py \
    --graph examples/python-workflow/minotaur-graph.json --root src --no-refresh \
    --text-fallback --exclude-pattern select_sources
no unreferenced symbols
```

## 6. Read source context

Context prints a small current-source window around a recorded site. The `>`
marker identifies the requested line, while the preceding and following lines
provide enough surrounding code to understand the call.

```console
$ minotaur query context --site minotaur/language_interpreter/selection.py:48 \
    --before 1 --after 1 --graph examples/python-workflow/minotaur-graph.json --root src
minotaur/language_interpreter/selection.py:47-49
  47:         if resolved.is_file():
> 48:             if not registry.supports(resolved):
  49:                 raise SelectionError(f"unsupported source file: {target}")
```

## 7. Observe freshness and choose a snapshot

To try the refresh behavior without touching the bundled example, prepare a
temporary source and graph copy. Run this setup from the repository root; the
first edit makes the copied source differ from the copied graph's recorded
hash.

```bash
rm -rf /tmp/query-walkthrough-src /tmp/query-walkthrough-graph.json
mkdir -p /tmp/query-walkthrough-src/minotaur/language_interpreter
cp src/minotaur/language_interpreter/selection.py \
   /tmp/query-walkthrough-src/minotaur/language_interpreter/selection.py
cp examples/python-workflow/minotaur-graph.json /tmp/query-walkthrough-graph.json
printf '\n# scratch edit 1\n' >> \
  /tmp/query-walkthrough-src/minotaur/language_interpreter/selection.py
```

Without `--no-refresh`, Minotaur re-analyzes the recorded selection before
answering. The warning names the drifted path; the definition still comes from
the refreshed graph.

```console
$ minotaur query definitions select_sources \
    --graph /tmp/query-walkthrough-graph.json --root /tmp/query-walkthrough-src
minotaur: refreshed graph (1 drifted paths)
minotaur: stale: minotaur/language_interpreter/selection.py
minotaur/language_interpreter/selection.py:34  minotaur.language_interpreter.selection.select_sources  function
```

Append a second edit to the same temporary source before running the next
command:

```bash
printf '\n# scratch edit 2\n' >> \
  /tmp/query-walkthrough-src/minotaur/language_interpreter/selection.py
```

`--no-refresh` preserves the prior snapshot and reports the stale path instead
of rewriting the graph. This is useful when an agent needs to inspect the old
answer while deciding whether a refresh is appropriate.

```console
$ minotaur query definitions select_sources \
    --graph /tmp/query-walkthrough-graph.json --root /tmp/query-walkthrough-src --no-refresh
minotaur: stale: minotaur/language_interpreter/selection.py
minotaur/language_interpreter/selection.py:34  minotaur.language_interpreter.selection.select_sources  function
```

## 8. Compare snapshots

Finally, compare the bundled graph with the fresh scratch snapshot created at
the start. `diff` is graph-to-graph, so it does not perform a source freshness
check; `no changes` confirms that the checked-in example matches the current
source.

```console
$ minotaur analyze --root src --output /tmp/current.json --force \
    src/minotaur/language_interpreter/selection.py
$ minotaur query diff examples/python-workflow/minotaur-graph.json /tmp/current.json
no changes
```
