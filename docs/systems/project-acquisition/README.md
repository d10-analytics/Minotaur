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

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `command-interface` points to `project-acquisition` for configuration and
  preparation dispatch.
- `project-acquisition` points to `graph-contract` for graph inputs and
  validation.
- `project-acquisition` points to `analysis-platform` for source-selection and
  production contracts.
- `project-acquisition` points to `query-and-system-reporting` for comparison
  and reporting consumers.
- `project-acquisition` points to `source-presentation` for the immutable
  captured source bytes a comparison returns.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
