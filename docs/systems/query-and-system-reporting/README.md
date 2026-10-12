# Query and system reporting

Query and system reporting owns graph indexing, public queries, exact system
loading, reporting, and comparison views. The TOML declaration in
`system.toml` is the exact membership authority for this boundary; this README
is narrative only and does not add fields or relationship declarations. The
exact membership authority is the TOML declaration in `system.toml`.

This boundary includes the query package and the shared system-definition
loader listed in `system.toml`. It excludes graph production, interpreter
semantics, policy or architectural enforcement, and browser rendering.

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
  excerpts and presentation payloads.

System membership remains an exact TOML scope, not a desired dependency,
allow/deny rule, architectural grade, or runtime requirement. These
directions describe the selected proof universe only.
