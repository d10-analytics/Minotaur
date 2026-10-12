# SQL analysis

SQL analysis owns the bounded, AST-authoritative T-SQL structural facts listed
in `system.toml`. The TOML declaration is the exact membership authority for
this boundary; this README is narrative only and does not add fields or
relationship declarations.

This boundary includes exactly the SQL interpreter files listed in
`system.toml`. It excludes generic SQL support, dialect autodetection, mixed
language composition, and query-specific SQL policy.

## Expected interactions

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `analysis-sql` points to `analysis-platform` for shared reading, emission,
  accumulation, and workspace contracts.
- `analysis-sql` points to `graph-contract` for graph entities, identities,
  and relationships.
- `analysis-sql` points to `project-acquisition` for the SQL interpreter
  settings contract (`SqlSettings`).

The SQL analyzer is the final `.sql` entry in `default_registry()`, so the
shared selection and freshness workflows can activate this bounded language.
These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
