"""The SQL guide keeps the view warning contract visible to users."""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).parents[1] / "docs/guides/analyze-sql.md"
FORMAT_REFERENCE = Path(__file__).parents[1] / "docs/formats/minotaur-graph-v1.md"


def _text() -> str:
    return re.sub(r"\s+", " ", GUIDE.read_text(encoding="utf-8")).strip()


def _format_text() -> str:
    return re.sub(r"\s+", " ", FORMAT_REFERENCE.read_text(encoding="utf-8")).strip()


def test_sql_guide_documents_view_warning_codes_and_nonfatal_severity() -> None:
    text = _text()
    assert "`circular-dependency`" in text
    assert "`view-depth-warning`" in text
    assert "namespaced `minotaur-sql` metadata" in text
    assert "Warnings are printed with severity and do not fail `analyze`" in text
    assert "error diagnostics still produce the existing nonzero status" in text


def test_sql_guide_documents_depth_scope_and_exclusions() -> None:
    text = _text()
    assert "`view_depth_threshold`" in text
    assert "default is `3`" in text
    assert "Procedure and function reads" in text
    assert "ordinary diamonds" in text
    assert "unrelated generic references" in text


def test_sql_guide_documents_procedure_and_function_static_read_boundary() -> None:
    text = _text()
    assert "`sql:procedure` and `sql:function` symbols" in text
    assert "parser-represented static query roots" in text
    assert "`sql:reads-from` relationships to persistent tables and views" in text
    assert "`sql:calls` relationships to declared user-defined functions" in text
    assert "A schema-qualified call is a candidate for the typed function resolver" in text
    assert (
        "an unqualified call is a candidate only when it matches a declared `sql:function`" in text
    )
    assert "Built-ins and unknown bare calls produce no call fact" in text
    assert "One relationship keeps all distinct physical call locations" in text
    assert "each location spans the function name" in text
    assert "Queries contained in DML, temporary sources, dynamic strings" in text
    assert "`EXEC(@sql)` or `sp_executesql`" in text
    assert "opaque parser forms do not create SQL dependency facts" in text
    assert "`EXEC` or `EXECUTE` procedure invocation" in text
    assert "table `DEFAULT` constraints, computed-column definitions" in text
    assert "standalone `RETURN` expressions, and `SET` assignments" in text
    assert "a separate eligible root remains available" in text
    assert "malformed batches retain the existing batch recovery behavior" in text


def test_graph_format_documents_procedure_and_function_static_read_boundary() -> None:
    text = _format_text()
    assert "`sql:procedure`, and `sql:function`" in text
    assert "declaration and static-read sources" in text
    assert "parser-represented static query roots in their bodies" in text
    assert (
        "Queries contained in DML, temporary sources, dynamic strings, and opaque parser forms"
        in text
    )
    assert "`callers` and `impact` consume their persisted read and call relationships" in text
    assert "`unreferenced` considers SQL tables, views, and functions as candidates" in text
    assert (
        "A table-valued function used as a query source has a call relationship "
        "without a read relationship" in text
    )
    assert "Calls in `EXEC` or `EXECUTE`, dynamic SQL strings, table defaults" in text
    assert "SQL relationships use `sql:reads-from`, `sql:foreign-key-to`, and `sql:calls`" in text
    assert "each location spans the function name" in text
    assert "The graph format remains stable" in text


def test_sql_guide_documents_duplicate_categories_payload_and_exclusions() -> None:
    text = _text()
    assert "`canonical-and-migration`" in text
    assert "`multi-migration`" in text
    assert "`multi-canonical`" in text
    assert '`{"minotaur-sql":{"category":<category>}}`' in text
    assert "root-relative POSIX" in text
    assert "Git history" in text
    assert "live catalog" in text
    assert "execute SQL" in text
    assert "replay migrations" in text


def test_sql_guide_documents_orphan_fk_warning_boundaries() -> None:
    text = _text()
    assert "warning with code `orphaned-foreign-key`" in text
    assert (
        "ordered `minotaur-sql` payload always contains `source_table`, `constraint_name`, "
        "`target`, and `reason`"
    ) in text
    assert "`ambiguous` — more than one selected declaration matches the target" in text
    assert "This reason takes precedence over every mapping result" in text
    assert (
        "`extraction-gap` — the target has an exact configured mapping and that root-relative "
        "`.sql` path was both selected and readable"
    ) in text
    assert (
        "`undeclared` — there is no qualifying declaration and the mapped file is absent, "
        "unreadable, or outside the selected source scope"
    ) in text
    assert 'foreign_key_target_files = { "dbo.Parent" = "schema/parent.sql" }' in text
    assert "A filename, directory name, Git history" in text
    assert "does not establish that an extraction gap exists" in text
    assert "does not change the resolved `sql:foreign-key-to` edge" in text
    assert "coalesced to one unresolved target per source table and target text" in text


def test_sql_guide_documents_foreign_key_column_evidence_with_format_owner() -> None:
    text = _text()
    # The relationship stays table-level while evidence may carry column pairs.
    assert "The relationship remains a table-level dependency" in text
    # Retained exclusions.
    assert "does not retain constraint names" in text
    assert "referential-action values" in text
    assert "replication modifiers" in text
    assert "separate edges for multiple constraints between the same table pair" in text
    # Optional ordered foreign_key_columns evidence; a mapping-less record is valid.
    assert (
        "Each `sql:foreign-key-to` evidence record may optionally carry ordered "
        "`foreign_key_columns` mappings with `local` and `referenced` position-paired, "
        "and a record without a mapping remains valid"
    ) in text
    # The format reference owns the exact field rules; the guide only links to it.
    assert (
        "[graph format reference](../formats/minotaur-graph-v1.md) owns the exact field rules"
    ) in text
    # The old wrong claim must never return.
    assert "column mappings or ordering" not in text


def test_sql_guide_documents_nested_query_reads_and_rejection() -> None:
    text = _text()
    assert "`EXISTS`, `IN`, `ALL`, `SOME`, or `ANY` body" in text
    assert "rejects the whole view or the enclosing procedure or function query root" in text


def test_sql_guide_documents_proc_spelling_and_column_less_references() -> None:
    text = _text()
    assert "`CREATE PROC` and `CREATE PROCEDURE` are equivalent" in text
    # A column-less REFERENCES keeps the table-level edge and adds no mapping.
    assert (
        "`REFERENCES` form that omits the referenced columns records the table-level edge "
        "without a column mapping in all three accepted forms"
    ) in text
    assert "an inline column constraint, a table `FOREIGN KEY` constraint" in text
    assert "a named `ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY (…) REFERENCES …` clause" in text


def test_sql_guide_documents_named_alter_foreign_key_limits() -> None:
    text = _text()
    # An ALTER whose altered table is undeclared or ambiguous warns and adds no edge.
    assert (
        "reports `orphaned-foreign-key` against the altered table's name whenever the target "
        "is not exactly one declared table"
    ) in text
    assert "keeps the existing unresolved and ambiguous-reference facts unchanged" in text
    # A declared target records nothing new: the stated limit, not a recovery.
    assert "When the target is a declared table, that same statement records nothing new" in text
    assert "foreign-key components count table nodes only" in text
    assert "this is a documented limit" in text


def test_sql_guide_documents_function_body_representability_and_limits() -> None:
    text = _text()
    # Multi-statement scalar and table-valued bodies read and call through roots.
    assert (
        "A multi-statement scalar function and a table-valued function contribute reads and "
        "calls from the parser-represented static query roots of their bodies" in text
    )
    assert (
        "statements after the body's end statement in the same batch are analyzed normally" in text
    )
    assert (
        "a table-valued function's `INSERT` contributes no read even when it selects from a "
        "declared table" in text
    )
    # An unrepresentable body keeps the declaration and one located diagnostic.
    assert (
        "An `IF`, a `WHILE`, a nested `BEGIN … END` block in any spelling case or at any "
        "depth, an opaque statement, or a missing end statement makes the body unrepresentable"
    ) in text
    assert (
        "the declaration is kept with no reads or calls and exactly one `unsupported-syntax` "
        "located at the function name" in text
    )
    assert (
        "Top-level statements the parser keeps between such a body and the next declaration "
        "in the same batch are consumed with the function, while every later declaration it "
        "keeps as its own statement in the same batch is still analyzed" in text
    )
    # A statement folded into the body is a stated limit, never a recovery.
    assert (
        "A later statement that the parser folds into the body, after a top-level `IF` or "
        "`WHILE` or after an outer `END` without `;`, is deliberately not recovered" in text
    )
    assert "the statement yields no facts, and it belongs in a later `GO` batch" in text
    # A body the parser rejects is a batch error, not a kept declaration.
    assert (
        "make the parser reject the whole batch, so the batch is one `parse-error` with no "
        "procedure or function facts" in text
    )


def test_sql_guide_documents_return_members_and_unsupported_arrangements() -> None:
    text = _text()
    # Accepted members, their subquery reads and calls, and their direct-call limit.
    assert (
        "`DECLARE`, `SET` and `RETURN` members, including a bare `RETURN;`, are accepted in "
        "procedure and function bodies rather than reported as unsupported syntax" in text
    )
    assert (
        "A subquery inside one of them is an ordinary query root, so it contributes reads and "
        "calls by the nested-query rule above" in text
    )
    assert "an unsupported source inside it rejects that root rather than the declaration" in text
    assert (
        "A direct call in one of them contributes no call: `SET @r = dbo.F(1)` produces no "
        "`sql:calls`" in text
    )
    # The first unsupported arrangement: a whole-batch rejection.
    assert (
        "After an earlier member of a procedure or function body, `RETURN 0;` and `RETURN @r` "
        "directly before `END` make the parser reject the whole batch" in text
    )
    # The second: a sole-member procedure keeps no declaration, with its own pin.
    assert (
        "As the sole member of a procedure, `RETURN 0;`, with or without `BEGIN`/`END`, keeps "
        "no procedure and reports one located `unsupported-syntax`" in text
    )
    # The bare RETURN sole-member shape behaves the same way and has its own pin.
    assert (
        "A procedure whose sole member is a bare `RETURN;` behaves the same way: one located "
        "`unsupported-syntax` and no procedure symbol" in text
    )


def test_sql_guide_documents_whole_statement_command_limit() -> None:
    text = _text()
    # No function tree or parsed name exists to keep without re-parsing text.
    assert (
        "When SQLGlot parses a whole `CREATE FUNCTION` as one opaque `Command`, there is no "
        "function tree and no parsed name to keep without re-parsing its text" in text
    )
    assert (
        "The statement and each following body tree in the batch report `unsupported-syntax`, "
        "and later declarations in the batch are still analyzed" in text
    )
    assert (
        "A declaration with no `;` before it is part of that `Command`'s text, not a tree of "
        "its own, so it yields no facts" in text
    )
    # The guide states the limit and promises no recovery.
    assert "This is a stated limit and not a recovery promise" in text
    # Internal decision identifiers never reach a user-facing guide.
    for identifier in ("AR-04", "D-00", "F-04"):
        assert identifier not in text


def test_sql_guide_documents_diagnostic_locations() -> None:
    text = _text()
    assert (
        "Every SQL diagnostic carries a location except file-level source-read and decoding "
        "failures and `interpreter-error`" in text
    )
    assert (
        "A diagnostic that reports a statement spans the statement's identifiers when it has "
        "any" in text
    )
    assert (
        "the location falls back to the line holding the first non-whitespace character of "
        "its batch" in text
    )
    assert "An unterminated lexical state is located on its opening line" in text
