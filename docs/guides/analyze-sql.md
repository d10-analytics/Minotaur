# Analyze bounded T-SQL

Minotaur's `.sql` registration supports one bounded T-SQL slice through the
ordinary `analyze` command. Select a `.sql` file or a directory containing only
SQL files; matching is case-insensitive, and a graph never composes SQL with
Python or JavaScript.

```bash
minotaur analyze --root . --output graph.json --force schema.sql
minotaur query definitions Customer --graph graph.json --root . --no-refresh
```

This is structural analysis only. Minotaur does not execute SQL, connect to a
database, inspect a live catalog, replay migrations, infer a dialect from the
source, or infer application-to-database relationships. The initial dialect
boundary is the pinned SQLGlot 30.18.0 parser; there is no user-facing dialect
selector and this guide does not claim generic SQL support.

## Supported slice

The interpreter recognizes the bounded declarations, references, and
relationships described by its AST-authoritative implementation:

- schemas, persistent tables, and views become `sql:schema`, `sql:table`, and
  `sql:view` symbols;
- persistent procedures and functions become `sql:procedure` and `sql:function`
  symbols. They are visible declarations only; parameters and return syntax do
  not become graph symbols;
- table and view reads become the namespaced `sql:reads-from` relationship;
- foreign-key references become `sql:foreign-key-to` relationships;
- a top-level, unconditional `ALTER TABLE` can add one or more named
  `CONSTRAINT ... FOREIGN KEY (...) REFERENCES ...` clauses when the entire
  statement contains only those additions. The altered and referenced tables
  must have persistent one- or two-part names. Optional `ON DELETE`,
  `ON UPDATE`, and `NOT FOR REPLICATION` modifiers are accepted;
- `CREATE OR ALTER VIEW` and `CREATE OR REPLACE VIEW` use SQLGlot's equivalent
  `replace=True` AST and therefore produce identical facts;
- procedure and function bodies are opaque at this acceptance point. They emit
  no `sql:reads-from` facts, and caller, impact, and `unreferenced` semantics do
  not include their body contents. Static body-read extraction belongs to the
  [`sql-proc-function-read-dependencies`](a75baecd-3ed5-462e-b165-ceca57fb1fd3)
  package;
- `GO` batch boundaries are recognized without treating text scanning as SQL
  semantics.

SQLGlot's AST is the semantic authority. Unsupported AST statements are
statement-atomic, parser failures are batch-atomic, and source-read,
decoding, and unterminated-batch failures are file-atomic. A failure in one
file, batch, or statement therefore does not discard eligible facts from its
sibling scope. Diagnostics distinguish source-read, parse, unsupported,
duplicate-declaration, and ambiguous-reference conditions.

Existing source diagnostics are errors by default. Completed SQL graphs also
emit warning diagnostics for view cycles (`circular-dependency`) and overlong
noncyclic view paths (`view-depth-warning`); warnings include namespaced
`minotaur-sql` metadata with the canonical path and, for depth findings, the
measured depth. Warnings are printed with severity and do not fail `analyze`;
parse or other error diagnostics still produce the existing nonzero status
while the partial graph is written.

Duplicate declarations are warning-only diagnostics. Every declaration in a
duplicate group keeps its source location and receives the exact structured
payload `{"minotaur-sql":{"category":<category>}}`. The category is
`canonical-and-migration` when the group contains both migration and ordinary
files, `multi-migration` when every file matches `migration_patterns`, and
`multi-canonical` when none match. A nonmatching file is canonical regardless
of its directory, and classification uses each file's root-relative POSIX
path.

This classification does not inspect Git history or timestamps, query a live
catalog, execute SQL, or replay migrations. It describes only duplicate
declarations found in the selected source files.

### Orphaned foreign-key findings

When a foreign-key target is not resolved to exactly one declared SQL table,
the analyzer emits a warning with code `orphaned-foreign-key`. Its ordered
`minotaur-sql` payload always contains `source_table`, `constraint_name`,
`target`, and `reason`. The constraint name is the declared name, or
`unnamed` when the constraint has no name. The warning reasons are:

* `ambiguous` — more than one selected declaration matches the target. This
  reason takes precedence over every mapping result.
* `extraction-gap` — the target has an exact configured mapping and that
  root-relative `.sql` path was both selected and readable, but the analyzed
  file did not declare the target.
* `undeclared` — there is no qualifying declaration and the mapped file is
  absent, unreadable, or outside the selected source scope. A filename,
  directory name, Git history, or a mapped file that was not selected does not
  establish that an extraction gap exists.

For example, this mapping establishes which file should contain `dbo.Parent`:

```toml
[minotaur.sql]
foreign_key_target_files = { "dbo.Parent" = "schema/parent.sql" }
```

The mapping is an exact target-to-file association. It does not change the
resolved `sql:foreign-key-to` edge, generic unresolved identity, source
selection, or graph schema. An orphaned observation keeps the existing generic
unresolved `references` relationship, coalesced to one unresolved target per
source table and target text; the warning adds finding details without turning
that generic graph fact into a typed edge.

The optional SQL setting `view_depth_threshold` in `[minotaur.sql]` controls the maximum
view path depth before a `view-depth-warning` is emitted. The default is `3`.
View cycles are reported once per elementary cycle after all references have
resolved. Procedure and function reads are not extracted: declarations do not
create body reads;
ordinary diamonds and unrelated generic references do not create view warnings.
A direct core `references`
edge from a view to an unresolved reference is the final permitted depth-path
hop; generic references from any other owner do not extend that path.

Standalone foreign-key additions do not include conditional statements,
unnamed constraints, temporary or three-part table names, or ALTER statements
that add `PRIMARY KEY`, `UNIQUE`, or `CHECK` constraints, drop constraints,
alter columns, or mix foreign-key and other actions. An unsupported ALTER
statement contributes no partial foreign-key facts. A malformed batch yields
no facts from that batch, even when an earlier statement in it was valid;
later `GO` batches remain eligible. The graph records table-level dependencies
only: it does not retain constraint names, column mappings or ordering,
referential-action values, replication modifiers, or separate edges for
multiple constraints between the same table pair. Accepted modifiers add no
edge payload.

## Graph and query meaning

Every readable SQL file contributes a file node with the
`minotaur-sql.content_sha256` digest of its original bytes, including any BOM
and line endings. SQL symbols and relationships are namespaced extensions to
the graph vocabulary. Each SQL schema, table, or view symbol records its final
parsed identifier in `extensions["minotaur-sql"]["name"]`, preserving quoted
identifier segments that contain dots for `unreferenced` exclusions and text
fallback on saved graphs. Generic `definitions` and `diff` observe supported
SQL facts. `callers`, `impact`, `surface`, `consumers`,
`system-deps`, and `unreferenced` consume resolved `sql:reads-from` and
`sql:foreign-key-to` edges; their [query-reference sections](query-reference.md)
define the exact contracts. `unreferenced` uses those edges for current SQL
table and view results. Other SQL symbol and relationship kinds remain outside
these query contracts. The existing generic unresolved-reference recall remains
available.

The graph records the selected target paths. After registration, adding or
editing SQL under a recorded directory is ordinary freshness drift. A
`--no-refresh` query preserves the saved graph and reports stale paths. An
automatic refresh announces the exact attempt line before analysis. If the
new selection would mix languages, refresh exits `2`, prints the attempt and
stale lines plus the established composition error, and leaves graph and
sidecar bytes unchanged. A successful pure-SQL refresh writes the graph first
and then its digest sidecar; terminal JSON reports `refreshed: true` only after
both writes complete. If sidecar replacement fails, the newly written graph is
retained and the sidecar is absent or stale for the next read to distrust.

See [Graph freshness and snapshot order](../concepts/freshness.md), the
[query reference](query-reference.md), the [graph format](../formats/minotaur-graph-v1.md),
and the [interpreter edge-case catalog](../concepts/interpreter-edge-cases.md)
for the exact public contracts.

## File and collection conventions

The SQL analyzer is selected by the shared registry as the final `.sql`
registration. Direct `.sql` and `.SQL` selections are equivalent; recursive
directory selection ignores unsupported files, while explicitly selecting an
unsupported file remains a command error. Keep SQL proof modules language
qualified, such as
`tests/language_interpreter/sql/test_sql_interpreter.py`, so ordinary pytest
collection cannot confuse sibling `test_interpreter` modules.
