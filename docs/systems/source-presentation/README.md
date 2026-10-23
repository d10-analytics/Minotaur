# Source presentation

Source presentation owns safe source excerpts, ordinary and comparison
presentation payloads, captured-bytes source reading, and Python HTML
rendering. The TOML declaration in `system.toml` is the exact membership
authority for this boundary; this README is narrative only and does not add
fields or relationship declarations.

This boundary includes the visualizer Python implementation and the shared
source reader listed in `system.toml`, including the immutable captured-bytes
reader that comparison payloads use after a source capture has been released.
It excludes browser JavaScript, CSS, HTML templates and vendor assets, query
policy, and graph production.

## Expected interactions

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `command-interface` points to `source-presentation` for presentation
  dispatch.
- `query-and-system-reporting` points to `source-presentation` for source
  excerpts and presentation payloads.
- `project-acquisition` points to `source-presentation` for the immutable
  captured-bytes reader that comparison acquisition calls before its captures
  are released.
- `source-presentation` points to `analysis-platform` for the shared Python
  source-encoding policy.
- `source-presentation` points to `graph-contract` for the canonical graph
  payload, source locations, and the shared path guard.
- `source-presentation` points to `query-and-system-reporting` for the
  system-membership and endpoint-classification types (`System`,
  `SystemMembership`, `EndpointKind`).
- `source-presentation` consumes the canonical graph payload and
  source-location contract through `command-interface` composition.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy; an observed edge is not asserted as
required, and an absent edge does not prove independence.
