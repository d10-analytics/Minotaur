# Current Python analysis

Minotaur provides a bounded analyzer for Python source structure. The
language-neutral CLI selects files by registered extension. This page covers
the Python registration for `.py` files; the separate JavaScript guide covers
the pure `.js` selection boundary.

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
The output parent directory must already exist, and an output path may never
also be a selected source file. An existing graph can be reused when the
recorded selection is clean and, for a Git work tree, its recorded commit and
branch still match the current checkout: Minotaur prints
`graph is up to date, skipping analysis` and leaves the file unchanged. Use
`--force` to analyze and rewrite an existing graph regardless of freshness.
When a previously generated graph has drifted, Minotaur safely replaces it
after validating the current selection. A Git commit or branch change also
causes re-analysis even when selected source content is unchanged, so the
graph's `source_control` metadata remains a coherent snapshot.

The command writes canonical JSON atomically. It exits `0` on a clean graph,
`1` after writing a valid partial graph with parse or source-read diagnostics,
and `2` for argument, selection, dispatch, or output-preflight errors (with
no output written).

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
Nested function definitions do not become separate symbol nodes, and methods
of nested classes remain outside this slice.

Repeated direct declarations with the same name in one scope remain separate
nodes, and each node receives its own header and body relationships. Name-based
calls, loads, and imports resolve to the last definition of that name.

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

Names bound in the lexical scope that owns the load (including parameters,
assignment targets, loop and context-manager targets, comprehension and walrus
targets, and nonlocal names) are suppressed instead of being reported as
unresolved, because a load through a local name says nothing static about the
workspace. This suppression applies to function and method bodies only:
module-level and class-body bindings are still reported, so a module-level loop
variable used as `module_level_name.attr`, or a class attribute used as
`attr.thing` in the class body, remains an unresolved fact. A global
declaration remains eligible for resolution. A name bound by an `import` or
`from ... import` statement is never a dynamic local, including inside a
function body: a function-local `from lib import helper` followed by `helper()`
is reported as unresolved until function-local import resolution lands, and a
function-local import that rebinds a module alias to a different target
(`import lib` at module level, `import other as lib` in the body) refuses to
resolve through the module alias and reports `lib.helper` as unresolved.

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
a Git work tree, the document may also contain the current commit and branch
in `source_control`; this is snapshot context, not a freshness substitute.

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
