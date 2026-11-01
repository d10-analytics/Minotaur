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

## Cross-system invariants

A path recorded in a graph is read from disk only through `resolve_source_path`
in `src/minotaur/source.py`, which this system owns. Given an already resolved
source root and a slash-separated graph path, it resolves the joined path
strictly and returns the resolved path, or `None` when the path is missing, a
parent component is not a directory, it contains NUL, it loops through
symlinks, or it resolves outside the root, including through a symlink. Any
other `OSError`, such as a permission error, propagates, so an unreadable
directory is never mistaken for a missing file. The function does not check the
file type, and a caller reads the returned path, never the root joined with the
graph path, so a symlink cannot be followed past the containment check. Each
caller keeps its own handling of `None` and of a propagated error:
`read_source_path` reports the source as unavailable and `capture_source_bytes`
omits it, while the readers in `query-and-system-reporting` and the comparison
capture in `project-acquisition` follow the pointers in their READMEs.

## Expected interactions

The bullets below follow a fixed claim convention: a bullet of the form
`X` points to `Y` states an observed static edge between two declared
systems, one of which is this system, while a bullet of the form consumes
something through `<system>` composition describes a dependency carried
through composition rather than a direct static edge.

- `command-interface` points to `source-presentation` for presentation
  dispatch.
- `query-and-system-reporting` points to `source-presentation` for source
  excerpts, presentation payloads, and containment-checked source path
  resolution.
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
