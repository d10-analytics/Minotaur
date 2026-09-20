"""Behavioral proof for the unregistered AST-authoritative SQL analyzer."""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlglot

from minotaur.language_interpreter.contract import DiagnosticCode
from minotaur.language_interpreter.sql import analyze_sql_files
from minotaur.language_interpreter.workspace import Workspace


def _analyze(tmp_path: Path, **files: bytes | str):
    paths: list[Path] = []
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        paths.append(path)
    return analyze_sql_files(Workspace(tmp_path), tuple(paths))


def _symbols(result):
    return {node.label: node for node in result.document.nodes if node.symbol_kind}


def _edges(result, kind: str):
    labels = {node.id: node.label for node in result.document.nodes}
    return {
        (labels[edge.source], labels[edge.target])
        for edge in result.document.relationships
        if edge.kind == kind
    }


def test_runtime_dependency_is_exact_and_parser_is_real() -> None:
    assert sqlglot.__version__ == "30.18.0"


def test_declarations_fks_reads_and_raw_digest_preserve_source_coordinates(tmp_path: Path) -> None:
    content = (
        "\ufeffCREATE SCHEMA [Dø] \r\nGO\r\nCREATE TABLE [Dø].[Parent] (id int)\r\nGO\r\n"
        "CREATE TABLE [Dø].[Child] (parent_id int REFERENCES [Dø].[Parent](id))\r\nGO\r\n"
        "CREATE VIEW [Dø].[V] AS SELECT * FROM [Dø].[Child]\r\n"
    )
    result = _analyze(tmp_path, **{"catalog.sql": content.encode()})
    symbols = _symbols(result)
    assert not result.diagnostics
    assert {"Dø", "Dø.Parent", "Dø.Child", "Dø.V"} <= symbols.keys()
    assert _edges(result, "sql:foreign-key-to") == {("Dø.Child", "Dø.Parent")}
    assert _edges(result, "sql:reads-from") == {("Dø.V", "Dø.Child")}
    assert symbols["Dø.Parent"].location is not None
    assert symbols["Dø.Parent"].location.range.start.line == 2
    assert symbols["Dø.Parent"].location.range.start.character == 13
    file_node = next(node for node in result.document.nodes if node.path == "catalog.sql")
    assert file_node.extensions == {
        "minotaur-sql": {"content_sha256": hashlib.sha256(content.encode()).hexdigest()}
    }


def test_go_scanner_ignores_strings_identifiers_and_nested_comments(tmp_path: Path) -> None:
    sql = """
CREATE TABLE BeforeGo (id int)
GO
SELECT 'GO' AS x, [GO] AS y /* GO /* nested */ still comment */
GO -- comment after boundary
CREATE TABLE AfterGo (id int)
GO 2
CREATE TABLE FinalGo (id int)
"""
    result = _analyze(tmp_path, **{"go.sql": sql})
    assert {"BeforeGo", "AfterGo", "FinalGo"} <= _symbols(result).keys()
    assert [d.code for d in result.diagnostics].count(DiagnosticCode.UNSUPPORTED_SYNTAX) == 2
    assert any("GO count" in diagnostic.message for diagnostic in result.diagnostics)
    assert _edges(result, "contains")


def test_unterminated_scanner_is_file_atomic(tmp_path: Path) -> None:
    result = _analyze(tmp_path, **{"bad.sql": "CREATE TABLE Lost (id int) /* unterminated"})
    assert [node.label for node in result.document.nodes] == ["bad.sql"]
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].code == DiagnosticCode.PARSE_ERROR


def test_batch_and_statement_recovery_keeps_supported_siblings(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "good.sql": (
                "CREATE TABLE A (id int)\nGO\nCREATE TABLE B (id int)\nGO\nCREATE TABLE C (id int)"
            ),
            "bad.sql": "CREATE TABLE Broken (id int\nGO\nCREATE TABLE Never (id int)",
        },
    )
    labels = _symbols(result)
    assert {"A", "B", "C"} <= labels.keys()
    assert "Broken" not in labels
    assert "Never" in labels
    assert any(
        d.code == DiagnosticCode.PARSE_ERROR and d.path == "bad.sql" for d in result.diagnostics
    )


def test_view_queries_cover_joins_subqueries_sets_and_cte_shadowing(tmp_path: Path) -> None:
    sql = """
CREATE TABLE OuterTable (id int)
GO
CREATE TABLE InnerTable (id int)
GO
CREATE VIEW V AS
WITH OuterTable AS (SELECT * FROM InnerTable)
SELECT * FROM OuterTable
UNION ALL
SELECT * FROM (SELECT * FROM OuterTable) AS nested
"""
    result = _analyze(tmp_path, **{"query.sql": sql})
    assert not any(
        node.label == "OuterTable" and node.symbol_kind == "sql:view"
        for node in result.document.nodes
    )
    assert _edges(result, "sql:reads-from") == {("V", "InnerTable")}


def test_unsupported_query_and_near_miss_do_not_emit_partial_facts(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "unsupported.sql": (
                "SELECT * FROM T\nGO\nCREATE TABLE T AS SELECT * FROM U\nGO\n"
                "ALTER TABLE T ADD CONSTRAINT fk FOREIGN KEY (x) REFERENCES U(x)"
            ),
        },
    )
    assert not _symbols(result)
    assert len(result.diagnostics) == 3
    assert all(d.code == DiagnosticCode.UNSUPPORTED_SYNTAX for d in result.diagnostics)


def test_recursive_and_temporary_ctes_are_rejected_whole(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "views.sql": (
                "CREATE VIEW recursive_v AS WITH chain AS ("
                "SELECT id FROM Base UNION ALL SELECT id FROM chain) SELECT * FROM chain\nGO\n"
                "CREATE VIEW temporary_v AS SELECT * FROM #scratch"
            )
        },
    )
    assert not _symbols(result)
    assert sum(d.code == DiagnosticCode.UNSUPPORTED_SYNTAX for d in result.diagnostics) == 2


def test_exact_neutral_families_do_not_resolve_declarations(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "neutral.sql": """
CREATE TABLE T (id int)
GO
CREATE VIEW V AS SELECT 1
GO
CREATE INDEX ix ON T(id)
GO
CREATE INDEX iv ON V(id)
GO
CREATE INDEX im ON Missing(id)
GO
EXEC sys.sp_addextendedproperty @name=N'punctuation !?', @value=N'v'
GO
EXEC sys.sp_updateextendedproperty @name=N'x', @value=N'y'
GO
EXEC sys.sp_dropextendedproperty @name=N'x'
""",
        },
    )
    assert {"T", "V"} <= _symbols(result).keys()
    assert not _edges(result, "sql:reads-from")
    assert not _edges(result, "sql:foreign-key-to")
    assert not any(d.code == DiagnosticCode.AMBIGUOUS_REFERENCE for d in result.diagnostics)
    assert sum(d.code == DiagnosticCode.UNSUPPORTED_SYNTAX for d in result.diagnostics) == 0


def test_typed_lookup_is_order_independent_and_ambiguous(tmp_path: Path) -> None:
    one = _analyze(
        tmp_path / "one",
        **{
            "a.sql": (
                "CREATE SCHEMA S\nGO\nCREATE TABLE S.T (id int)\nGO\n"
                "CREATE VIEW V AS SELECT * FROM S.T"
            ),
            "b.sql": "CREATE TABLE T (id int)",
        },
    )
    two = _analyze(
        tmp_path / "two",
        **{
            "b.sql": "CREATE TABLE T (id int)",
            "a.sql": (
                "CREATE SCHEMA S\nGO\nCREATE TABLE S.T (id int)\nGO\n"
                "CREATE VIEW V AS SELECT * FROM S.T"
            ),
        },
    )
    assert _edges(one, "sql:reads-from") == _edges(two, "sql:reads-from") == {("V", "S.T")}
    ambiguous = _analyze(
        tmp_path / "ambiguous",
        **{
            "a.sql": "CREATE TABLE T (id int)",
            "b.sql": "CREATE TABLE T (id int)\nGO\nCREATE VIEW V AS SELECT * FROM T",
        },
    )
    assert not _edges(ambiguous, "sql:reads-from")
    assert any(d.code == DiagnosticCode.DUPLICATE_DECLARATION for d in ambiguous.diagnostics)
    assert any(d.code == DiagnosticCode.AMBIGUOUS_REFERENCE for d in ambiguous.diagnostics)
    assert any(node.node_class.value == "unresolved-reference" for node in ambiguous.document.nodes)


def test_quoted_dot_is_one_identifier_and_three_part_names_are_rejected(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "names.sql": (
                "CREATE TABLE [schema.with.dot].[table.with.dot] (id int)\nGO\n"
                "CREATE TABLE db.schema.table (id int)"
            )
        },
    )
    assert "schema.with.dot.table.with.dot" in _symbols(result)
    assert len(_symbols(result)) == 1
    assert sum(d.code == DiagnosticCode.UNSUPPORTED_SYNTAX for d in result.diagnostics) == 1


def test_global_temporary_and_nonpersistent_alter_targets_are_rejected(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "near_misses.sql": (
                "CREATE VIEW V AS SELECT * FROM ##scratch\nGO\n"
                "CREATE INDEX ix ON ##scratch(id)\nGO\n"
                "ALTER VIEW V ADD c int\nGO\n"
                "ALTER TABLE #scratch ADD c int\nGO\n"
                "ALTER TABLE db.schema.T ADD c int\nGO\n"
                "ALTER TABLE T ADD c int"
            )
        },
    )
    assert [node.label for node in result.document.nodes] == ["near_misses.sql"]
    assert sum(d.code == DiagnosticCode.UNSUPPORTED_SYNTAX for d in result.diagnostics) == 5
