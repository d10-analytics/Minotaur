"""Behavioral coverage for SQL table and view unreferenced results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.query import system as system_query


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _analyze(root: Path, output: Path) -> None:
    assert cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)]) == 0


def _query_json(
    graph: Path,
    root: Path,
    capsys: object,
    *arguments: str,
) -> list[dict[str, object]]:
    status = cli.main(
        ["query", "unreferenced", *arguments, "--graph", str(graph), "--root", str(root), "--json"]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 0, captured.err
    return json.loads(captured.out)["results"]


def test_sql_unreferenced_uses_inbound_edges_and_public_filters(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "source"
    _write(
        root,
        "schema.sql",
        """\
CREATE SCHEMA S
GO
CREATE TABLE S.Parent (id int)
GO
CREATE TABLE S.Child (parent_id int)
GO
ALTER TABLE S.Child ADD CONSTRAINT FK_child_parent FOREIGN KEY (parent_id) REFERENCES S.Parent(id)
GO
CREATE VIEW S.Reader AS SELECT * FROM S.Parent
GO
CREATE TABLE S.SelfRef (id int)
GO
ALTER TABLE S.SelfRef ADD CONSTRAINT FK_self FOREIGN KEY (id) REFERENCES S.SelfRef(id)
GO
CREATE VIEW S.ViewTarget AS SELECT 1 AS id
GO
CREATE VIEW S.ViewConsumer AS SELECT * FROM S.ViewTarget
GO
CREATE TABLE S.test_orders (id int)
GO
CREATE TABLE S.__audit__ (id int)
GO
CREATE TABLE S.Unused (id int)
""",
    )
    _write(root, "other.sql", "CREATE TABLE S.Unselected (id int)\n")
    graph = tmp_path / "graph.json"
    _analyze(root, graph)

    records = _query_json(graph, root, capsys, "schema.sql")
    by_symbol = {record["symbol"]: record for record in records}

    assert {(record["symbol"], record["kind"]) for record in records} == {
        ("S.Child", "sql:table"),
        ("S.Reader", "sql:view"),
        ("S.SelfRef", "sql:table"),
        ("S.ViewConsumer", "sql:view"),
        ("S.test_orders", "sql:table"),
        ("S.__audit__", "sql:table"),
        ("S.Unused", "sql:table"),
    }
    assert "S.Parent" not in by_symbol
    assert "S.ViewTarget" not in by_symbol
    assert "S.Unselected" not in by_symbol

    assert "S.Unused" not in {
        record["symbol"]
        for record in _query_json(graph, root, capsys, "schema.sql", "--exclude", "Unused")
    }

    exclusions = tmp_path / "exclude.json"
    exclusions.write_text(json.dumps(["SelfRef"]), encoding="utf-8")
    assert "S.SelfRef" not in {
        record["symbol"]
        for record in _query_json(
            graph, root, capsys, "schema.sql", "--exclude-file", str(exclusions)
        )
    }
    assert "S.test_orders" not in {
        record["symbol"]
        for record in _query_json(
            graph, root, capsys, "schema.sql", "--exclude-pattern", r"S\.test_orders$"
        )
    }


def test_sql_text_fallback_folds_sql_names_and_preserves_python_case_behavior(
    tmp_path: Path, capsys: object
) -> None:
    sql_root = tmp_path / "sql"
    _write(
        sql_root,
        "schema.sql",
        """\
CREATE TABLE S.OnlyDecl (id int)
GO
CREATE TABLE S.CaseThing (id int)
-- casething appears in a legacy note
""",
    )
    sql_graph = tmp_path / "sql.json"
    _analyze(sql_root, sql_graph)
    sql_records = _query_json(sql_graph, sql_root, capsys, "--text-fallback")
    sql_mentions = {record["symbol"]: record["text_mention"] for record in sql_records}
    assert sql_mentions == {"S.CaseThing": True, "S.OnlyDecl": False}

    python_root = tmp_path / "python"
    _write(
        python_root,
        "module.py",
        "def CaseOnly():\n    pass\n\n# caseonly is a different Python name\n",
    )
    python_graph = tmp_path / "python.json"
    _analyze(python_root, python_graph)
    python_records = _query_json(python_graph, python_root, capsys, "--text-fallback")
    assert python_records == [
        {
            "kind": "function",
            "line": 1,
            "path": "module.py",
            "symbol": "module.CaseOnly",
            "text_mention": False,
        }
    ]


def test_sql_function_candidates_keep_unused_and_recursive_functions(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "source"
    _write(
        root,
        "functions.sql",
        """\
CREATE FUNCTION S.Unused() RETURNS int AS RETURN 1
GO
CREATE FUNCTION S.Callee() RETURNS int AS RETURN 1
GO
CREATE VIEW S.ExternalCaller AS SELECT S.Callee() AS value
GO
CREATE TABLE S.Base (id int)
GO
CREATE FUNCTION S.Recur() RETURNS TABLE AS RETURN (SELECT S.Recur() FROM S.Base)
""",
    )
    graph = tmp_path / "graph.json"
    _analyze(root, graph)

    records = _query_json(graph, root, capsys, "functions.sql")
    sql_functions = {record["symbol"] for record in records if record["kind"] == "sql:function"}
    assert sql_functions == {"S.Unused", "S.Recur"}
    assert "S.Callee" not in sql_functions


def test_system_query_docstrings_name_registry_relative_sql_kinds() -> None:
    docstrings = (
        system_query.__doc__,
        system_query.surface.__doc__,
        system_query.consumers.__doc__,
        system_query.system_deps.__doc__,
    )
    assert all(
        doc is not None and "current SQL dependency" in " ".join(doc.split()) for doc in docstrings
    )
    assert "CURRENT_SQL_DEPENDENCY_KINDS" in (system_query.__doc__ or "")
    assert "sql:reads-from`` / ``sql:foreign-key-to" not in (system_query.__doc__ or "")


def test_sql_quoted_dot_identifier_preserves_final_name_for_fallback_and_exclusion(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "sql"
    _write(
        root,
        "schema.sql",
        "CREATE TABLE [schema.with.dot].[table.with.dot] (id int)\n",
    )
    graph = tmp_path / "sql.json"
    _analyze(root, graph)

    assert _query_json(graph, root, capsys, "--text-fallback") == [
        {
            "kind": "sql:table",
            "line": 1,
            "path": "schema.sql",
            "symbol": "schema.with.dot.table.with.dot",
            "text_mention": False,
        }
    ]
    assert _query_json(graph, root, capsys, "--exclude", "table.with.dot") == []


@pytest.mark.parametrize(
    ("declaration", "old_name", "label"),
    [
        ("CREATE TABLE S.Old (id int)", "Old", "S.Old"),
        (
            "CREATE TABLE [schema.with.dot].[table.with.dot] (id int)",
            "table.with.dot",
            "schema.with.dot.table.with.dot",
        ),
        (
            'CREATE VIEW "schema.with.dot"."view.with.dot" AS SELECT 1 AS id',
            "view.with.dot",
            "schema.with.dot.view.with.dot",
        ),
    ],
)
@pytest.mark.parametrize("missing_source", [False, True])
def test_sql_saved_names_survive_source_changes_without_refresh(
    tmp_path: Path,
    capsys: object,
    declaration: str,
    old_name: str,
    label: str,
    missing_source: bool,
) -> None:
    root = tmp_path / "sql"
    _write(root, "schema.sql", declaration + "\n")
    graph = tmp_path / "sql.json"
    _analyze(root, graph)
    assert _query_json(graph, root, capsys, "--exclude", old_name) == []
    if missing_source:
        (root / "schema.sql").unlink()
    else:
        _write(root, "schema.sql", "CREATE TABLE S.New (id int)\n")

    records = _query_json(
        graph, root, capsys, "--no-refresh", "--exclude", "New", "--text-fallback"
    )
    assert [(record["symbol"], record["text_mention"]) for record in records] == [(label, False)]
    assert _query_json(graph, root, capsys, "--no-refresh", "--exclude", old_name) == []
