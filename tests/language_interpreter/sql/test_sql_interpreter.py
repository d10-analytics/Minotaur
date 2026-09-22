"""Behavioral proof for the unregistered AST-authoritative SQL analyzer."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
import sqlglot

from minotaur.language_interpreter.contract import DiagnosticCode
from minotaur.language_interpreter.sql import analyze_sql_files
from minotaur.language_interpreter.sql import interpreter as sql_interpreter
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


def test_or_alter_and_or_replace_views_have_the_same_ast_authorized_facts(tmp_path: Path) -> None:
    results = []
    for spelling in ("ALTER", "REPLACE"):
        results.append(
            _analyze(
                tmp_path / spelling.casefold(),
                **{
                    "view.sql": (
                        "CREATE TABLE T(id int)\nGO\n"
                        f"CREATE OR {spelling} VIEW V AS SELECT * FROM T"
                    )
                },
            )
        )

    def facts(result):
        return (
            {(node.label, node.symbol_kind) for node in result.document.nodes if node.symbol_kind},
            _edges(result, "sql:reads-from"),
            tuple((item.code, item.message) for item in result.diagnostics),
        )

    assert facts(results[0]) == facts(results[1])
    assert facts(results[0]) == (
        {("T", "sql:table"), ("V", "sql:view")},
        {("V", "T")},
        (),
    )


def test_foreign_key_names_actions_and_replication_modifiers_do_not_change_facts(
    tmp_path: Path,
) -> None:
    result = _analyze(
        tmp_path,
        **{
            "foreign_keys.sql": (
                "CREATE TABLE Parent(id int PRIMARY KEY)\nGO\n"
                "CREATE TABLE InlineChild(parent_id int CONSTRAINT fk_inline "
                "REFERENCES Parent(id) ON DELETE CASCADE ON UPDATE SET NULL)\nGO\n"
                "CREATE TABLE TableChild(parent_id int, CONSTRAINT fk_table "
                "FOREIGN KEY(parent_id) REFERENCES Parent(id) ON DELETE NO ACTION "
                "ON UPDATE CASCADE NOT FOR REPLICATION)"
            )
        },
    )
    assert not result.diagnostics
    assert _edges(result, "sql:foreign-key-to") == {
        ("InlineChild", "Parent"),
        ("TableChild", "Parent"),
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


def test_nested_cte_shadowing_is_scope_local_and_preserves_cte_fences(tmp_path: Path) -> None:
    sql = """
CREATE TABLE T (id int)
GO
CREATE VIEW V AS
WITH a AS (
  SELECT * FROM (
    WITH a AS (SELECT * FROM T)
    SELECT * FROM a
  ) AS q
)
SELECT * FROM a
GO
CREATE VIEW direct_v AS
WITH chain AS (SELECT id FROM T UNION ALL SELECT id FROM chain)
SELECT * FROM chain
GO
CREATE VIEW indirect_v AS
WITH first AS (SELECT * FROM second), second AS (SELECT * FROM first)
SELECT * FROM first
GO
CREATE VIEW temporary_v AS SELECT * FROM #scratch
GO
CREATE VIEW modifying_v AS
WITH changed AS (DELETE FROM T OUTPUT DELETED.id)
SELECT * FROM changed
"""
    result = _analyze(tmp_path, **{"nested.sql": sql})

    assert set(_symbols(result)) == {"T", "V"}
    assert _edges(result, "sql:reads-from") == {("V", "T")}
    assert len(result.diagnostics) == 4
    assert all(item.code == DiagnosticCode.UNSUPPORTED_SYNTAX for item in result.diagnostics)


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


def test_bare_and_sys_extended_property_executes_are_graph_neutral(tmp_path: Path) -> None:
    procedures = (
        "sp_addextendedproperty",
        "sp_updateextendedproperty",
        "sp_dropextendedproperty",
    )
    statements = [
        f"{keyword} {prefix}{procedure} @name=N'x', @value=N'y'"
        for keyword in ("EXEC", "EXECUTE")
        for prefix in ("", "sys.")
        for procedure in procedures
    ]
    result = _analyze(tmp_path, **{"property.sql": "\nGO\n".join(statements)})

    assert not result.diagnostics
    assert not _symbols(result)
    assert not result.document.relationships


def test_qualified_and_unrelated_executes_remain_unsupported(tmp_path: Path) -> None:
    statements = (
        "EXEC dbo.sp_addextendedproperty @name=N'x', @value=N'y'",
        "EXECUTE dbo.sp_addextendedproperty @name=N'x', @value=N'y'",
        "EXEC database.sys.sp_updateextendedproperty @name=N'x', @value=N'y'",
        "EXECUTE database.sys.sp_updateextendedproperty @name=N'x', @value=N'y'",
        "EXEC sys..sp_dropextendedproperty @name=N'x'",
        "EXECUTE sys..sp_dropextendedproperty @name=N'x'",
        "EXEC sp_rename N'x', N'y'",
        "EXECUTE sp_rename N'x', N'y'",
    )
    result = _analyze(tmp_path, **{"unsupported.sql": "\nGO\n".join(statements)})

    assert not _symbols(result)
    assert not result.document.relationships
    assert not any(d.code == DiagnosticCode.AMBIGUOUS_REFERENCE for d in result.diagnostics)
    assert [d.code for d in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX] * len(
        statements
    )


def test_complete_index_neutral_predicate_never_resolves_a_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_resolution(*_args, **_kwargs):
        pytest.fail("index neutrality must not enter declaration resolution")

    monkeypatch.setattr(sql_interpreter, "_resolve", fail_resolution)
    result = _analyze(
        tmp_path,
        **{
            "indexes.sql": (
                "CREATE SCHEMA S\nGO\nCREATE TABLE T(id int)\nGO\n"
                "CREATE TABLE S.Qualified(id int)\nGO\nCREATE VIEW V AS SELECT 1\nGO\n"
                "CREATE INDEX table_ix ON T(id)\nGO\n"
                "CREATE INDEX view_ix ON V(id)\nGO\n"
                "CREATE INDEX missing_ix ON Missing(id)\nGO\n"
                "CREATE UNIQUE INDEX qualified_ix ON S.Qualified(id DESC) INCLUDE (id)\nGO\n"
                "CREATE INDEX qualified_missing_ix ON S.Missing(id) WITH (FILLFACTOR=80)\nGO\n"
                "CREATE NONCLUSTERED INDEX nonclustered_table_ix ON T(id)\nGO\n"
                "CREATE CLUSTERED INDEX clustered_view_ix ON V(id)\nGO\n"
                "CREATE UNIQUE NONCLUSTERED INDEX un_missing_ix ON Missing(id)\nGO\n"
                "CREATE UNIQUE CLUSTERED INDEX unique_clustered_table_ix ON T(id)\nGO\n"
                "CREATE INDEX local_temp_ix ON #scratch(id)\nGO\n"
                "CREATE INDEX global_temp_ix ON ##scratch(id)\nGO\n"
                "CREATE INDEX variable_ix ON @scratch(id)\nGO\n"
                "CREATE INDEX three_part_ix ON db.S.T(id)\nGO\n"
                "CREATE INDEX empty_ix ON T\nGO\n"
                "CREATE NONCLUSTERED INDEX nonclustered_local_temp_ix ON #scratch(id)\nGO\n"
                "CREATE NONCLUSTERED INDEX nonclustered_global_temp_ix ON ##scratch(id)\nGO\n"
                "CREATE NONCLUSTERED INDEX nonclustered_variable_ix ON @scratch(id)\nGO\n"
                "CREATE NONCLUSTERED INDEX nonclustered_three_part_ix ON db.S.T(id)\nGO\n"
                "CREATE NONCLUSTERED INDEX nonclustered_empty_ix ON T\nGO\n"
                "CREATE CLUSTERED INDEX clustered_local_temp_ix ON #scratch(id)\nGO\n"
                "CREATE CLUSTERED INDEX clustered_global_temp_ix ON ##scratch(id)\nGO\n"
                "CREATE CLUSTERED INDEX clustered_variable_ix ON @scratch(id)\nGO\n"
                "CREATE CLUSTERED INDEX clustered_three_part_ix ON db.S.T(id)\nGO\n"
                "CREATE CLUSTERED INDEX clustered_empty_ix ON T\nGO\n"
                "CREATE UNIQUE NONCLUSTERED INDEX un_local_ix ON #scratch(id)\nGO\n"
                "CREATE UNIQUE NONCLUSTERED INDEX un_global_ix ON ##scratch(id)\nGO\n"
                "CREATE UNIQUE NONCLUSTERED INDEX un_variable_ix ON @scratch(id)\nGO\n"
                "CREATE UNIQUE NONCLUSTERED INDEX un_three_part_ix ON db.S.T(id)\nGO\n"
                "CREATE UNIQUE NONCLUSTERED INDEX un_empty_ix ON T\nGO\n"
                "CREATE UNIQUE CLUSTERED INDEX uc_local_ix ON #scratch(id)\nGO\n"
                "CREATE UNIQUE CLUSTERED INDEX uc_global_ix ON ##scratch(id)\nGO\n"
                "CREATE UNIQUE CLUSTERED INDEX uc_variable_ix ON @scratch(id)\nGO\n"
                "CREATE UNIQUE CLUSTERED INDEX uc_three_part_ix ON db.S.T(id)\nGO\n"
                "CREATE UNIQUE CLUSTERED INDEX uc_empty_ix ON T"
            )
        },
    )
    assert set(_symbols(result)) == {"S", "T", "S.Qualified", "V"}
    assert [d.code for d in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX] * 25
    assert not any(
        node.node_class.value == "unresolved-reference" for node in result.document.nodes
    )


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


def test_wrong_kind_and_qualified_missing_targets_remain_typed_unresolved(
    tmp_path: Path,
) -> None:
    result = _analyze(
        tmp_path,
        **{
            "typed.sql": (
                "CREATE TABLE Namespace(id int)\nGO\n"
                "CREATE TABLE Namespace.Member(id int)\nGO\n"
                "CREATE VIEW WrongKind AS SELECT 1\nGO\n"
                "CREATE TABLE Child(id int REFERENCES WrongKind(id))\nGO\n"
                "CREATE VIEW Reader AS SELECT * FROM MissingSchema.Target"
            )
        },
    )
    nodes = {node.id: node for node in result.document.nodes}
    unresolved = {
        node.label for node in nodes.values() if node.node_class.value == "unresolved-reference"
    }
    references = {
        (nodes[edge.source].label, nodes[edge.target].label)
        for edge in result.document.relationships
        if edge.kind == "references"
    }
    containment = {
        (nodes[edge.source].label, nodes[edge.target].label)
        for edge in result.document.relationships
        if edge.kind == "contains" and nodes[edge.source].symbol_kind
    }
    assert unresolved == {"WrongKind", "MissingSchema.Target"}
    assert references == {("Child", "WrongKind"), ("Reader", "MissingSchema.Target")}
    assert ("Namespace", "Namespace.Member") not in containment
    assert not result.diagnostics


def test_duplicate_declarations_keep_their_own_outgoing_dependencies(tmp_path: Path) -> None:
    files = {
        "a.sql": "CREATE TABLE A(id int)\nGO\nCREATE VIEW Duplicate AS SELECT * FROM A",
        "b.sql": "CREATE TABLE B(id int)\nGO\nCREATE VIEW Duplicate AS SELECT * FROM B",
    }

    def duplicate_facts(result):
        nodes = {node.id: node for node in result.document.nodes}
        duplicates = [node for node in nodes.values() if node.label == "Duplicate"]
        outgoing = {
            (nodes[edge.source].location.path, nodes[edge.target].label)
            for edge in result.document.relationships
            if edge.kind == "sql:reads-from"
        }
        return (
            len(duplicates),
            outgoing,
            tuple(
                (item.path, item.code)
                for item in result.diagnostics
                if item.code == DiagnosticCode.DUPLICATE_DECLARATION
            ),
        )

    first = _analyze(tmp_path / "first", **files)
    second = _analyze(tmp_path / "second", **dict(reversed(tuple(files.items()))))
    assert (
        duplicate_facts(first)
        == duplicate_facts(second)
        == (
            2,
            {("a.sql", "A"), ("b.sql", "B")},
            (
                ("a.sql", DiagnosticCode.DUPLICATE_DECLARATION),
                ("b.sql", DiagnosticCode.DUPLICATE_DECLARATION),
            ),
        )
    )


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
                "CREATE TABLE ##declared(id int)\nGO\n"
                "CREATE VIEW V AS SELECT * FROM ##scratch\nGO\n"
                "CREATE INDEX ix ON ##scratch(id)\nGO\n"
                "ALTER VIEW V ADD c int\nGO\n"
                "ALTER TABLE #scratch ADD c int\nGO\n"
                "ALTER TABLE db.schema.T ADD c int\nGO\n"
                "CREATE TABLE Child(id int REFERENCES ##scratch(id))\nGO\n"
                "ALTER TABLE T ADD c int"
            )
        },
    )
    assert [node.label for node in result.document.nodes] == ["near_misses.sql"]
    assert sum(d.code == DiagnosticCode.UNSUPPORTED_SYNTAX for d in result.diagnostics) == 7


def test_parser_fallback_is_exposed_only_as_sanitized_diagnostic(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="sqlglot"):
        result = _analyze(
            tmp_path,
            **{"fallback.sql": "CREATE VIEW V AS SELECT 1 WITH SCHEMABINDING"},
        )
    assert result.diagnostics == (result.diagnostics[0],)
    assert result.diagnostics[0].code == DiagnosticCode.UNSUPPORTED_SYNTAX
    assert result.diagnostics[0].message == "unsupported T-SQL syntax"
    assert not any(record.name == "sqlglot" for record in caplog.records)


def test_parse_and_fallback_diagnostics_are_ordered_and_sanitized(tmp_path: Path, caplog) -> None:
    files = {
        "b_parse.sql": "CREATE TABLE Broken(",
        "a_fallback.sql": "CREATE VIEW V AS SELECT 1 WITH SCHEMABINDING",
    }
    with caplog.at_level(logging.WARNING, logger="sqlglot"):
        first = _analyze(tmp_path / "first", **files)
        second = _analyze(tmp_path / "second", **dict(reversed(tuple(files.items()))))

    def diagnostics(result):
        return tuple(
            (item.path, item.code, item.message, item.location) for item in result.diagnostics
        )

    expected = (
        ("a_fallback.sql", DiagnosticCode.UNSUPPORTED_SYNTAX, "unsupported T-SQL syntax", None),
        ("b_parse.sql", DiagnosticCode.PARSE_ERROR, "unable to parse T-SQL batch", None),
    )
    assert diagnostics(first) == diagnostics(second) == expected
    assert all(
        "\x1b" not in item.message and "Command" not in item.message for item in first.diagnostics
    )
    assert not any(record.name == "sqlglot" for record in caplog.records)


def test_quoted_at_identifier_uses_parser_marker_not_text_prefix(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{"quoted.sql": "CREATE TABLE [@Persistent] (id int)"},
    )
    assert "@Persistent" in _symbols(result)
    assert not result.diagnostics
