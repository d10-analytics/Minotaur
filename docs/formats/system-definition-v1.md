# System definition format v1

A system definition is a committed, repository-visible artifact: one
directory per system under a configurable location, each holding a single
machine-readable file named `system.toml` whose contract is described here.
Minotaur reads nothing else in the system directory — narrative documentation
may coexist with the definition and is ignored.

Why definitions exist as committed scopes, and what questions they make
answerable, is stated in
[Purpose and boundary](../concepts/purpose.md); the query model built on them
is documented in [System definitions](../guides/system-definitions.md) and the
[query reference](../guides/query-reference.md).

## Location and the flat tree

Definitions live under the resolved `systems_dir`. The location is configured
by the optional `systems_dir` field of a
[project configuration](../guides/project-configuration.md) and defaults to
`docs/systems` inside the declared project root. When no configuration
governs a query, `systems_dir` defaults to `docs/systems` under the explicit
`--root`, so the three system queries work with just `--graph` and `--root`.

Systems are flat peers:

* Only an **immediate child directory** of `systems_dir` that contains a
  `system.toml` defines a system.
* A `system.toml` in a nested subdirectory defines nothing — systems are
  never nested, and no directory is contained in another system.
* Stray files anywhere under `systems_dir` (including a root-level
  `system.toml`) and directories without a `system.toml` define nothing.

A future system-of-systems composition would name its member systems
explicitly; there is no containment or most-specific-owner machinery in this
version.

## The definition file

Each definition is a TOML file with exactly three known top-level fields:

```toml
schema_version = 1
name = "orders"
files = ["shop/orders.py"]
```

* `schema_version` — **required**; the integer `1`. The loader supports only
  this value.
* `name` — **required**; a non-empty string unique across all definitions.
* `files` — **required**; a non-empty list of root-relative *individual
  repository file paths*, each listing one file in exactly one system.

No other field is known. `files` entries must name individual files:

* Never directories, globs or patterns, or node IDs.
* Never absolute paths, and never paths escaping the repository root through
  `..` or empty/`.` segments.
* Slash-separated, like every repository-relative path in the graph; a path
  declared twice within one definition is deduplicated, keeping its first
  position.

A definition is a scope, not a claim: it says which files a question is about.
A file is listed as a root-relative path — the definition references no
qualified symbols and never node IDs. The "all files under a directory"
convenience entry is deliberately not offered in this version.

## Narrative files are ignored

Only the `system.toml` file is read and validated. Narrative documentation
may sit beside it in the same directory (for example a `README.md`), and any
other file or directory under `systems_dir` that is not an immediate child
directory holding a `system.toml` is equally ignored. No prose is ever
parsed for boundaries or owners.

## Failure conditions

Loading the systems tree is deterministic and strict: directories are scanned
in sorted order, every definition is fully parsed and validated before any
cross-system check, and an error anywhere fails the whole load — a system
query exits `2` with a file-attributed error before answering, and no partial
system set is ever returned. Each error names the offending definition file;
a duplicate name or an overlapping file names both defining files.

The loader rejects:

* a missing, mistyped (non-integer or boolean), or unsupported
  `schema_version`;
* any unknown field — including hand-recorded, expectation-shaped, and
  curated-rule-shaped relationship keys such as `depends_on`,
  `expectations`, or edge lists, which are not part of this contract;
* a missing, mistyped, or empty `name`;
* a missing, non-list, or empty `files` list;
* a `files` entry that is not a root-relative individual file path — a
  non-string, an empty string, an absolute path, a `..` escape, a glob or
  pattern, a node ID, or a path with empty/`.` segments or a backslash;
* two definitions declaring the same system `name`; and
* one file listed in two systems — a file belongs to at most one system.

A listed file that the analyzed graph does not contain is not a load error:
it is surfaced as a `minotaur: warning:` line when the system is queried,
never silently dropped. A `system.toml` that cannot be read or parsed fails
through the same file-attributed error path as every other definition.

## Example

A repository using the default location would commit:

```text
docs/systems/
├── billing/
│   ├── README.md          # human narrative; ignored by Minotaur
│   └── system.toml
└── orders/
    └── system.toml
```

```toml
# docs/systems/billing/system.toml
schema_version = 1
name = "billing"
files = ["shop/billing.py"]
```

```toml
# docs/systems/orders/system.toml
schema_version = 1
name = "orders"
files = ["shop/orders.py"]
```

The [`system walkthrough`](../../examples/system-walkthrough/) shows these
definitions in a runnable example.
