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

`ROOT` must exist. Every target must be an existing file or directory inside
that root after symlinks are resolved. Directory targets are scanned
recursively for registered extensions; unsupported files found during that
scan are ignored, while an explicitly named unsupported file is an error.
Repeated and overlapping targets are analyzed once.

Normal recursive scans exclude hidden directories, caches, and virtual
environments. Explicitly selecting such a file or directory includes it.
The output parent directory must already exist. Existing outputs require
`--force`, and an output path may never also be a selected source file.

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
resolve. Import and call relationships include source-location evidence so a
consumer can identify the reference site that established the relationship.

The output uses the canonical Minotaur wire contract described in the
[Minotaur graph format reference](../formats/minotaur-graph-v1.md).

For freshness checks, each file node carries the SHA-256 digest of its exact
source bytes in the producer extension
`extensions["minotaur-python"]["content_sha256"]`. The analyze command also
records its sorted root-relative input targets in the document extension
`extensions["minotaur"]["selection"]`. These extensions are metadata and do
not affect node identity or the graph format version.

## Diagnostics and unresolved references

If a source file cannot be read or parsed, the analyzer reports a diagnostic
for that file and continues with the other files in the workspace. The failed
file contributes no structural facts.

When an import is missing from the workspace, or a call cannot be resolved
statically, the analyzer records an explicit unresolved-reference node and a
reference relationship with source-location evidence. It does not guess a
relationship to a likely target.

## Boundaries of the current slice

The analyzer does not execute source code or import the workspace it examines.
Its relationships are not runtime-dispatch claims, and it makes no support
claim for relationships beyond this current subset. In particular, dynamic or
otherwise unresolvable calls remain unresolved rather than being inferred from
runtime behavior.
