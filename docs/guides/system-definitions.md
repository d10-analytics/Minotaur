# System definitions

System definitions give the fixed graph queries something they otherwise
cannot ask: *part* of a codebase. With committed definitions, `systems`
inventories every declared boundary and its graph coverage, while `surface`,
`consumers`, and `system-deps` answer who reaches across a named boundary. This
guide documents the repository overview and the model the three named-boundary
queries share: how membership works, the two consumption layers they report,
and the deterministic record semantics of each query.

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

The repository overview and the three named-boundary queries share the
graph-query options `--graph`, `--root`, `--no-refresh`, and `--json`, plus the
opt-in `--details` evidence view; run the
[system walkthrough](../../examples/system-walkthrough/) for executed output.

## Repository overview

Use `query systems` when the question is "what declared systems are present, and
what did this graph actually represent?" It has no positional system name and
reports every strictly loaded declaration in lexical name order:

```bash
minotaur query systems [--details] [--json] \
  --graph GRAPH.json --root ROOT --no-refresh
```

The shared graph-query options may also discover the graph, root, systems
directory, and refresh policy from project configuration. The compact text
answer starts with one canonical `coverage ` JSON line and then one inventory
line per system. A valid system name is a non-empty string with no Unicode
General Category `Cc` control characters, including line breaks, tabs, Escape,
or Delete; strict loading rejects such a name before refresh or output:

```text
coverage {"declared_files":{"absent":1,"represented":2,"scope":"all_declared_system_files","total":3},"graph_files":{"count":3,"scope":"final_graph_file_nodes"},"recorded_unresolved_references":{"count":1,"scope":"all_declared_system_files"},"selection":{"status":"recorded","targets":["."]},"source_diagnostics":{"status":"unavailable"},"unassigned_files":{"count":1,"scope":"final_graph_file_node_derived_paths"}}
billing  declared 1  represented 1  absent 0
orders  declared 2  represented 1  absent 1
```

The same compact facts in JSON have exactly the top-level keys `query`,
`refreshed`, `stale`, `results`, and `coverage`. Each result has `name` and a
`declared_files` object with `scope`, `total`, `represented`, and `absent`.
The JSON representation is canonical: object keys are sorted, arrays use the
documented lexical order, separators are compact, and one newline terminates
the answer.

`coverage` keeps three file universes separate. `graph_files` counts raw final
graph `FILE` nodes. `declared_files` sums each named system's declared paths,
including paths absent from the graph; `represented` is based on any final
graph node classified to that path, so a symbol-only graph can represent a
declared path. `unassigned_files` counts sorted distinct paths derived only
from final-graph `FILE` nodes classified as `no_system`. These counts are not a
partition equation: duplicate file nodes and symbol-only representation are
valid. `recorded_unresolved_references` counts unresolved-reference nodes
classified to named systems, excluding `no_system` and `external`; it is not a
graph-wide unresolved count. `selection` records the saved analysis targets
in the existing canonical lexical order and does not promise that every target
was successfully analyzed. `source_diagnostics` is unavailable for a clean or
`--no-refresh` answer, and is `observed_on_refresh` with a count after refresh,
including zero.

`--details` puts each system's sorted declared paths beside its
`declared_files` object, puts sorted unassigned paths beside
`coverage.unassigned_files`, and adds top-level `connections`. The compact
answer omits these keys entirely. A connection is retained only when at least
one endpoint is a named system and the two endpoints are not the same named
system. Thus named-to-named (different names), named-to-`no_system`,
`no_system`-to-named, named-to-`external`, and `external`-to-named edges are
included; all same-named, unassigned-to-unassigned, and external-only pairs
are excluded. Each row sorts by source/target category, has sorted distinct
`kinds`, and carries the existing full relationship detail (endpoint IDs,
unavailable fields, extensions, evidence, and sorted sites). A details text
answer appends canonical `declared_files {}` and `connections []` lines when
there are no systems; otherwise it appends the corresponding path mapping and
connection array. An empty systems tree is a successful answer, and a partial
graph is only a report of analyzed facts — this command never claims repository
completeness.

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

## Comparing complete system results

The typed system comparison is computed once from the old and new reporting
snapshots. Its pure view can then replace the selected system without reading
either snapshot again. For example, keep the complete result returned by the
comparison, select `Checkout` to inspect its changes, and select
`Notifications` from that same complete result to see the `Payments` to
`Notifications` boundary and the new Notifications file change. A selection
uses stored involvement, so a cross-system explanation remains visible from
either participant.

The view deliberately replaces selection. Cumulative narrowing was considered,
but applying it by default would hide a Notifications change after a caller had
first viewed Checkout. If users later need an explicit intersection of several
simultaneously selected systems, that demand is the trigger to revisit the
selection policy. This view has no command-line grammar or visual interface;
it is a projection over the completed typed result. `filter_system_diff` always
returns a new typed result, including when no system is selected; its compact
view ends with the four stored coverage and selection lines, and `render_json`
delegates to the canonical typed projection.

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

Before any answer, every system query strict-loads the whole committed systems
tree from the resolved `systems_dir`. A name containing a Unicode General
Category `Cc` control character makes the definition invalid. Any invalid
definition fails the invocation with a file-attributed `minotaur: error:` and
exit `2` before any freshness refresh can start. The three named-boundary
queries then resolve the requested name; an unknown system name also exits `2`,
listing up to five nearest declared systems.

After the graph is loaded or refreshed, a declared file with no analyzed node
is reported as one `minotaur: warning: {path} (listed by system {name})` line
on standard error. A named-boundary query reports absent files for its selected
system; the repository overview reports them across all declared systems. Each
warning is a diagnosis, never a silent drop, and never changes the answer or
its exit status.
