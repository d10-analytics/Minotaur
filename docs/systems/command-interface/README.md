# Command interface

The command interface owns Minotaur's entry point, command-line parsing,
dispatch, and composition of the public workflows. The TOML declaration in
`system.toml` is the exact membership authority for this boundary; this README
is narrative only and does not add fields or relationship declarations. The
exact membership authority is the TOML declaration in `system.toml`.

This boundary includes the package entry marker, module entry point, and CLI
implementation listed in `system.toml`. It excludes project acquisition,
graph entities and serialization, language-analysis semantics, query policy,
and source or browser presentation.

## Expected interactions

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- The command interface points to `project-acquisition` for configuration and
  current-versus-committed preparation.
- It points to `graph-contract` for graph loading and validation.
- It points to `analysis-platform` for source-analysis composition.
- It points to `query-and-system-reporting` for public query dispatch and
  reporting.
- It points to `source-presentation` for presentation dispatch.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy.
