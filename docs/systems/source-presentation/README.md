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

The following directions are expected/observed ownership or data-flow
interactions. They are non-enforced and selection-bounded; a direction carried
through composition need not appear as a direct static connection in the
selected graph.

- `command-interface` points to source presentation for presentation dispatch;
  source presentation receives that request.
- Source presentation consumes `graph-contract`'s canonical graph payload and
  source-location contract through command-interface composition.
- The selected graph also observes a direct static `source-presentation` to
  `graph-contract` connection (`calls`, `imports`, and `references`), including
  the shared `is_safe_path` path guard. Like every observed edge in this
  section, it is not asserted as required.

These directions describe the selected proof universe, not required runtime
dependencies or architectural policy; an observed edge is not asserted as
required, and an absent edge does not prove independence.
