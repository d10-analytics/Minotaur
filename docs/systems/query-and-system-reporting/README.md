# Query and system reporting

Query and system reporting owns graph indexing, public queries, exact system
loading, reporting, and comparison views. The TOML declaration in
`system.toml` is the exact membership authority for this boundary; this README
is narrative only and does not add fields or relationship declarations. The
exact membership authority is the TOML declaration in `system.toml`.

This boundary includes the query package and the shared system-definition
loader listed in `system.toml`. It excludes graph production, interpreter
semantics, policy or architectural enforcement, and browser rendering.

## Cross-system invariants

Safe text output spans this system and `command-interface`. `escape_text` in
`src/minotaur/query/render.py` is the single definition of an unsafe character:
each character whose `str` form is not printable is replaced by Python's own
escape, printable characters — including every non-ASCII letter — pass through
unchanged, and no quotes are added. Record renderers escape the values they
print field by field, at the point where each is interpolated, so no symbol
label, path, kind, or role reaches a result line raw. `query diff --systems`
prints a value as a JSON string when it contains whitespace or any character the
helper changes, so one value stays one token. A JSON-encoded fragment embedded in
any printed line — a result line on standard output or a diagnostic or message
line on standard error — is escaped by the JSON serializer instead, so its
values use JSON's ASCII escapes (`\u001b` for ESC, `\u00e9` for `é`) and a value
there can differ in spelling from an escaped field on the next line; no raw
character is printed. Examples include the `coverage` and `relationships` lines
of system-query text, the `declared_files` and `connections` lines of
`query systems --details`, the old/new coverage and selection lines with the
detail values of `query diff --systems`, and the `metadata=` object of an
analysis diagnostic on standard error. The `command-interface` whole-line
stderr writer `_write_stderr` in `src/minotaur/cli.py` escapes each finished
line once before printing it, so graph, configuration, system-definition, and
Git-probe text cannot carry a raw control character to the terminal. `--json`
output bypasses the helper and carries the exact strings, encoded only by the
JSON serializer.

Every read of a graph-recorded source path, in freshness drift, source context,
and the unreferenced-symbol text scan, follows the
[source presentation cross-system invariants](../source-presentation/README.md#cross-system-invariants):
the path is resolved with `resolve_source_path` and the returned path is read.

## Expected interactions

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `command-interface` points to `query-and-system-reporting` for public query
  dispatch.
- `project-acquisition` points to `query-and-system-reporting` for comparison
  and reporting inputs.
- `query-and-system-reporting` points to `graph-contract` for graph indexing,
  validation, and graph-backed views.
- `query-and-system-reporting` points to `analysis-platform` for shared
  analysis contracts.
- `query-and-system-reporting` points to `project-acquisition` for
  configuration and project inputs.
- `query-and-system-reporting` points to `source-presentation` for source
  excerpts, presentation payloads, and containment-checked source path
  resolution.

System membership remains an exact TOML scope, not a desired dependency,
allow/deny rule, architectural grade, or runtime requirement. These
directions describe the selected proof universe only.
