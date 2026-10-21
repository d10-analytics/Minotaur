# Minotaur systems

Each child folder of this directory is one system: a named, reviewable
boundary of Minotaur's own source. The repository's `.minotaur.toml` analyzes
`src/` and reads these definitions, so Minotaur maps itself with its own system
queries (see [Using Minotaur](README.md#using-minotaur)).

## Defining a system

A system is a folder `docs/systems/<name>/` containing two files:

- `system.toml`, with `schema_version = 1`, a `name` (this repository uses the
  folder name), and `files`, a list of individual file paths relative to
  `src/`, the configured root. Globs are not allowed. The format is
  [system definition v1](../formats/system-definition-v1.md).
- `README.md`, a narrative description of what the boundary includes and
  excludes, ending with its `## Expected interactions`.

`system.toml` is the exact membership authority. The README is narrative only
and does not define membership. A file belongs to at most one system. Minotaur
ignores files placed directly in this directory, such as this README.

## Reading path

Before design, implementation or review, read in this order:

1. [AGENTS.md](../../AGENTS.md), the operating contract for any agent.
2. The module docstrings of the files you change.
3. The `README.md` and `system.toml` of each system that owns those files,
   including its `## Cross-system invariants` section where present.
4. [Repository-wide rules](README.md#repository-wide-rules) below.
5. By task:
   - What an interpreter records:
     [the structural analysis contract](../concepts/structural-analysis-contract.md).
   - Analyzer output:
     [Changing analyzer output](../guides/contributing.md#changing-analyzer-output).
   - Graph format: [minotaur graph v1](../formats/minotaur-graph-v1.md).
   - Queries and freshness: [the query reference](../guides/query-reference.md)
     and [freshness](../concepts/freshness.md).
   - System queries: [system definitions](../guides/system-definitions.md).
   - Tests and CI:
     [Development setup and checks](../guides/contributing.md#development-setup-and-checks).

## Where documentation goes

- A rule about one module (how to call it, what it must never do) goes in that
  module's docstring.
- A rule about one system goes in that system's README.
- An invariant whose members span systems goes in the owner system's README
  under `## Cross-system invariants`, placed before `## Expected interactions`.
  The owner is the system holding the shared helper or the single authority,
  otherwise the producer side. Every other member system's README gets a
  one-line pointer to it.
- A rule with no owner system goes in
  [Repository-wide rules](README.md#repository-wide-rules).
- User-visible behavior is documented in `docs/guides/` and `docs/concepts/`
  and pinned by a test that reads the claim.
- Do not restate what code or tests show, inventories Minotaur answers, or
  counts the code determines.

## Repository-wide rules

These rules have no owner system. Each names the test that enforces it.

- **System map.** Adding, moving, or removing a module under `src/`, or
  changing the dependencies between systems, updates in the same change the
  owning `system.toml`, the `## Expected interactions` bullets of the systems
  involved, and the manifest in `tests/test_repository_system_definitions.py`:
  `_SYSTEM_FILES`, `_CONNECTIONS`, and the file and connection counts its tests
  assert. The `.minotaur.toml` `targets` list single files and whole
  directories; when the module is not under a listed directory, the same change
  also updates those `targets` and `_TARGETS` to match.
  `test_root_discovered_system_map_has_exact_manifest_and_observed_connections`
  pins each system's exact files, the targets, and the observed connections;
  `test_readme_interaction_claims_match_the_observed_connection_manifest`
  requires every observed connection to be claimed by a README bullet and every
  claim to be observed. Bullets follow the convention each README states.

## Using Minotaur

From the repository root, with this checkout's `dev` environment installed (see
[AGENTS.md](../../AGENTS.md)):

```bash
.venv/bin/minotaur analyze                        # build the graph (gitignored)
.venv/bin/minotaur query systems --details        # systems, their files, connections
.venv/bin/minotaur query consumers <system>       # outside files that use <system>
.venv/bin/minotaur query system-deps <system>     # what <system> depends on
.venv/bin/minotaur query surface <system>         # <system> symbols reached from outside
```

The graph is `minotaur-system-definitions.json` with its `.sha256` sidecar,
both ignored by Git. Queries refresh a stale graph; `--no-refresh` answers from
the graph as it is.
