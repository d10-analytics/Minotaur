# Project configuration

A Minotaur project can pin the inputs of its source-analysis commands in a
versioned `.minotaur.toml` file instead of repeating them on every command
line. When such a file governs your invocation, `analyze`, `visualize`, and
the config-consuming `query` subcommands (`callers`, `consumers`, `context`,
`definitions`, `impact`, `surface`, `system-deps`, and `unreferenced`) fill
their defaults from it, and you can run them from anywhere inside the project
without spelling out the graph path and source root again.

This guide documents the configuration contract the current CLI ships: the
`.minotaur.toml` format and its schema version, how a configuration file is
located, how configured values combine with explicit command-line values,
how every configured path is anchored, and the Python 3.10 compatibility
mechanism behind TOML parsing.

## File format and schema version

The configuration filename is `.minotaur.toml`, a TOML file whose project
contract lives in a single `[minotaur]` table. The table declares the version
of the contract it speaks with an integer `schema_version = 1`:

```toml
[minotaur]
schema_version = 1
targets = ["src/minotaur"]
```

`schema_version` is required and must be the integer `1`. A missing,
non-integer, or unsupported version is rejected so an older or newer file can
never be interpreted under the wrong contract.

The known fields inside `[minotaur]` are:

* `schema_version` — required; the integer `1`, as above.
* `targets` — required; a non-empty list of strings naming the source files
  and directories to analyze.
* `root` — optional; the declared project root. It defaults to the
  configuration file's directory and is anchored there (see
  [Path anchoring and defaults](#path-anchoring-and-defaults)).
* `graph` — optional; the analyzed graph JSON path. It defaults to
  `minotaur-graph.json` inside the declared project root.
* `systems_dir` — optional; the directory that holds the project's committed
  system definitions. It defaults to `docs/systems` inside the declared
  project root.

Any other field is unknown to the current contract and is rejected, so a
configuration can never silently carry fields the shipped commands do not
honor.

## Locating a configuration file

`analyze`, `visualize`, and the config-consuming `query` subcommands look for
a `.minotaur.toml` when they run. The explicit two-file form, `query diff OLD
NEW`, never locates, parses, or validates a configuration file; it is strictly
config-free. The committed-reference form, `query diff` with no positional
graphs (optionally with `--scope NAME`), requires a located configuration. Its
help path only locates the file to expose the configured grammar; it does not
parse or validate that configuration.

Discovery starts in the current working directory and walks up through its
parent directories toward the filesystem root, and the nearest
`.minotaur.toml` found on the way up governs the invocation. The walk has one
boundary: when the current directory is inside a Git work tree, discovery
stops at the work-tree root, so a `.minotaur.toml` above the work-tree root
never binds to a project inside it. Outside a Git work tree — or when the Git
probe is unavailable — the walk continues all the way to the filesystem root,
still preferring the nearest file.

Passing `--config CONFIG` selects exactly that configuration file instead:
the value is resolved from the current working directory when relative, must
exist (a missing file is an error naming the path), and its presence disables
walk-up discovery entirely. An explicitly selected file never merges with a
discovered configuration; one file governs the invocation. `--config` is
registered per command on the config-consuming commands.

When no configuration file governs the invocation, the command line works
exactly as it always has: the flags the configuration could have defaulted
stay required, and no-config usage errors keep their historical text.

## Overriding configured values

A configuration file supplies defaults; it never vetoes the command line.
Configured values and explicit values merge field by field, and an explicit
CLI value always wins for its own field:

* `analyze` — `--root` overrides `root`, `--output` overrides `graph`, and a
  non-empty positional `TARGET` list overrides `targets`.
* config-consuming `query` subcommands — `--root` overrides `root` and
  `--graph` overrides `graph`.
* `visualize` — `--input` overrides `graph`; `--source-root` overrides `root`
  only when that root exists as a directory (otherwise source excerpts stay
  disabled), and `--output` is always required and is never supplied by the
  configuration.

Because merging is field by field, you can override just the graph path while
keeping the configured root and targets, or keep the configured graph while
analyzing a different root. An explicit CLI value keeps its own spelling: a
relative value stays relative and is interpreted from the working directory,
and an absolute value stays absolute, exactly as before configuration
existed.

A configuration file present in the tree is validated on every
config-consuming invocation, including one whose flags are fully explicit; an
invalid file is never silently ignored in favor of command-line values.

## Path anchoring and defaults

Every configured path is anchored to a stable base so the same
`.minotaur.toml` behaves identically no matter which directory you run the
command from:

* `root` is resolved relative to the directory that contains the
  configuration file. When `root` is omitted, it defaults to the
  configuration file's directory.
* Relative `targets` entries and a relative `graph` are resolved relative to
  the declared project `root`. When `graph` is omitted, it defaults to
  `minotaur-graph.json` inside the project root.
* A relative `systems_dir` is likewise resolved relative to the declared
  project `root`. When `systems_dir` is omitted it defaults to `docs/systems`
  inside that root.
* Config-sourced paths are absolutized and canonicalized before any command
  consumes them, so analysis, queries, and visualization all see one absolute
  spelling.

For a configuration file at `/path/to/project/.minotaur.toml`:

```toml
[minotaur]
schema_version = 1
root = "src"                       # /path/to/project/src
graph = "graph.json"               # /path/to/project/src/graph.json
targets = ["pkg", "pkg/one.py"]    # under /path/to/project/src
```

Config-sourced `targets` must stay inside the declared project `root`: a
target that resolves outside the root is rejected before any source analysis,
with an error naming the offending target and root.

## Configuration errors

A configuration that cannot be honored fails the invocation before any source
analysis, graph load, or graph write. The failure exits with status `2` and
names the offending field or path: a missing or unknown field, a missing or
empty `targets`, a missing, non-integer, or unsupported `schema_version`, a
wrongly typed field, a config target escaping the declared root, or a
`--config` file that does not exist. The message tells you which field or
path to fix, and no graph or stamp sidecar is produced by the failed
invocation.

## Python 3.10 compatibility

Minotaur's supported Python floor is 3.10, which has no standard-library
`tomllib` (that module arrived in Python 3.11). TOML is therefore read through
exactly one compatibility mechanism that is effective on Python 3.10 only:

* `pyproject.toml` declares the conditional dependency
  `tomli>=2.0; python_version < "3.11"`, so `tomli` is installed only on
  Python versions below 3.11.
* The configuration module imports TOML only through a guarded shim:

  ```python
  try:
      import tomllib
  except ModuleNotFoundError:  # Python < 3.11
      import tomli as tomllib
  ```

On Python 3.11 and newer the standard-library `tomllib` import succeeds and
`tomli` is never installed or imported, so there is no mandatory third-party
TOML dependency on 3.11+. On Python 3.10 the marker installs `tomli` and the
guarded fallback binds it as the module's `tomllib`, so configuration parsing
works unchanged on the floor version. Users do not need to install anything
by hand: a normal install pulls in `tomli` exactly when the running Python is
below 3.11.
