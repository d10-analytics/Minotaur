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

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `analysis-platform` points to `analysis-javascript` for JavaScript
  interpretation.
- `analysis-javascript` points to `analysis-platform` for shared reading,
  emission, accumulation, and workspace contracts.
- `analysis-javascript` points to `graph-contract` for graph entities and
  serialization contracts.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
