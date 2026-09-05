# System definitions

System definitions give the fixed graph queries something they otherwise
cannot ask: *part* of a codebase. With one or more committed definitions,
`surface`, `consumers`, and `system-deps` answer who reaches across a declared
system boundary. This guide documents the model the three queries share: how
boundary membership works, the two consumption layers they report, and the
deterministic record semantics of each query.

The committed file format itself — where definitions live and what makes one
invalid — is the [system definition format v1](../formats/system-definition-v1.md)
reference. Why the shipped definition is only a scope is stated in
[Purpose and boundary](../concepts/purpose.md).

## Prerequisites

A system query needs three things to exist:

1. **An analyzed graph** of the source selection (`minotaur analyze`),
   because every relationship is computed from the analyzed graph only.
2. **A committed systems tree** at the resolved `systems_dir` (default
   `docs/systems` under the resolved root), holding one directory per declared
   system.
3. **The source root** the graph and the definition's root-relative file
   paths are relative to, passed as `--root` (or supplied by a
   [project configuration](project-configuration.md)).

Query a declared system by its name:

```bash
minotaur query surface orders \
  --graph examples/system-walkthrough/minotaur-graph.json \
  --root examples/system-walkthrough --no-refresh
```

The three system queries share the graph-query options `--graph`, `--root`,
`--no-refresh`, and `--json`, plus the opt-in `--details` evidence view; run the
[system walkthrough](../../examples/system-walkthrough/) for executed output.

## Boundary membership

Membership is the deterministic exact-file test "is this file listed — Y/N".
A listed file, and every graph endpoint whose location lies in a
listed file, belongs to the one system whose `files` list contains it. There
is no implicit package-to-system, module-to-system, or name-to-system mapping:
an unlisted file is never absorbed into a system because of a directory name.

Every relationship endpoint therefore classifies into exactly one of three
categories, spelled identically in text and JSON:

* `system: <name>` — the endpoint's file is listed by the named system.
* `no_system` — the endpoint carries a path that no system lists.
* `external` — the endpoint carries no path at all: a path-less upstream
  node can never belong to a system.

An endpoint's file derives from its location when it carries one, else from
its own node path; only a node with neither is `external`. No shipped
interpreter currently emits a path-less endpoint from Python or JavaScript
source, so `external` rows are the contract's vocabulary for upstream nodes
that other analyses (or future interpreters) may produce.

## The two consumption layers

Boundary relationships come from two consumption layers, each reported with
explicit kinds:

* **Symbol layer** — `calls` and `references`: outside code invokes or refers
  to the system's symbols.
* **Module layer** — `imports`: outside code links against the system's
  modules, even when no call into the system resolves.

Importing a system's module is a *consumer fact* and never an exposed
boundary: `imports` edges are reported by `consumers` and `system-deps` but
are never `surface`. The module is not an implicit callable
boundary.

## surface

`surface SYSTEM_NAME` returns one record per *exposed in-scope symbol*: a
symbol defined in a file the system lists and reached by an inbound `calls`
or `references` edge whose source sits outside the system.

* Only the symbol layer counts. A file that merely imports the system's
  module exposes nothing.
* An edge between two in-scope endpoints — including a same-file edge — is
  internal and exposes nothing.
* Records key on the exposed symbol, never on the call site: two outside files
  calling the same symbol produce one record, and an additional call site
  never changes the record set.

Text output is one line per record — `path  symbol  kinds` — and an empty
result prints `no exposed symbols` at exit `0`.

## consumers

`consumers SYSTEM_NAME` returns one record per *outside file* participating in
a boundary relationship into the system, carrying the distinct relationship
kinds that file contributes and the concrete in-scope targets it reaches as
detail.

* One record per outside file, keyed by the file, never by the call site: a
  consumer's second call site adds targets but never another record.
* A consumer file is classified like any other endpoint: a file listed by a
  *different* declared system is a `system: <name>` consumer, an unlisted file
  is `no_system`, and a path-less source endpoint has no file and so is not a
  consumer record.
* An outside module that only imports a system module is a consumer through
  `imports` even when its calls never resolve — module-layer linking counts.
* Consumers are always computed; a system may legitimately have no determined
  consumers. An empty result prints `no consumers` at exit `0`.

## system-deps

`system-deps SYSTEM_NAME` returns one record per *target category* of the
system's own outgoing boundary relationships: each named target system the
system reaches, plus explicit `no_system` and `external` rows.

* Every outgoing `calls`, `references`, or `imports` edge whose source
  endpoint lies inside the system classifies its target endpoint into exactly
  one category; a row exists only for categories with at least one target.
* Same-system and same-file edges are internal and never a dependency.
* No target is silently attributed to a system: a path-carrying target in no
  declared system is `no_system`, a path-less upstream target is `external`,
  and a target listed by another declared system names that system.
* Each row carries the deterministic nested target detail — endpoint label,
  root-relative path, and relationship kind.

An empty result prints `no dependencies` at exit `0`.

## Deterministic records and rendering

All three queries key records on the semantic participant — the exposed
symbol, the consumer file, or the target category — and preserve call sites
as payload only. Records are returned in stable sorted order, so
the same graph and systems always produce the same bytes.

Text begins with one deterministic `coverage ` line, then the existing summary
record lines. With `--json`, each query returns the system envelope (`query`,
`refreshed`, `results`, `stale`, `coverage`) in the shared JSON envelope, whose records carry semantic
endpoint labels, root-relative paths, explicit `kind` values, and the category
spellings above — never node IDs. `--details` adds a `relationships` line or
JSON array with endpoint IDs, locations, provenance, producer/rule tags, and
all recorded evidence sites; default summaries remain ID-free. See
[Output and exit status](query-reference.md#output-and-exit-status) in the
query reference.

Coverage always describes the final graph and selected declaration: saved
selection targets, all graph file nodes, declared files represented or absent,
recorded unresolved references in the declared scope, and current refresh
diagnostics. Clean and stale `--no-refresh` invocations report diagnostic
history as unavailable; a refresh records zero or more observed diagnostics.
Coverage limits are status-neutral. Ordinary valid answers, including empty
ones, exit `0`; a completed refresh with diagnostics exits `1`; invalid input
or definitions exit `2` before refresh or output.

## Strict loading and warnings

Before any answer, a system query strict-loads the whole committed systems
tree from the resolved `systems_dir` and resolves the requested name; an
invalid definition anywhere fails the invocation with a file-attributed
`minotaur: error:` and exit `2` before any freshness refresh can start. An
unknown system name also exits `2`, listing up to five nearest declared
systems.

After the graph is loaded or refreshed, a declared file with no analyzed node
is reported as one `minotaur: warning: {path} (listed by system {name})` line
on standard error for the queried system — a diagnosis, never a silent drop —
and never changes the answer or its exit status.
