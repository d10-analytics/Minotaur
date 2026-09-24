"""Behavioral proof for the unregistered AST-authoritative SQL analyzer."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
import sqlglot

from minotaur.config import SqlSettings
from minotaur.language_interpreter.contract import DiagnosticCode, DiagnosticSeverity
from minotaur.language_interpreter.sql import analyze_sql_files
from minotaur.language_interpreter.sql import interpreter as sql_interpreter
from minotaur.language_interpreter.workspace import Workspace


def _analyze(tmp_path: Path, *, settings: SqlSettings | None = None, **files: bytes | str):
    paths: list[Path] = []
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        paths.append(path)
    return analyze_sql_files(Workspace(tmp_path), tuple(paths), settings=settings)


def _symbols(result):
    return {node.label: node for node in result.document.nodes if node.symbol_kind}


def _edges(result, kind: str):
    labels = {node.id: node.label for node in result.document.nodes}
    return {
        (labels[edge.source], labels[edge.target])
        for edge in result.document.relationships
        if edge.kind == kind
    }


def _fk_components(result):
    return result.document.extensions["minotaur-sql"]["fk_components"]


def _fk_component_ids(result):
    return {
        node.label: node.extensions["minotaur-sql"]["fk_component"]
        for node in result.document.nodes
        if node.symbol_kind == "sql:table"
    }


def test_view_cycle_warnings_are_canonical_and_order_independent(tmp_path: Path) -> None:
    first = _analyze(
        tmp_path / "first",
        **{
            "b.sql": "CREATE VIEW b AS SELECT * FROM a\n",
            "a.sql": "CREATE VIEW a AS SELECT * FROM b\n",
        },
    )
    second = _analyze(
        tmp_path / "second",
        **{
            "a.sql": "CREATE VIEW a AS SELECT * FROM b\n",
            "b.sql": "CREATE VIEW b AS SELECT * FROM a\n",
        },
    )
    expected = [
        (item.code, item.severity, item.extensions, item.message) for item in first.diagnostics
    ]
    assert expected == [
        (item.code, item.severity, item.extensions, item.message) for item in second.diagnostics
    ]
    assert len(first.warnings) == 1
    assert first.warnings[0].code is DiagnosticCode.CIRCULAR_DEPENDENCY
    assert first.warnings[0].severity is DiagnosticSeverity.WARNING
    assert first.warnings[0].extensions["minotaur-sql"]["path"] == ("a", "b", "a")


def test_self_and_overlapping_view_cycles_are_complete_and_order_independent(
    tmp_path: Path,
) -> None:
    files = {
        "self.sql": "CREATE VIEW self_view AS SELECT * FROM self_view\n",
        "a.sql": "CREATE VIEW a AS SELECT * FROM b\n",
        "b.sql": ("CREATE VIEW b AS SELECT * FROM a UNION ALL SELECT * FROM c\n"),
        "c.sql": "CREATE VIEW c AS SELECT * FROM a\n",
    }
    first = _analyze(tmp_path / "first", **files)
    second = _analyze(
        tmp_path / "second",
        **{name: files[name] for name in reversed(files)},
    )

    def cycle_paths(result) -> list[tuple[str, ...]]:
        return [
            item.extensions["minotaur-sql"]["path"]
            for item in result.warnings
            if item.code is DiagnosticCode.CIRCULAR_DEPENDENCY
        ]

    expected = [
        ("a", "b", "a"),
        ("a", "b", "c", "a"),
        ("self_view", "self_view"),
    ]
    assert cycle_paths(first) == expected
    assert cycle_paths(second) == expected
    assert [item.code for item in first.warnings] == [
        DiagnosticCode.CIRCULAR_DEPENDENCY,
        DiagnosticCode.CIRCULAR_DEPENDENCY,
        DiagnosticCode.CIRCULAR_DEPENDENCY,
    ]
    assert all(item.severity is DiagnosticSeverity.WARNING for item in first.warnings)


def test_default_view_depth_boundary_reports_only_the_full_depth_four_path(
    tmp_path: Path,
) -> None:
    result = _analyze(
        tmp_path,
        **{
            "depth.sql": (
                "CREATE TABLE base (id int)\nGO\n"
                "CREATE VIEW depth_one AS SELECT * FROM base\nGO\n"
                "CREATE VIEW depth_two AS SELECT * FROM depth_one\nGO\n"
                "CREATE VIEW depth_three AS SELECT * FROM depth_two\nGO\n"
                "CREATE VIEW depth_four AS SELECT * FROM depth_three\n"
            )
        },
    )

    depth_warnings = [
        item for item in result.warnings if item.code is DiagnosticCode.VIEW_DEPTH_WARNING
    ]
    assert len(depth_warnings) == 1
    assert depth_warnings[0].severity is DiagnosticSeverity.WARNING
    assert depth_warnings[0].extensions["minotaur-sql"] == {
        "depth": 4,
        "path": ("depth_four", "depth_three", "depth_two", "depth_one", "base"),
    }
    assert "depth_three" not in {
        item.extensions["minotaur-sql"]["path"][0] for item in depth_warnings
    }


def test_cyclic_views_and_branches_entering_cycles_have_no_depth_warning(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cycles.sql"
    source.write_text(
        "CREATE VIEW cycle_a AS SELECT * FROM cycle_b\nGO\n"
        "CREATE VIEW cycle_b AS SELECT * FROM cycle_a\nGO\n"
        "CREATE VIEW enters_cycle AS SELECT * FROM cycle_a\nGO\n"
        "CREATE VIEW root_view AS SELECT * FROM enters_cycle\n",
        encoding="utf-8",
    )
    result = analyze_sql_files(
        Workspace(tmp_path),
        (source,),
        SqlSettings(view_depth_threshold=1),
    )

    assert [
        item.extensions["minotaur-sql"]["path"]
        for item in result.warnings
        if item.code is DiagnosticCode.CIRCULAR_DEPENDENCY
    ] == [("cycle_a", "cycle_b", "cycle_a")]
    assert not [item for item in result.warnings if item.code is DiagnosticCode.VIEW_DEPTH_WARNING]


def test_view_depth_warning_keeps_fk_and_path_metadata(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "schema.sql": (
                "CREATE TABLE parent (id int)\nGO\n"
                "CREATE TABLE child (parent_id int REFERENCES parent(id))\nGO\n"
                "CREATE VIEW middle AS SELECT * FROM child\nGO\n"
                "CREATE VIEW top_view AS SELECT * FROM middle\n"
            )
        },
    )
    result = analyze_sql_files(
        Workspace(tmp_path), (tmp_path / "schema.sql",), SqlSettings(view_depth_threshold=1)
    )
    warning = next(
        item for item in result.warnings if item.code is DiagnosticCode.VIEW_DEPTH_WARNING
    )
    assert warning.extensions["minotaur-sql"] == {
        "depth": 2,
        "path": ("top_view", "middle", "child"),
    }
    components = result.document.extensions["minotaur-sql"]["fk_components"]
    assert len(components) == 1
    assert components[0]["id"] == 0
    assert components[0]["size"] == 2
    table_ids = sorted(node.id for node in result.document.nodes if node.symbol_kind == "sql:table")
    assert components[0]["members"] == tuple(table_ids)
    assert all(
        node.extensions["minotaur-sql"]["fk_component"] == 0
        for node in result.document.nodes
        if node.symbol_kind == "sql:table"
    )


def test_unresolved_view_terminal_is_one_final_generic_hop(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "views.sql": (
                "CREATE VIEW root_view AS SELECT * FROM leaf_view\nGO\n"
                "CREATE VIEW leaf_view AS SELECT * FROM missing_table\n"
            )
        },
    )
    result = analyze_sql_files(
        Workspace(tmp_path), (tmp_path / "views.sql",), SqlSettings(view_depth_threshold=1)
    )
    warning = next(
        item for item in result.warnings if item.code is DiagnosticCode.VIEW_DEPTH_WARNING
    )
    assert warning.extensions["minotaur-sql"]["path"] == (
        "root_view",
        "leaf_view",
        "missing_table",
    )


def test_unrelated_table_reference_is_not_a_view_depth_hop(tmp_path: Path) -> None:
    source = tmp_path / "unrelated.sql"
    source.write_text(
        "CREATE TABLE base (id int REFERENCES missing_parent(id))\nGO\n"
        "CREATE VIEW middle_view AS SELECT * FROM base\nGO\n"
        "CREATE VIEW root_view AS SELECT * FROM middle_view\n",
        encoding="utf-8",
    )
    result = analyze_sql_files(
        Workspace(tmp_path),
        (source,),
        SqlSettings(view_depth_threshold=1),
    )

    warnings = [item for item in result.warnings if item.code is DiagnosticCode.VIEW_DEPTH_WARNING]
    assert len(warnings) == 1
    assert warnings[0].extensions["minotaur-sql"]["path"] == (
        "root_view",
        "middle_view",
        "base",
    )


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


def test_fk_components_partition_tables_and_exclude_other_edges(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        **{
            "schema.sql": (
                "CREATE SCHEMA S\nGO\n"
                "CREATE TABLE S.Root (id int)\nGO\n"
                "CREATE TABLE S.Middle (root_id int REFERENCES S.Root(id))\nGO\n"
                "CREATE TABLE S.Leaf (middle_id int REFERENCES S.Middle(id))\nGO\n"
                "CREATE TABLE S.Solo (id int)\nGO\n"
                "CREATE VIEW S.V AS SELECT * FROM S.Leaf\nGO\n"
                "ALTER TABLE S.Middle ADD CONSTRAINT missing_fk FOREIGN KEY (id) "
                "REFERENCES S.Missing(id)"
            )
        },
    )

    assert not result.diagnostics
    components = _fk_components(result)
    assert all(set(component) == {"id", "size", "members"} for component in components)
    assert sorted(component["size"] for component in components) == [1, 3]
    labels = {node.id: node.label for node in result.document.nodes}
    chain = next(component for component in components if component["size"] == 3)
    assert list(chain["members"]) == sorted(chain["members"])
    assert {labels[node_id] for node_id in chain["members"]} == {
        "S.Root",
        "S.Middle",
        "S.Leaf",
    }
    assert _fk_component_ids(result)["S.Solo"] != _fk_component_ids(result)["S.Root"]
    assert _edges(result, "sql:reads-from") == {("S.V", "S.Leaf")}
    assert "S.Missing" in {
        node.label
        for node in result.document.nodes
        if node.node_class.value == "unresolved-reference"
    }
    summary_by_member = {
        member: component for component in components for member in component["members"]
    }
    for node in result.document.nodes:
        if node.symbol_kind == "sql:table":
            component = summary_by_member[node.id]
            assert node.extensions["minotaur-sql"]["fk_component"] == component["id"]
        else:
            assert "fk_component" not in (node.extensions or {}).get("minotaur-sql", {})
    assert all(
        list(component["members"]) == sorted(component["members"]) for component in components
    )


def test_fk_component_order_is_stable_for_equal_groups_and_selection_order(tmp_path: Path) -> None:
    files = {
        "a.sql": (
            "CREATE TABLE A1 (id int)\nGO\n"
            "CREATE TABLE A2 (parent_id int REFERENCES A1(id))\nGO\n"
            "CREATE TABLE SoloA (id int)"
        ),
        "b.sql": (
            "CREATE TABLE B1 (id int)\nGO\n"
            "CREATE TABLE B2 (parent_id int REFERENCES B1(id))\nGO\n"
            "CREATE TABLE SoloB (id int)"
        ),
    }
    forward = _analyze(tmp_path, **files)
    reverse = _analyze(tmp_path, **{"b.sql": files["b.sql"], "a.sql": files["a.sql"]})

    assert _fk_components(forward) == _fk_components(reverse)
    assert _fk_component_ids(forward) == _fk_component_ids(reverse)
    components = _fk_components(forward)
    assert [component["id"] for component in components] == list(range(len(components)))
    assert list(components) == sorted(components, key=lambda item: (-item["size"], item["members"]))


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
    assert len(result.diagnostics) == 2
    assert all(d.code == DiagnosticCode.UNSUPPORTED_SYNTAX for d in result.diagnostics)
    assert _edges(result, "references") == {("unsupported.sql", "T")}


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

    positive_indexes = (
        "CREATE INDEX table_ix ON T(id)",
        "CREATE NONCLUSTERED INDEX nonclustered_ix ON T(id)",
        "CREATE CLUSTERED INDEX clustered_ix ON V(id)",
        "CREATE UNIQUE NONCLUSTERED INDEX unique_nonclustered_ix ON Missing(id)",
        "CREATE UNIQUE CLUSTERED INDEX unique_clustered_ix ON T(id)",
    )
    recognized_kinds = (
        ("INDEX", "plain"),
        ("NONCLUSTERED INDEX", "nonclustered"),
        ("CLUSTERED INDEX", "clustered"),
        ("UNIQUE NONCLUSTERED INDEX", "unique_nonclustered"),
        ("UNIQUE CLUSTERED INDEX", "unique_clustered"),
    )
    invalid_shapes = (
        ("local_temp", "#scratch(id)"),
        ("global_temp", "##scratch(id)"),
        ("variable", "@scratch(id)"),
        ("three_part", "db.S.T(id)"),
        ("empty", "T"),
    )
    invalid_indexes = tuple(
        f"CREATE {kind} {label}_{shape}_ix ON {target}"
        for kind, label in recognized_kinds
        for shape, target in invalid_shapes
    )
    statements = (
        "CREATE SCHEMA S",
        "CREATE TABLE T(id int)",
        "CREATE TABLE S.Qualified(id int)",
        "CREATE VIEW V AS SELECT 1",
        *positive_indexes,
        "CREATE UNIQUE INDEX qualified_ix ON S.Qualified(id DESC) INCLUDE (id)",
        "CREATE INDEX qualified_missing_ix ON S.Missing(id) WITH (FILLFACTOR=80)",
        *invalid_indexes,
    )
    monkeypatch.setattr(sql_interpreter, "_resolve", fail_resolution)
    result = _analyze(
        tmp_path,
        **{"indexes.sql": "\nGO\n".join(statements)},
    )
    assert set(_symbols(result)) == {"S", "T", "S.Qualified", "V"}
    assert [d.code for d in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX] * 25
    assert not _edges(result, "sql:reads-from")
    assert not _edges(result, "sql:foreign-key-to")
    assert not any(
        node.node_class.value == "unresolved-reference" for node in result.document.nodes
    )

    declarations = "CREATE TABLE T(id int)\nGO\nCREATE VIEW V AS SELECT 1"
    for number, statement in enumerate(positive_indexes):
        positive = _analyze(
            tmp_path / f"positive_{number}",
            **{"index.sql": f"{declarations}\nGO\n{statement}"},
        )
        assert not positive.diagnostics, statement

    for number, statement in enumerate(invalid_indexes):
        invalid = _analyze(
            tmp_path / f"invalid_{number}",
            **{"index.sql": statement},
        )
        assert [diagnostic.code for diagnostic in invalid.diagnostics] == [
            DiagnosticCode.UNSUPPORTED_SYNTAX
        ], statement


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


def test_duplicate_declaration_categories_are_complete_and_order_independent(
    tmp_path: Path,
) -> None:
    files = {
        "canonical/mixed.sql": "CREATE TABLE Mixed (id int)\n",
        "migrations/mixed.sql": "CREATE TABLE Mixed (id int)\n",
        "migrations/001.sql": "CREATE TABLE Migrated (id int)\n",
        "migrations/nested/002.sql": "CREATE TABLE Migrated (id int)\n",
        "ordinary/one.sql": "CREATE TABLE Canonical (id int)\n",
        "ordinary/two.sql": "CREATE TABLE Canonical (id int)\n",
    }
    settings = SqlSettings(migration_patterns=("migrations/**/*.sql",))

    first = _analyze(tmp_path / "first", settings=settings, **files)
    second = _analyze(
        tmp_path / "second",
        settings=settings,
        **dict(reversed(tuple(files.items()))),
    )

    expected_categories = {
        "canonical/mixed.sql": "canonical-and-migration",
        "migrations/mixed.sql": "canonical-and-migration",
        "migrations/001.sql": "multi-migration",
        "migrations/nested/002.sql": "multi-migration",
        "ordinary/one.sql": "multi-canonical",
        "ordinary/two.sql": "multi-canonical",
    }
    expected_ranges = {
        "canonical/mixed.sql": (0, 13, 0, 18),
        "migrations/mixed.sql": (0, 13, 0, 18),
        "migrations/001.sql": (0, 13, 0, 21),
        "migrations/nested/002.sql": (0, 13, 0, 21),
        "ordinary/one.sql": (0, 13, 0, 22),
        "ordinary/two.sql": (0, 13, 0, 22),
    }

    def duplicate_warnings(result):
        return [
            item for item in result.warnings if item.code is DiagnosticCode.DUPLICATE_DECLARATION
        ]

    first_duplicates = duplicate_warnings(first)
    assert not first.errors
    assert not second.errors
    assert len(first_duplicates) == len(expected_categories)
    assert first.diagnostics == second.diagnostics
    assert first.document == second.document
    assert [(item.path, item.location.sort_key) for item in first_duplicates] == sorted(
        (item.path, item.location.sort_key) for item in first_duplicates
    )
    assert [
        (
            item.path,
            item.location.range.start.line,
            item.location.range.start.character,
            item.location.range.end.line,
            item.location.range.end.character,
        )
        for item in first_duplicates
    ] == [(path, *expected_ranges[path]) for path in sorted(expected_ranges)]
    assert [(item.path, item.extensions) for item in first_duplicates] == [
        (path, {"minotaur-sql": {"category": expected_categories[path]}})
        for path in sorted(expected_categories)
    ]
    assert all(item.severity is DiagnosticSeverity.WARNING for item in first_duplicates)

    malformed = _analyze(
        tmp_path / "malformed",
        settings=settings,
        **{**files, "broken.sql": "CREATE TABLE Broken ("},
    )
    assert [item.code for item in malformed.errors] == [DiagnosticCode.PARSE_ERROR]
    assert len(duplicate_warnings(malformed)) == len(expected_categories)
    assert any(node.label == "Mixed" for node in malformed.document.nodes)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("migrations/*.sql", "canonical-and-migration"),
        ("*.sql", "multi-canonical"),
    ],
)
def test_duplicate_categories_use_whole_path_component_matching(
    tmp_path: Path, pattern: str, expected: str
) -> None:
    result = _analyze(
        tmp_path,
        settings=SqlSettings(migration_patterns=(pattern,)),
        **{
            "migrations/001.sql": "CREATE TABLE Shared (id int)\n",
            "migrations/nested/002.sql": "CREATE TABLE Shared (id int)\n",
        },
    )

    assert not result.errors
    duplicate_warnings = [
        item for item in result.warnings if item.code is DiagnosticCode.DUPLICATE_DECLARATION
    ]
    assert len(duplicate_warnings) == 2
    assert all(
        item.extensions == {"minotaur-sql": {"category": expected}} for item in duplicate_warnings
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


def _sql_edges(result, kind: str):
    nodes = {node.id: node for node in result.document.nodes}
    return [
        (nodes[edge.source], nodes[edge.target], edge)
        for edge in result.document.relationships
        if edge.kind == kind
    ]


def _evidence_locations(edge):
    return {
        (location.path, location.range.start.line, location.range.start.character)
        for item in edge.evidence
        for location in item.locations
    }


def test_fk_column_details_preserve_ast_forms_order_and_irregular_locations(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE [S].[Parent] ([X Col] int, [Y Col] int)
GO
CREATE TABLE [S].[Child] (
 [Local A] int,
 [Local B] int,
 [R] int,
 CONSTRAINT fk_table FOREIGN KEY ([Local A],[Local B]) REFERENCES [S].[Parent]([X Col],[Y Col]),
 CONSTRAINT fk_reverse FOREIGN KEY ([Local B],[Local A]) REFERENCES [S].[Parent]([Y Col],[X Col]),
 CONSTRAINT fk_duplicate FOREIGN KEY ([Local A],[Local B]) REFERENCES [S].[Parent]([X Col],[Y Col]),
 CONSTRAINT fk_repeat FOREIGN KEY ([R],[R]) REFERENCES [S].[Parent]([X Col],[X Col]),
 CONSTRAINT fk_empty FOREIGN KEY () REFERENCES [S].[Parent](),
 CONSTRAINT fk_unequal FOREIGN KEY ([R],[R]) REFERENCES [S].[Parent]([X Col])
)
GO
CREATE TABLE [S].[InlineChild] ([Local A] int REFERENCES [S].[Parent]([X Col]))
"""
    result = _analyze(tmp_path, **{"tables.sql": sql})

    assert not result.diagnostics
    edges = _sql_edges(result, "sql:foreign-key-to")
    child_edge = next(
        edge for edge in edges if edge[0].label == "S.Child" and edge[1].label == "S.Parent"
    )[2]
    assert len(child_edge.evidence) == 4
    extensions = [
        item.to_dict()["extensions"] for item in child_edge.evidence if item.extensions is not None
    ]
    assert extensions == [
        {
            "minotaur-sql": {
                "foreign_key_columns": [
                    {"local": "Local A", "referenced": "X Col"},
                    {"local": "Local B", "referenced": "Y Col"},
                ]
            }
        },
        {
            "minotaur-sql": {
                "foreign_key_columns": [
                    {"local": "Local B", "referenced": "Y Col"},
                    {"local": "Local A", "referenced": "X Col"},
                ]
            }
        },
        {
            "minotaur-sql": {
                "foreign_key_columns": [
                    {"local": "R", "referenced": "X Col"},
                    {"local": "R", "referenced": "X Col"},
                ]
            }
        },
    ]
    assert all(set(extension) == {"minotaur-sql"} for extension in extensions)
    payload_free = [item for item in child_edge.evidence if item.extensions is None]
    assert len(payload_free) == 1
    assert {
        (location.path, location.range.start.line, location.range.start.character)
        for location in payload_free[0].locations
    } == {
        ("tables.sql", 10, sql.splitlines()[10].index("[S].[Parent]")),
        ("tables.sql", 11, sql.splitlines()[11].index("[S].[Parent]")),
    }
    inline_edge = next(
        edge for edge in edges if edge[0].label == "S.InlineChild" and edge[1].label == "S.Parent"
    )[2]
    assert [item.to_dict()["extensions"] for item in inline_edge.evidence] == [
        {"minotaur-sql": {"foreign_key_columns": [{"local": "Local A", "referenced": "X Col"}]}}
    ]


def test_standalone_fk_details_exclude_names_and_modifiers(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE Parent (x int, y int)
GO
CREATE TABLE Child (a int, b int)
GO
ALTER TABLE Child ADD CONSTRAINT [fk decorated] FOREIGN KEY (a,b)
REFERENCES Parent(x,y) ON DELETE CASCADE ON UPDATE SET NULL NOT FOR REPLICATION
"""
    result = _analyze(tmp_path, **{"standalone.sql": sql})

    assert not result.diagnostics
    edges = _sql_edges(result, "sql:foreign-key-to")
    assert len(edges) == 1
    edge = edges[0][2]
    assert [item.to_dict()["extensions"] for item in edge.evidence] == [
        {
            "minotaur-sql": {
                "foreign_key_columns": [
                    {"local": "a", "referenced": "x"},
                    {"local": "b", "referenced": "y"},
                ]
            }
        }
    ]
    assert "fk decorated" not in str(edge.to_dict())
    assert "ON DELETE" not in str(edge.to_dict())
    assert "ON UPDATE" not in str(edge.to_dict())
    assert "NOT FOR REPLICATION" not in str(edge.to_dict())


def test_equal_fk_mappings_merge_locations_across_selected_files(tmp_path: Path) -> None:
    declaration = "CREATE TABLE Parent (x int, y int)\nGO\nCREATE TABLE Child (a int, b int)"
    alter = "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (a,b) REFERENCES Parent(x,y)"
    result = _analyze(
        tmp_path,
        **{"declarations.sql": declaration, "first.sql": alter, "second.sql": alter},
    )

    assert not result.diagnostics
    edges = _sql_edges(result, "sql:foreign-key-to")
    assert len(edges) == 1
    evidence = edges[0][2].evidence
    assert len(evidence) == 1
    assert evidence[0].to_dict()["extensions"] == {
        "minotaur-sql": {
            "foreign_key_columns": [
                {"local": "a", "referenced": "x"},
                {"local": "b", "referenced": "y"},
            ]
        }
    }
    assert {location.path for location in evidence[0].locations} == {"first.sql", "second.sql"}


def test_irregular_and_unresolved_fk_evidence_has_no_mapping_payload(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE Parent (x int, y int)
GO
CREATE TABLE Child (a int, b int)
GO
ALTER TABLE Child ADD CONSTRAINT fk_empty FOREIGN KEY () REFERENCES Parent()
GO
ALTER TABLE Child ADD CONSTRAINT fk_unequal FOREIGN KEY (a,b) REFERENCES Parent(x)
GO
ALTER TABLE MissingChild ADD CONSTRAINT fk_missing FOREIGN KEY (a) REFERENCES Parent(x)
GO
ALTER TABLE Child ADD CONSTRAINT fk_missing_target FOREIGN KEY (a) REFERENCES MissingParent(x)
"""
    result = _analyze(tmp_path, **{"irregular.sql": sql})

    edges = _sql_edges(result, "sql:foreign-key-to")
    assert len(edges) == 1
    assert all(item.extensions is None for item in edges[0][2].evidence)
    assert _evidence_locations(edges[0][2]) == {
        ("irregular.sql", 4, sql.splitlines()[4].index("Parent")),
        ("irregular.sql", 6, sql.splitlines()[6].index("Parent")),
    }
    assert not any(
        edge[0].label == "MissingChild" or edge[1].label == "MissingParent"
        for edge in _sql_edges(result, "sql:foreign-key-to")
    )
    unresolved_edges = _sql_edges(result, "references")
    assert len(unresolved_edges) == 2
    assert all(item.extensions is None for _, _, edge in unresolved_edges for item in edge.evidence)


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_standalone_fk_resolves_after_all_files_case_insensitively(
    tmp_path: Path, qualified: bool, reverse: bool
) -> None:
    prefix = "dbo." if qualified else ""
    declaration = (
        f"CREATE TABLE {prefix}Parent (Id int)\nGO\nCREATE TABLE {prefix}Child (ParentId int)"
    )
    alter = (
        f"ALTER TABLE {prefix.lower()}child ADD CONSTRAINT FK_child_parent "
        f"FOREIGN KEY (ParentId) REFERENCES {prefix.lower()}parent(Id)"
    )
    files = [("declarations.sql", declaration), ("alter.sql", alter)]
    if reverse:
        files.reverse()
    result = _analyze(tmp_path, **dict(files))
    edges = _sql_edges(result, "sql:foreign-key-to")
    assert not result.diagnostics
    assert len(edges) == 1
    assert (edges[0][0].label, edges[0][1].label) == (f"{prefix}Child", f"{prefix}Parent")
    assert _evidence_locations(edges[0][2]) == {
        ("alter.sql", 0, alter.rindex(f"{prefix.lower()}parent"))
    }


def test_standalone_fks_coalesce_and_retain_both_references(tmp_path: Path) -> None:
    sql = (
        "CREATE TABLE Parent (Id int)\nGO\nCREATE TABLE Child (A int, B int)\nGO\n"
        "ALTER TABLE Child ADD CONSTRAINT FK_A FOREIGN KEY (A) REFERENCES Parent(Id), "
        "CONSTRAINT FK_B FOREIGN KEY (B) REFERENCES Parent(Id)"
    )
    result = _analyze(tmp_path, **{"fks.sql": sql})
    edges = _sql_edges(result, "sql:foreign-key-to")
    assert not result.diagnostics
    assert len(edges) == 1
    assert (edges[0][0].label, edges[0][1].label) == ("Child", "Parent")
    assert _evidence_locations(edges[0][2]) == {
        ("fks.sql", 4, sql.splitlines()[4].index("Parent")),
        ("fks.sql", 4, sql.splitlines()[4].rindex("Parent")),
    }


@pytest.mark.parametrize("ambiguous", [False, True])
def test_standalone_fk_missing_or_ambiguous_source_is_file_origin(
    tmp_path: Path, ambiguous: bool
) -> None:
    declarations = "CREATE TABLE Parent (Id int)"
    if ambiguous:
        declarations += "\nGO\nCREATE TABLE Child (Id int)\nGO\nCREATE TABLE Child (Id int)"
    sql = "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id)"
    result = _analyze(tmp_path, **{"declarations.sql": declarations, "alter.sql": sql})
    unresolved = _sql_edges(result, "references")
    assert len(unresolved) == 1
    assert unresolved[0][0].id == next(
        node.id for node in result.document.nodes if node.path == "alter.sql"
    )
    assert unresolved[0][1].label == "Child"
    assert _evidence_locations(unresolved[0][2]) == {("alter.sql", 0, 12)}
    assert not _sql_edges(result, "sql:foreign-key-to")
    assert [d.code for d in result.diagnostics].count(DiagnosticCode.AMBIGUOUS_REFERENCE) == int(
        ambiguous
    )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_standalone_fk_missing_or_ambiguous_target_is_table_origin(
    tmp_path: Path, ambiguous: bool
) -> None:
    declarations = "CREATE TABLE Child (Id int)"
    if ambiguous:
        declarations += "\nGO\nCREATE TABLE Parent (Id int)\nGO\nCREATE TABLE Parent (Id int)"
    sql = "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id)"
    result = _analyze(tmp_path, **{"declarations.sql": declarations, "alter.sql": sql})
    unresolved = _sql_edges(result, "references")
    assert len(unresolved) == 1
    assert unresolved[0][0].label == "Child"
    assert unresolved[0][1].label == "Parent"
    assert _evidence_locations(unresolved[0][2]) == {("alter.sql", 0, sql.index("Parent"))}
    assert not _sql_edges(result, "sql:foreign-key-to")
    assert [d.code for d in result.diagnostics].count(DiagnosticCode.AMBIGUOUS_REFERENCE) == int(
        ambiguous
    )


@pytest.mark.parametrize(
    "alter",
    [
        "ALTER TABLE #Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id)",
        "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES #Parent(Id)",
        "ALTER TABLE db.dbo.Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id)",
        "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES db.dbo.Parent(Id)",
        "ALTER TABLE Child ADD FOREIGN KEY (Id) REFERENCES Parent(Id)",
        "IF 1 = 1 ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id)",
        "ALTER TABLE Child ADD CONSTRAINT PK_Child PRIMARY KEY (Id)",
        "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id), "
        "CONSTRAINT PK_Child PRIMARY KEY (Id)",
    ],
)
def test_rejected_standalone_alter_is_atomic(tmp_path: Path, alter: str) -> None:
    result = _analyze(
        tmp_path,
        **{
            "tables.sql": "CREATE TABLE Parent (Id int)\nGO\nCREATE TABLE Child (Id int)",
            "alter.sql": alter,
        },
    )
    assert [d.code for d in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
    assert not _sql_edges(result, "sql:foreign-key-to")
    assert not _sql_edges(result, "references")
    assert set(_symbols(result)) == {"Parent", "Child"}


@pytest.mark.parametrize(
    "options",
    [
        "ON DELETE CASCADE ON UPDATE SET NULL NOT FOR REPLICATION",
        "on delete cascade on update set null not for replication",
    ],
)
def test_decorated_standalone_fk_emits_payload_free_edge(tmp_path: Path, options: str) -> None:
    sql = (
        "CREATE TABLE Parent (Id int)\nGO\nCREATE TABLE Child (Id int)\nGO\n"
        "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id) "
        f"{options}"
    )
    result = _analyze(tmp_path, **{"decorated.sql": sql})
    edges = _sql_edges(result, "sql:foreign-key-to")
    assert not result.diagnostics
    assert len(edges) == 1
    assert (edges[0][0].label, edges[0][1].label) == ("Child", "Parent")
    assert edges[0][2].extensions is None
    assert _evidence_locations(edges[0][2]) == {
        ("decorated.sql", 4, sql.splitlines()[4].index("Parent"))
    }


@pytest.mark.parametrize(
    "option",
    [
        "MATCH FULL",
        "DEFERRABLE",
        "ON DELETE CASCADE ON DELETE SET NULL",
        "ON UPDATE CASCADE ON UPDATE NO ACTION",
        "on delete cascade ON DELETE SET NULL",
    ],
)
def test_standalone_fk_rejects_unapproved_or_duplicate_reference_options(
    tmp_path: Path, option: str
) -> None:
    alter = f"ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id) {option}"
    result = _analyze(
        tmp_path,
        **{
            "tables.sql": "CREATE TABLE Parent (Id int)\nGO\nCREATE TABLE Child (Id int)",
            "alter.sql": alter,
        },
    )
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.UNSUPPORTED_SYNTAX
    ]
    assert not _sql_edges(result, "sql:foreign-key-to")
    assert not _sql_edges(result, "references")
    assert set(_symbols(result)) == {"Parent", "Child"}


def test_standalone_fk_parse_failure_discards_batch_and_later_batch_recovers(
    tmp_path: Path,
) -> None:
    sql = (
        "CREATE TABLE Parent (Id int)\nGO\n"
        "CREATE TABLE Child (Id int)\nGO\n"
        "ALTER TABLE Child ADD CONSTRAINT FK_lost FOREIGN KEY (Id) REFERENCES Parent(Id);\n"
        "CREATE TABLE Broken (\nGO\n"
        "ALTER TABLE Child ADD CONSTRAINT FK_kept FOREIGN KEY (Id) REFERENCES Parent(Id)\nGO\n"
        "ALTER TABLE Child ADD ExtraColumn int"
    )
    result = _analyze(tmp_path, **{"catalog.sql": sql})

    assert [diagnostic.code for diagnostic in result.diagnostics] == [DiagnosticCode.PARSE_ERROR]
    assert set(_symbols(result)) == {"Parent", "Child"}
    edges = _sql_edges(result, "sql:foreign-key-to")
    assert len(edges) == 1
    assert (edges[0][0].label, edges[0][1].label) == ("Child", "Parent")
    assert _evidence_locations(edges[0][2]) == {
        ("catalog.sql", 7, sql.splitlines()[7].index("Parent"))
    }
    assert not _sql_edges(result, "references")
    components = _fk_components(result)
    assert len(components) == 1
    assert components[0]["size"] == 2
    assert _fk_component_ids(result)["Parent"] == _fk_component_ids(result)["Child"]


def test_standalone_fk_rejects_duplicate_replication_modifier_atomically(tmp_path: Path) -> None:
    alter = (
        "ALTER TABLE Child ADD CONSTRAINT FK FOREIGN KEY (Id) REFERENCES Parent(Id) "
        "NOT FOR REPLICATION NOT FOR REPLICATION"
    )
    result = _analyze(
        tmp_path,
        **{
            "tables.sql": "CREATE TABLE Parent (Id int)\nGO\nCREATE TABLE Child (Id int)",
            "alter.sql": alter,
        },
    )
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.UNSUPPORTED_SYNTAX
    ]
    assert not _sql_edges(result, "sql:foreign-key-to")
    assert not _sql_edges(result, "references")
    assert set(_symbols(result)) == {"Parent", "Child"}


@pytest.mark.parametrize(
    ("declaration", "name"),
    [
        ("CREATE SCHEMA [schema.with.dot]", "schema.with.dot"),
        ("CREATE TABLE [schema.with.dot].[table.with.dot] (id int)", "table.with.dot"),
        ('CREATE VIEW "S"."view.with.dot" AS SELECT 1 AS id', "view.with.dot"),
        ("CREATE TABLE S.MixedCase (id int)", "MixedCase"),
    ],
)
def test_declaration_final_identifier_survives_graph_serialization(
    tmp_path: Path, declaration: str, name: str
) -> None:
    from minotaur.graph_model.node import Node

    result = _analyze(tmp_path, **{"names.sql": declaration})
    node = next(iter(_symbols(result).values()))
    restored = Node.from_dict(node.to_dict())
    expected = {"minotaur-sql": {"name": name}}
    if node.symbol_kind == "sql:table":
        expected["minotaur-sql"]["fk_component"] = 0
    assert restored.extensions == expected
    assert restored.id == node.id
