# Project acquisition

Project acquisition owns configuration, Git observations, and preparation of
current and committed project inputs for Minotaur workflows. The TOML
declaration in `system.toml` is the exact membership authority for this
boundary; this README is narrative only and does not add fields or
relationship declarations. The exact membership authority is the TOML
declaration in `system.toml`.

This boundary includes the configuration, Git probe, and comparison-acquisition
owners listed in `system.toml`. It excludes CLI grammar and dispatch,
subsystem semantics, graph representation, interpreter behavior, and
presentation details.

## Expected interactions

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- `command-interface` points to project acquisition; project acquisition
  receives that preparation request.
- Project acquisition points to `graph-contract` for graph inputs and
  validation.
- It points to `analysis-platform` for source-selection and production
  contracts.
- It points to `query-and-system-reporting` for comparison and reporting
  consumers.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
