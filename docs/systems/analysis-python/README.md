# Python analysis

Python analysis owns Python source discovery and interpretation. The TOML declaration in
`system.toml` is the exact membership authority for this boundary; this README
is narrative only and does not add fields or relationship declarations. The
exact membership authority is the TOML declaration in `system.toml`.

This boundary includes exactly the Python interpreter files listed in
`system.toml`. It excludes JavaScript interpretation, interpreter registry and
query ownership, browser JavaScript and other browser assets, and unrelated
language-platform policy.

## Expected interactions

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `analysis-platform` points to `analysis-python` for Python interpretation.
- `analysis-python` points to `analysis-platform` for shared reading, emission,
  accumulation, and workspace contracts.
- `analysis-python` points to `graph-contract` for graph entities and
  serialization contracts.

Re-exports are not resolved: `from pkg import f` records an unresolved
`pkg.f` reference with no module-layer `imports` consumer, so
`pkg/__init__.py` never links a name that another module actually defines.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
