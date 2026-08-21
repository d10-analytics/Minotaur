# Current Python analysis

Minotaur provides a bounded analyzer for Python source structure. The
language-neutral CLI selects files by registered extension; this release
registers Python for `.py` files only.

## Analyze selected paths

Run the same command through the installed console script or as a module:

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
decorators, default arguments, and annotations are evaluated outside its body
but are attributed to the function or method they belong to, at every nesting
level. Annotations count because `def f(x: Handler)` is a real dependency on
`Handler`: an agent asking whether a symbol is still used must see it before
deleting that symbol. Class headers are treated the same way: the decorators,
bases, and keywords of `class Sub(Base, metaclass=Meta)` are references from
the class (or, for a nested class, from the enclosing scope), so a base class
used only through subclassing is not reported as unreferenced. Nested function
definitions do not become separate symbol nodes, and methods of nested classes
remain outside this slice.

The interpreter also records resolvable non-call references as `references`
relationships. For example, passing a function as `register(handler)` or
accessing `button.clicked.connect(self.on_click)` records the resolved target
and the load's source location. The function of a `Call` is represented by the
`calls` relationship instead of an extra `references` relationship. An
unresolved non-call load is omitted; unresolved calls and imports retain their
explicit unresolved-reference nodes. This asymmetry keeps graphs useful for
callback discovery without creating a node for every unresolved attribute
access.

Import, call, and reference relationships include source-location evidence so
a consumer can identify the site that established the relationship.

The output uses the canonical Minotaur wire contract described in the
[Minotaur graph format reference](../formats/minotaur-graph-v1.md).

For freshness checks, each file node carries the lowercase SHA-256 digest of
its exact source bytes in the producer extension
`extensions["minotaur-python"]["content_sha256"]`. The analyze command also
records its sorted root-relative input targets in the document extension
`extensions["minotaur"]["selection"]`. The analyzer records import resolution
counts in the document extension `extensions["minotaur-python"]`:
`imports_resolved`, `imports_unresolved`, `imports_root_mismatched` (imports
that would resolve under a different root), and, when mismatches exist,
`import_root_hint` (the root-relative directory that would resolve them). These extensions are metadata and do
not affect node identity or the graph format version. When the root is inside
a Git work tree, the document may also contain the current commit and branch
in `source_control`; this is snapshot context, not a freshness substitute.

## Diagnostics and unresolved references

If a source file cannot be read or parsed, the analyzer reports a diagnostic
for that file and continues with the other files in the workspace. The failed
file contributes no structural facts.

When an import is missing from the workspace, or a call cannot be resolved
statically, the analyzer records an explicit unresolved-reference node and a
reference relationship with source-location evidence. It does not guess a
relationship to a likely target. A query can use those unresolved references
to preserve recall when searching for callers; see the [agent-facing query
guide](query-reference.md).

## Boundaries of the current slice

The analyzer does not execute source code or import the workspace it examines.
Its relationships are not runtime-dispatch claims, and it makes no support
claim for relationships beyond this current subset. In particular, dynamic or
otherwise unresolvable calls remain unresolved rather than being inferred from
runtime behavior.
