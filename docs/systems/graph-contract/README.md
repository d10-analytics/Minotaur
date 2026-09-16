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

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- `command-interface` points to graph contract for graph operations.
- `project-acquisition` points to graph contract for acquired graph inputs.
- `analysis-platform` points to graph contract for emission and graph
  contracts.
- `analysis-python` points to graph contract for Python graph facts.
- `analysis-javascript` points to graph contract for JavaScript graph facts.
- `query-and-system-reporting` points to graph contract for graph indexing and
  queries.
- `source-presentation` points to graph contract for graph-backed payloads.

Graph contract therefore receives the selected interactions from the other
seven systems. These directions describe the selected proof universe, not
required runtime dependencies or architectural policy; an observed edge is
not asserted as required, and an absent edge does not prove independence.
