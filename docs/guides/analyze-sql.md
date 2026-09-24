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
- table and view reads become the namespaced `sql:reads-from` relationship;
- foreign-key references become `sql:foreign-key-to` relationships;
- a top-level, unconditional `ALTER TABLE` can add one or more named
  `CONSTRAINT ... FOREIGN KEY (...) REFERENCES ...` clauses when the entire
  statement contains only those additions. The altered and referenced tables
  must have persistent one- or two-part names. Optional `ON DELETE`,
  `ON UPDATE`, and `NOT FOR REPLICATION` modifiers are accepted;
- `CREATE OR ALTER VIEW` and `CREATE OR REPLACE VIEW` use SQLGlot's equivalent
  `replace=True` AST and therefore produce identical facts;
- `GO` batch boundaries are recognized without treating text scanning as SQL
  semantics.

SQLGlot's AST is the semantic authority. Unsupported AST statements are
statement-atomic, parser failures are batch-atomic, and source-read,
decoding, and unterminated-batch failures are file-atomic. A failure in one
file, batch, or statement therefore does not discard eligible facts from its
sibling scope. Diagnostics distinguish source-read, parse, unsupported,
duplicate-declaration, and ambiguous-reference conditions.

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
and line endings. SQL symbols and relationships are payload-free namespaced
extensions to the graph vocabulary. Generic `definitions` and `diff` observe
supported SQL facts. `callers`, `impact`, `surface`, `consumers`,
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
