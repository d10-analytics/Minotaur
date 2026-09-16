# Python analysis

Python analysis owns Python source discovery and interpretation, including
dormant Python modules and binding-flow behavior. The TOML declaration in
`system.toml` is the exact membership authority for this boundary; this README
is narrative only and does not add fields or relationship declarations. The
exact membership authority is the TOML declaration in `system.toml`.

This boundary includes exactly the Python interpreter files listed in
`system.toml`. It excludes JavaScript interpretation, interpreter registry and
query ownership, browser JavaScript and other browser assets, and unrelated
language-platform policy.

## Expected interactions

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- `analysis-platform` points to Python analysis for Python interpretation;
  Python analysis receives that request.
- Python analysis points to shared `analysis-platform` helpers and contracts.
- It points to `graph-contract` for graph entities and serialization contracts.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
