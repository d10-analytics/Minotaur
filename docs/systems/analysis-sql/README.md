# SQL analysis

SQL analysis owns the bounded, AST-authoritative T-SQL structural facts listed
in `system.toml`. The TOML declaration is the exact membership authority for
this boundary; this README is narrative only and does not add fields or
relationship declarations.

This boundary includes exactly the two SQL interpreter files listed in
`system.toml`. It excludes generic SQL support, dialect autodetection, mixed
language composition, and query-specific SQL policy.

## Expected interactions

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- SQL analysis points to `analysis-platform` for shared reading, emission,
  accumulation, and workspace contracts.
- It points to `graph-contract` for graph entities, identities, and
  relationships.

The SQL analyzer is the final `.sql` entry in `default_registry()`, so the
shared selection and freshness workflows can activate this bounded language.
These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
