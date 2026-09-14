# Follow a program through Minotaur

Start with the [installation and virtual environment](../../README.md#quick-start)
and [first Python graph](../../examples/getting-started/README.md). This guide
assumes you can read a function, an `if` statement, and an import; it explains
additional Python and analysis vocabulary as it becomes relevant.

## Development setup and checks

From the checkout, with the environment activated:

```bash
python -m pip install -e ".[dev,visualizer]"
python -m playwright install chromium
python -m pytest tests/query/test_query_walkthrough.py -q
```

The `dev` extra installs pytest, Ruff, and mypy. The `visualizer` extra installs
Playwright for browser tests and screenshot capture; Chromium is a separate
browser download. Linux browser execution may also require system libraries;
on a machine you administer, Playwright's `install --with-deps chromium`
command installs those dependencies as well. Rendering HTML itself does not
require Playwright or a running browser.

The first test command is a focused feedback loop. Before submitting changes,
run the repository's complete GitHub Actions parity checks from bash on Linux:

```bash
scripts/run_ci.sh all
```

The runner requires Git, Python with venv support, network access for dependency
installation, and a browser-capable Linux environment. It creates fresh
environments and source copies; it does not reuse your activated environment.
Windows users can use a Linux checkout in WSL for these bash-based checks.
Run `scripts/run_ci.sh --help` for the supported switches. Individual lanes are
`test`, `lint`, `typecheck`, `package`, `browser`, and `build`; `all` runs them
in that order and continues after ordinary lane failures. Logs and results
live below `${XDG_STATE_HOME:-$HOME/.local/state}/minotaur-ci/runs`.

Dirty input is accepted for diagnosis. The runner treats only an unchanged,
clean revision with default time limits as eligible final acceptance evidence.
For a quick local lint pass, use `python -m ruff check .` and
`python -m ruff format --check .`; type checking is `python -m mypy`.
Record failures and their cause instead of assuming a small passing test means
the complete checks passed. Browser launch restrictions are missing evidence,
not successful browser verification.

## Trace the first example

Keep [app.py](../../examples/getting-started/app.py) open beside these files.
The key fact to follow is `welcome` calling `greeting`. Minotaur reads the
source; it does not call either function.

1. [cli.py](../../src/minotaur/cli.py): `main` parses command-line arguments;
   `_analyze` resolves defaults and checks output policy. `python -m minotaur`
   enters through [__main__.py](../../src/minotaur/__main__.py), which invokes
   the same CLI as the installed command. Configuration resolution lives in
   [config.py](../../src/minotaur/config.py), not in each interpreter.
2. [selection.py](../../src/minotaur/language_interpreter/selection.py):
   `select_sources` validates the root and targets and returns a workspace and
   selected paths. [registry.py](../../src/minotaur/language_interpreter/registry.py)
   maps `.py` to the Python interpreter. Selection owns path containment and
   file discovery; the language implementation receives validated files.
3. [reading.py](../../src/minotaur/language_interpreter/reading.py):
   `read_and_parse` retains original bytes and decoded text, invokes the parser,
   and returns parsed files plus diagnostics. An **AST**, or abstract syntax
   tree, represents source constructs such as functions and calls as objects.
   It describes syntax without executing the source.
4. [python/interpreter.py](../../src/minotaur/language_interpreter/python/interpreter.py):
   begin with `analyze_python_files`, then follow its declaration and expression
   emission calls. It emits nodes for `app.greeting` and `app.welcome` and a
   calls relationship from the latter to the former. That edge retains the
   source location that supports it. A **provenance** value explains how a fact
   was established; it is not a probability that the program will execute it.
   The active binding logic is in this file. The separate `binding_flow.py`
   primitives have standalone tests but are not currently imported here.
5. [document.py](../../src/minotaur/graph_model/document.py) and
   [serialization.py](../../src/minotaur/graph_model/serialization.py):
   `GraphDocument` collects nodes and relationships. The CLI attaches selection
   metadata and writes canonical JSON and its digest sidecar through shared
   graph operations. Canonical means equivalent values have a stable byte
   representation. File-content hashes detect changed input; node IDs identify
   graph entities. They serve different purposes.
6. [query/index.py](../../src/minotaur/query/index.py) and
   [query/symbols.py](../../src/minotaur/query/symbols.py): `GraphIndex` provides
   lookups over a loaded graph. `definitions` finds the helper; `callers` follows
   incoming calls to report `app.welcome`. The CLI coordinates freshness before
   those queries. [impact.py](../../src/minotaur/query/impact.py) traverses
   incoming calls/imports with **breadth-first search**: visit immediate
   neighbors first, then neighbors two steps away, giving shortest depths.
7. [presentation.py](../../src/minotaur/graph_visualizer/presentation.py) converts
   graph facts into display data. [html/render.py](../../src/minotaur/graph_visualizer/html/render.py)
   embeds that data and viewer assets into a standalone HTML file. Optional
   source embedding is handled separately from language interpretation.

For system comparisons, follow `prepare_comparison` in
[comparison.py](../../src/minotaur/comparison.py): it obtains the graph committed
at `HEAD` and a current graph produced in memory, then prepares reporting
snapshots. [system_diff.py](../../src/minotaur/query/system_diff.py) compares
system reports; [system_diff_view.py](../../src/minotaur/query/system_diff_view.py)
filters and renders that result. Run the
[system comparison example](../../examples/system-walkthrough/comparison.md)
to see why source changes and membership changes are distinct.

## Python patterns you will encounter

- **Type annotations**, such as `Path` or `tuple[Path, ...]`, describe expected
  values. The latter is a tuple containing zero or more paths. `X | None`
  means either an `X` or no value. Mypy checks these descriptions statically.
- **Dataclasses** generate routine object methods from declared fields.
  `frozen=True` prevents field reassignment; it does not make every nested
  object immutable. `slots=True` restricts instance storage to declared fields.
- **Enums** give a finite set of named choices, such as relationship kinds.
  They help distinguish a supported category from an arbitrary string.
- **Immutable collections** such as tuples and frozensets can preserve prior
  snapshots. A persistent environment returns a new value when changed instead
  of overwriting information another step still needs.
- **Generators** use `yield`, or generator-expression syntax, to produce values
  as a caller requests them. A return annotation describes their yielded values
  and does not imply all items were created in advance.
- **Underscore names** conventionally mark implementation details. A public CLI
  example should call the CLI, not depend on private helpers remaining stable.

In binding analysis, a **lattice** is a model for combining what is known about
a name: unbound, a definite import, an ordinary value, or uncertain. A **join**
combines branch information conservatively. A **tombstone** explicitly records
that a route was removed, preventing a broader imported prefix from reviving
it during lookup. A **worklist** revisits blocks when incoming facts change.
Read the [binding examples](python-binding-examples.md) before the solver tests;
those examples explain current public behavior, while standalone solver tests
explain the separate primitives.

Comments should explain ownership, ordering, and reasons for conservative
choices. Prefer a small before/after example to a restatement of a loop. Keep
user-visible contracts in guides and test them through real entry points.
