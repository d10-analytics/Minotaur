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

The following directions are expected/observed ownership or data-flow
interactions. They are non-enforced and selection-bounded; a direction carried
through composition need not appear as a direct static connection in the
selected graph.

- `command-interface` points to source presentation for presentation dispatch;
  source presentation receives that request.
- Source presentation consumes `graph-contract`'s canonical graph payload and
  source-location contract through command-interface composition. This is not
  a direct static `source-presentation` to `graph-contract` connection in the
  selected graph.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy; an observed edge is not asserted as
required, and an absent edge does not prove independence.
