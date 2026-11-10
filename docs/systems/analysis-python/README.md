# Python analysis

Python analysis delegates source discovery to the shared source selection and owns Python interpretation. The TOML declaration in
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

Re-exports resolve only to the module: `from pkg import f` records an
`imports` edge from the importing file to `pkg`, so `query consumers` for
`pkg/__init__.py` lists the importer while `f` itself stays a single
unresolved `pkg.f` reference.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
