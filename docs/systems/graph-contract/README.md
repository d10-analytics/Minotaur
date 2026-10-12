# Graph contract

The graph contract owns graph entities, identity, validation, loading,
and serialization. The TOML declaration in `system.toml` is the exact
membership authority for this boundary; this README is narrative only and does
not add fields or relationship declarations. The exact membership authority is
the TOML declaration in `system.toml`.

This boundary includes the complete graph-model Python implementation listed
in `system.toml`. It explicitly excludes source selection, language
interpretation, query policy, source presentation, browser presentation, and
the JSON schema asset.

## Expected interactions

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `command-interface` points to `graph-contract` for graph operations.
- `project-acquisition` points to `graph-contract` for acquired graph inputs.
- `analysis-platform` points to `graph-contract` for emission and shared
  graph contracts.
- `analysis-python` points to `graph-contract` for Python graph facts.
- `analysis-javascript` points to `graph-contract` for JavaScript graph facts.
- `analysis-sql` points to `graph-contract` for SQL graph facts.
- `query-and-system-reporting` points to `graph-contract` for graph indexing
  and queries.
- `source-presentation` points to `graph-contract` for the canonical graph
  payload, source locations, and the shared path guard.
- `source-presentation` consumes the canonical graph payload and source
  locations through `command-interface` composition.

Graph contract therefore has a documented ownership or data-flow relationship
with the other eight systems. These directions describe the selected proof
universe, not required runtime dependencies or architectural policy; an
observed edge is not asserted as required, and an absent edge does not prove
independence.
