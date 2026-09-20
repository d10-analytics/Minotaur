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

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- `command-interface` points to the analysis platform for workflow
  composition; the platform receives that composition request.
- `project-acquisition` points to the analysis platform for source preparation;
  the platform receives those inputs.
- The platform points to `analysis-sql` for the bounded T-SQL interpreter.
- The platform points to `analysis-python` for Python interpretation.
- It points to `analysis-javascript` for the Python JavaScript interpreter.
- It points to `graph-contract` for graph emission and shared contracts.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
