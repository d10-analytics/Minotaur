# JavaScript analysis

JavaScript analysis owns Minotaur's Python implementation of JavaScript
interpretation. The TOML declaration in `system.toml` is the exact membership
authority for this boundary; this README is narrative only and does not add
fields or relationship declarations. The exact membership authority is the
TOML declaration in `system.toml`.

This boundary includes exactly the Python implementation files listed in
`system.toml`. It excludes browser JavaScript, HTML/CSS and other browser
assets, Python language semantics, interpreter registry ownership, and query
policy.

## Expected interactions

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- `analysis-platform` points to JavaScript analysis for JavaScript
  interpretation; JavaScript analysis receives that request.
- JavaScript analysis points to shared `analysis-platform` helpers and
  contracts.
- It points to `graph-contract` for graph entities and serialization contracts.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
