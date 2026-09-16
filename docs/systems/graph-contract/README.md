# Graph contract

The graph contract owns graph entities, identity, validation, loading,
slicing, and serialization. The TOML declaration in `system.toml` is the exact
membership authority for this boundary; this README is narrative only and does
not add fields or relationship declarations. The exact membership authority is
the TOML declaration in `system.toml`.

This boundary includes the complete graph-model Python implementation listed
in `system.toml`. It explicitly excludes source selection, language
interpretation, query policy, source presentation, browser presentation, and
the JSON schema asset.

## Expected interactions

The following directions are expected/observed ownership or data-flow
interactions. They are non-enforced and selection-bounded; a direction carried
through composition need not appear as a direct static connection in the
selected graph.

- `command-interface` points to graph contract for graph operations.
- `project-acquisition` points to graph contract for acquired graph inputs.
- `analysis-platform` points to graph contract for emission and graph
  contracts.
- `analysis-python` points to graph contract for Python graph facts.
- `analysis-javascript` points to graph contract for JavaScript graph facts.
- `query-and-system-reporting` points to graph contract for graph indexing and
  queries.
- `source-presentation` consumes the canonical graph payload and source
  locations through command-interface composition. This owner/data-flow
  direction is not a direct static `source-presentation` to `graph-contract`
  connection in the selected graph.

Graph contract therefore has a documented ownership or data-flow relationship
with the other seven systems. These directions describe the selected proof
universe, not required runtime dependencies or architectural policy; an
observed edge is not asserted as required, and an absent edge does not prove
independence.
