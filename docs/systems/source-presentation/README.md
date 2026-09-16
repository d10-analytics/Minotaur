# Source presentation

Source presentation owns safe source excerpts, presentation payloads, and
Python HTML rendering. The TOML declaration in `system.toml` is the exact
membership authority for this boundary; this README is narrative only and does
not add fields or relationship declarations. The exact membership authority is
the TOML declaration in `system.toml`.

This boundary includes the visualizer Python implementation and shared source
reader listed in `system.toml`. It excludes browser JavaScript, CSS, HTML
templates and vendor assets, query policy, and graph production.

## Expected interactions

The following directions are expected/observed, non-enforced, and
selection-bounded structural interactions:

- `command-interface` points to source presentation for presentation dispatch;
  source presentation receives that request.
- Source presentation points to `graph-contract` for graph-backed source
  locations and payload contracts.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy; an observed edge is not asserted as
required, and an absent edge does not prove independence.
