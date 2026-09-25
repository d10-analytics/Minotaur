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
    assert "Queries contained in DML, temporary sources, dynamic strings" in text
    assert "`EXEC(@sql)` or `sp_executesql`" in text
    assert "opaque parser forms do not create `sql:reads-from` facts" in text
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
    assert "`callers` and `impact` consume their persisted read relationships" in text
    assert "`unreferenced` continues to consider only SQL tables and views" in text
    assert "SQL relationships use `sql:reads-from` and `sql:foreign-key-to`" in text
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
