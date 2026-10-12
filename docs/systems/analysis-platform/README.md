# Analysis platform

The analysis platform owns interpreter registration, source selection,
workspace and source reading, emission, accumulation, and shared analysis
contracts. The TOML declaration in `system.toml` is the exact membership
authority for this boundary; this README is narrative only and does not add
fields or relationship declarations. The exact membership authority is the
TOML declaration in `system.toml`.

This boundary includes the shared language-interpreter platform files listed
in `system.toml`. It excludes Python and JavaScript language semantics,
command-line ownership, UI and query policy, graph persistence, and browser
assets.

## Expected interactions

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `command-interface` points to `analysis-platform` for workflow composition.
- `project-acquisition` points to `analysis-platform` for source preparation.
- `analysis-platform` points to `analysis-javascript` for JavaScript
  interpretation.
- `analysis-platform` points to `analysis-python` for Python interpretation.
- `analysis-platform` points to `analysis-sql` for the bounded T-SQL
  interpreter.
- `analysis-platform` points to `graph-contract` for graph emission and shared
  contracts.
- `analysis-platform` points to `project-acquisition` for source-selection and
  production contracts.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
