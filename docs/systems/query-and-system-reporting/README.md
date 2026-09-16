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

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- `command-interface` points to query and system reporting for public query
  dispatch; query and system reporting receives that request.
- `project-acquisition` points to it for comparison and reporting inputs; it
  receives those inputs.
- Query and system reporting points to `graph-contract` for graph indexing,
  validation, and graph-backed views.

System membership remains an exact TOML scope, not a desired dependency,
allow/deny rule, architectural grade, or runtime requirement. These
directions describe the selected proof universe only.
