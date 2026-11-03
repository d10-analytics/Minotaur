"""Behavioral proof for the unregistered AST-authoritative SQL analyzer."""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import pytest
import sqlglot

from minotaur.config import SqlSettings
from minotaur.graph_model.identity import NodeIdentity
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import IdentityBasis, NodeClass
from minotaur.language_interpreter.contract import DiagnosticCode, DiagnosticSeverity
from minotaur.language_interpreter.sql import analyze_sql_files, diagnostics
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


def _count_module_calls(module_file: str, action: Callable[[], object]) -> tuple[object, int]:
    """Run action, counting Python calls whose code lives in module_file."""
    calls = 0

    def profiler(frame: object, event: str, _arg: object) -> None:
        nonlocal calls
        if event == "call" and frame.f_code.co_filename == module_file:  # type: ignore[attr-defined]
            calls += 1

    sys.setprofile(profiler)
    try:
        result = action()
    finally:
        sys.setprofile(None)
    return result, calls


def test_layered_views_report_depth_warnings_in_bounded_calls(tmp_path: Path) -> None:
    statements = ["CREATE TABLE dbo.base (x int)"]
    statements += [f"CREATE VIEW v0_{j} AS SELECT x FROM dbo.base" for j in (0, 1)]
    for layer in range(1, 14):
        for j in (0, 1):
            statements.append(
                f"CREATE VIEW v{layer}_{j} AS SELECT x FROM v{layer - 1}_0 "
                f"UNION ALL SELECT x FROM v{layer - 1}_1"
            )
    source = tmp_path / "layers.sql"
    source.write_text("\nGO\n".join(statements) + "\n", encoding="utf-8")

    result, calls = _count_module_calls(
        diagnostics.__file__,
        lambda: analyze_sql_files(
            Workspace(tmp_path), (source,), SqlSettings(view_depth_threshold=13)
        ),
    )

    tail = " -> v12_1 -> v11_1 -> v10_1 -> v9_1 -> v8_1 -> v7_1 -> v6_1 -> v5_1 -> v4_1"
    tail += " -> v3_1 -> v2_1 -> v1_1 -> v0_1 -> dbo.base"
    assert sorted(
        (item.code, item.message)
        for item in result.warnings  # type: ignore[attr-defined]
    ) == sorted(
        (
            DiagnosticCode.VIEW_DEPTH_WARNING,
            f"view dependency depth 14 exceeds threshold 13: {first}{tail}",
        )
        for first in ("v13_0", "v13_1")
    )
    # Measured 697 calls on Python 3.12.3; bound is 3x rounded up to 1,000.
    assert calls <= 3_000


def test_cycle_search_over_acyclic_layers_is_bounded() -> None:
    ids = [f"{layer:02d}_{j}" for layer in range(16) for j in (0, 1)]
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, "minotaur-sql")
    location = Location("views.sql", Range(Position(1, 0), Position(1, 1)))
    views = {
        node_id: Node(
            id="node:sha256:" + hashlib.sha256(node_id.encode()).hexdigest(),
            identity=identity,
            node_class=NodeClass.SYMBOL,
            label=node_id,
            symbol_kind="sql:view",
            location=location,
        )
        for node_id in ids
    }
    edges = {
        f"{layer:02d}_{j}": (f"{layer + 1:02d}_0", f"{layer + 1:02d}_1")
        for layer in range(15)
        for j in (0, 1)
    }

    result, calls = _count_module_calls(
        diagnostics.__file__, lambda: diagnostics._elementary_cycles(edges, views)
    )

    assert result == ()
    # Measured 2 calls on Python 3.12.3; bound is 3x rounded up to 1,000.
    assert calls <= 1_000


def test_cycle_and_branch_view_warnings_keep_literal_output(tmp_path: Path) -> None:
    source = tmp_path / "parity.sql"
    source.write_text(
        "CREATE TABLE dbo.base (x int)\nGO\n"
        "CREATE VIEW cycle_a AS SELECT x FROM cycle_b\nGO\n"
        "CREATE VIEW cycle_b AS SELECT x FROM cycle_a\nGO\n"
        "CREATE VIEW leaf AS SELECT x FROM dbo.base\nGO\n"
        "CREATE VIEW mixed AS SELECT x FROM cycle_a UNION ALL SELECT x FROM leaf\nGO\n"
        "CREATE VIEW enters AS SELECT x FROM cycle_b\nGO\n"
        "CREATE VIEW root_view AS SELECT x FROM mixed UNION ALL SELECT x FROM enters\n",
        encoding="utf-8",
    )
    result = analyze_sql_files(Workspace(tmp_path), (source,), SqlSettings(view_depth_threshold=1))

    assert [
        (item.code, item.message, item.extensions["minotaur-sql"]) for item in result.warnings
    ] == [
        (
            DiagnosticCode.CIRCULAR_DEPENDENCY,
            "circular view dependency: cycle_a -> cycle_b -> cycle_a",
            {"path": ("cycle_a", "cycle_b", "cycle_a")},
        ),
        (
            DiagnosticCode.VIEW_DEPTH_WARNING,
            "view dependency depth 2 exceeds threshold 1: mixed -> leaf -> dbo.base",
            {"depth": 2, "path": ("mixed", "leaf", "dbo.base")},
        ),
        (
            DiagnosticCode.VIEW_DEPTH_WARNING,
            "view dependency depth 3 exceeds threshold 1: root_view -> mixed -> leaf -> dbo.base",
            {"depth": 3, "path": ("root_view", "mixed", "leaf", "dbo.base")},
        ),
    ]


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


def test_sqlglot_version_is_pinned_to_exact_release() -> None:
    assert sqlglot.__version__ == "30.18.0"


def test_procedure_and_function_declarations_are_opaque_symbols(tmp_path: Path) -> None:
    sql = """\
CREATE SCHEMA S
GO
CREATE TABLE S.Base (id int)
GO
CREATE PROCEDURE S.GetBase @id int AS SELECT * FROM S.Base
GO
CREATE OR ALTER PROCEDURE S.Refresh AS BEGIN SELECT 1 END
GO
CREATE FUNCTION S.Scalar(@id int) RETURNS int AS RETURN @id
GO
CREATE OR ALTER FUNCTION S.Rows(@id int) RETURNS TABLE AS RETURN (SELECT * FROM S.Base)
GO
CREATE FUNCTION S.Block() RETURNS int AS BEGIN
RETURN 1;
END;
CREATE TABLE S.After (id int)
"""
    result = _analyze(tmp_path, **{"declarations.sql": sql})
    symbols = _symbols(result)

    assert not result.diagnostics
    assert {
        "S",
        "S.Base",
        "S.GetBase",
        "S.Refresh",
        "S.Scalar",
        "S.Rows",
        "S.Block",
        "S.After",
    } == set(symbols)
    assert {symbols[name].symbol_kind for name in symbols if name.startswith("S.")} == {
        "sql:table",
        "sql:procedure",
        "sql:function",
    }
    assert symbols["S.GetBase"].location.range.start.line == 4
    assert symbols["S.GetBase"].location.range.start.character == 17
    assert symbols["S.Scalar"].location.range.start.line == 8
    assert symbols["S.Scalar"].location.range.start.character == 16
    assert _edges(result, "sql:reads-from") == {
        ("S.GetBase", "S.Base"),
        ("S.Rows", "S.Base"),
    }
    assert {edge for edge in _edges(result, "contains") if edge[0] == "declarations.sql"} == {
        ("declarations.sql", "S"),
        ("declarations.sql", "S.Base"),
        ("declarations.sql", "S.GetBase"),
        ("declarations.sql", "S.Refresh"),
        ("declarations.sql", "S.Scalar"),
        ("declarations.sql", "S.Rows"),
        ("declarations.sql", "S.Block"),
        ("declarations.sql", "S.After"),
    }
    assert {edge for edge in _edges(result, "contains") if edge[0] == "S"} == {
        ("S", "S.Base"),
        ("S", "S.After"),
    }


def test_procedure_query_roots_use_the_shared_query_walker_once(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE TABLE S.Joined (id int)
GO
CREATE TABLE S.Other (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
WITH cte AS (SELECT id FROM S.Base)
SELECT * FROM cte
JOIN S.Joined ON cte.id = S.Joined.id
WHERE cte.id IN (SELECT id FROM S.Other)
UNION ALL SELECT * FROM S.Base;
END
"""
    result = _analyze(tmp_path, **{"roots.sql": sql})
    edges = _sql_edges(result, "sql:reads-from")
    labels = {(edge[0].label, edge[1].label) for edge in edges}

    assert not result.diagnostics
    assert labels == {
        ("S.Read", "S.Base"),
        ("S.Read", "S.Joined"),
        ("S.Read", "S.Other"),
    }
    locations = [location for edge in edges for location in edge[2].evidence[0].locations]
    assert len(locations) == len(set(locations))
    assert {
        (location.path, location.range.start.line, location.range.start.character)
        for location in locations
    } == {
        ("roots.sql", 7, sql.splitlines()[7].index("S.Base")),
        ("roots.sql", 9, sql.splitlines()[9].index("S.Joined")),
        ("roots.sql", 10, sql.splitlines()[10].index("S.Other")),
        ("roots.sql", 11, sql.splitlines()[11].index("S.Base")),
    }


def test_procedure_query_root_inside_while_is_retained(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
WHILE 1 = 1 BEGIN
SELECT * FROM S.Base;
END
END
"""
    result = _analyze(tmp_path, **{"loop.sql": sql})

    assert not result.diagnostics
    assert {"S.Base", "S.Read"} <= _symbols(result).keys()
    assert {(edge[0].label, edge[1].label) for edge in _sql_edges(result, "sql:reads-from")} == {
        ("S.Read", "S.Base")
    }


def test_procedure_if_condition_query_root_is_retained_once(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE TABLE S.Other (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
IF EXISTS (SELECT 1 FROM S.Base) SELECT * FROM S.Other;
END
"""
    result = _analyze(tmp_path, **{"condition.sql": sql})
    edges = _sql_edges(result, "sql:reads-from")

    assert not result.diagnostics
    assert {(edge[0].label, edge[0].symbol_kind) for edge in edges} == {("S.Read", "sql:procedure")}
    assert {(edge[1].label, edge[1].symbol_kind) for edge in edges} == {
        ("S.Base", "sql:table"),
        ("S.Other", "sql:table"),
    }
    assert {(edge[0].label, edge[1].label) for edge in edges} == {
        ("S.Read", "S.Base"),
        ("S.Read", "S.Other"),
    }
    assert {location for edge in edges for location in _evidence_locations(edge[2])} == {
        ("condition.sql", 5, sql.splitlines()[5].index("S.Base")),
        ("condition.sql", 5, sql.splitlines()[5].index("S.Other")),
    }


def test_scalar_function_expression_nested_query_root_is_retained(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE FUNCTION S.F() RETURNS int AS RETURN 1 + (SELECT COUNT(*) FROM S.Base)
"""
    result = _analyze(tmp_path, **{"function.sql": sql})
    edges = _sql_edges(result, "sql:reads-from")

    assert not result.diagnostics
    assert [
        (edge[0].label, edge[0].symbol_kind, edge[1].label, edge[1].symbol_kind) for edge in edges
    ] == [("S.F", "sql:function", "S.Base", "sql:table")]
    assert _evidence_locations(edges[0][2]) == {
        ("function.sql", 2, sql.splitlines()[2].index("S.Base"))
    }


def test_query_function_calls_cover_expression_roots_and_self_calls(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE TABLE S.Other (id int)
GO
CREATE FUNCTION S.Calc(@id int) RETURNS int AS RETURN @id
GO
CREATE FUNCTION S.Recur(@id int) RETURNS int AS RETURN (
 SELECT S.Recur(@id) FROM S.Base
)
GO
CREATE FUNCTION S.TVFn(@id int) RETURNS TABLE AS RETURN (SELECT @id AS id)
GO
CREATE VIEW S.Read AS
SELECT S.Calc(b.id), CASE WHEN S.Calc(b.id) > 0 THEN S.Calc(b.id) ELSE 0 END
FROM S.Base b
JOIN S.Other o ON S.Calc(o.id) = o.id
WHERE S.Calc(b.id) > 0
GROUP BY b.id, o.id
HAVING S.Calc(b.id) > 0
  AND b.id IN (SELECT S.Calc(n.id) FROM S.Other n)
GO
CREATE VIEW S.TVRead AS SELECT * FROM S.TVFn(1) JOIN S.Base ON 1 = 1
"""
    result = _analyze(tmp_path, **{"calls.sql": sql})
    call_edges = _sql_edges(result, "sql:calls")
    calls = {(source.label, target.label): edge for source, target, edge in call_edges}

    assert not result.diagnostics
    assert set(calls) == {
        ("S.Read", "S.Calc"),
        ("S.Recur", "S.Recur"),
        ("S.TVRead", "S.TVFn"),
    }
    assert {
        (
            location.path,
            location.range.start.line,
            location.range.start.character,
            location.range.end.line,
            location.range.end.character,
        )
        for location in next(item for item in calls[("S.Read", "S.Calc")].evidence).locations
    } == {
        ("calls.sql", 13, 9, 13, 13),
        ("calls.sql", 13, 33, 13, 37),
        ("calls.sql", 13, 55, 13, 59),
        ("calls.sql", 15, 20, 15, 24),
        ("calls.sql", 16, 8, 16, 12),
        ("calls.sql", 18, 9, 18, 13),
        ("calls.sql", 19, 24, 19, 28),
    }
    assert {
        (
            location.path,
            location.range.start.line,
            location.range.start.character,
            location.range.end.line,
            location.range.end.character,
        )
        for location in next(item for item in calls[("S.Recur", "S.Recur")].evidence).locations
    } == {("calls.sql", 7, 10, 7, 15)}
    assert {
        (
            location.path,
            location.range.start.line,
            location.range.start.character,
            location.range.end.line,
            location.range.end.character,
        )
        for location in next(item for item in calls[("S.TVRead", "S.TVFn")].evidence).locations
    } == {("calls.sql", 21, 40, 21, 44)}
    assert _edges(result, "sql:reads-from") == {
        ("S.Recur", "S.Base"),
        ("S.Read", "S.Base"),
        ("S.Read", "S.Other"),
        ("S.TVRead", "S.Base"),
    }
    assert not {
        (source.label, target.label)
        for source, target, _edge in _sql_edges(result, "sql:reads-from")
        if target.label == "S.TVFn"
    }


def test_ordered_parenthesized_query_roots_record_order_clause_calls(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE dbo.a (x int)
GO
CREATE FUNCTION dbo.gfn(@v int) RETURNS int AS RETURN @v
GO
CREATE FUNCTION f(@id int) RETURNS TABLE AS
RETURN (SELECT x FROM dbo.a) ORDER BY dbo.gfn(x) OFFSET 0 ROWS
GO
CREATE PROCEDURE p AS BEGIN
IF 1 IN ((SELECT x FROM dbo.a) ORDER BY dbo.gfn(x) OFFSET 0 ROWS) BEGIN SELECT x FROM dbo.a; END
END
"""
    result = _analyze(tmp_path, **{"calls.sql": sql})
    call_edges = {
        (source.label, target.label): edge
        for source, target, edge in _sql_edges(result, "sql:calls")
    }
    expected_calls = {
        ("f", "calls.sql", line, character, line, character + len("gfn"))
        for line, character in _reference_positions(sql, "CREATE FUNCTION f", "gfn")
    } | {
        ("p", "calls.sql", line, character, line, character + len("gfn"))
        for line, character in _reference_positions(sql, "CREATE PROCEDURE p", "gfn")
    }
    expected_reads = {
        ("f", "calls.sql", line, character)
        for line, character in _reference_positions(sql, "CREATE FUNCTION f", "dbo.a")
    } | {
        ("p", "calls.sql", line, character)
        for line, character in _reference_positions(sql, "CREATE PROCEDURE p", "dbo.a")
    }

    assert set(_symbols(result)) == {"dbo.a", "dbo.gfn", "f", "p"}
    assert set(call_edges) == {("f", "dbo.gfn"), ("p", "dbo.gfn")}
    assert {
        (
            source.label,
            location.path,
            location.range.start.line,
            location.range.start.character,
            location.range.end.line,
            location.range.end.character,
        )
        for source, _target, edge in _sql_edges(result, "sql:calls")
        for item in edge.evidence
        for location in item.locations
    } == expected_calls
    assert _edges(result, "sql:reads-from") == {("f", "dbo.a"), ("p", "dbo.a")}
    assert {
        (source.label, location.path, location.range.start.line, location.range.start.character)
        for source, _target, edge in _sql_edges(result, "sql:reads-from")
        for item in edge.evidence
        for location in item.locations
    } == expected_reads
    assert not result.diagnostics
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]


def test_rejected_query_root_drops_calls_and_reads_but_keeps_sibling(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE FUNCTION S.Calc(@id int) RETURNS int AS RETURN @id
GO
CREATE PROCEDURE S.Read AS BEGIN
SELECT S.Calc(id) INTO S.Destination FROM S.Base;
SELECT S.Calc(id) FROM S.Base;
END
"""
    result = _analyze(tmp_path, **{"rejected.sql": sql})

    assert _edges(result, "sql:calls") == {("S.Read", "S.Calc")}
    assert _edges(result, "sql:reads-from") == {("S.Read", "S.Base")}
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_rejected_function_shape_drops_procedure_root_reads(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
SELECT (id).fn(1) FROM S.Base;
END
"""
    result = _analyze(tmp_path, **{"rejected-call.sql": sql})

    assert not _edges(result, "sql:calls")
    assert not _edges(result, "sql:reads-from")
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_function_resolution_is_forward_casefolded_and_preserves_fallbacks(
    tmp_path: Path,
) -> None:
    forward = _analyze(
        tmp_path / "forward",
        **{
            "view.sql": (
                "CREATE TABLE S.Base (id int)\nGO\nCREATE VIEW S.V AS SELECT calc(id) FROM S.Base"
            ),
            "function.sql": "CREATE FUNCTION CALC(@id int) RETURNS int AS RETURN @id",
        },
    )
    assert _edges(forward, "sql:calls") == {("S.V", "CALC")}
    assert not forward.diagnostics

    ambiguous = _analyze(
        tmp_path / "ambiguous",
        **{
            "a.sql": "CREATE FUNCTION F(@id int) RETURNS int AS RETURN @id",
            "b.sql": "CREATE FUNCTION F(@id int) RETURNS int AS RETURN @id",
            "view.sql": "CREATE TABLE Base (id int)\nGO\nCREATE VIEW V AS SELECT F(id) FROM Base",
        },
    )
    assert not _edges(ambiguous, "sql:calls")
    assert any(item.code is DiagnosticCode.DUPLICATE_DECLARATION for item in ambiguous.diagnostics)
    assert any(item.code is DiagnosticCode.AMBIGUOUS_REFERENCE for item in ambiguous.diagnostics)
    assert any(
        node.node_class.value == "unresolved-reference" and node.label == "F"
        for node in ambiguous.document.nodes
    )

    fallback = _analyze(
        tmp_path / "fallback",
        **{
            "view.sql": (
                "CREATE TABLE Base (id int)\nGO\n"
                "CREATE VIEW V AS SELECT dbo.Missing(id), Unknown(id), COUNT(id), COALESCE(id, 0) "
                "FROM Base"
            )
        },
    )
    assert not _edges(fallback, "sql:calls")
    unresolved = {
        node.label
        for node in fallback.document.nodes
        if node.node_class.value == "unresolved-reference"
    }
    assert unresolved == {"dbo.Missing"}
    assert not fallback.diagnostics


@pytest.mark.parametrize(
    ("member", "supported"),
    [
        ("SET NOCOUNT ON;", True),
        ("COMMIT TRANSACTION;", False),
        ("DECLARE @n int;", True),
        ("ROLLBACK TRANSACTION;", False),
        ("END CONVERSATION S.Hidden;", False),
    ],
)
def test_procedure_members_keep_separate_procedure_read_root(
    tmp_path: Path, member: str, supported: bool
) -> None:
    sql = f"""\
CREATE TABLE S.Base (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
{member}
SELECT * FROM S.Base;
END
"""
    result = _analyze(tmp_path, **{"members.sql": sql})

    symbols = _symbols(result)
    assert {"S.Base", "S.Read"} <= symbols.keys()
    assert symbols["S.Read"].symbol_kind == "sql:procedure"
    assert symbols["S.Base"].symbol_kind == "sql:table"
    edges = _sql_edges(result, "sql:reads-from")
    assert len(edges) == 1
    assert (edges[0][0].label, edges[0][1].label) == ("S.Read", "S.Base")
    assert _evidence_locations(edges[0][2]) == {("members.sql", 4, 14)}
    assert not _sql_edges(result, "references")
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]
    if supported:
        assert not result.diagnostics
    else:
        assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
        assert result.diagnostics[0].location is None


def test_declare_set_and_return_members_contribute_nested_query_reads_and_calls(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE dbo.Items (id int);
GO
CREATE TABLE dbo.Other (id int);
GO
CREATE FUNCTION dbo.Calc(@id int) RETURNS int AS RETURN @id;
GO
CREATE PROCEDURE dbo.P AS
BEGIN
  DECLARE @r int = (SELECT COUNT(*) FROM dbo.Items);
  SET @r = (SELECT dbo.Calc(id) FROM dbo.Other);
  SET @r = dbo.Calc(1);
  RETURN @r;
END;
"""
    result = _analyze(tmp_path, **{"declarations.sql": sql})

    symbols = _symbols(result)
    assert {"dbo.Items", "dbo.Other", "dbo.Calc", "dbo.P"} <= symbols.keys()
    assert symbols["dbo.P"].symbol_kind == "sql:procedure"
    assert symbols["dbo.Calc"].symbol_kind == "sql:function"
    assert not result.diagnostics
    reads = _sql_edges(result, "sql:reads-from")
    assert {(edge[0].label, edge[1].label) for edge in reads} == {
        ("dbo.P", "dbo.Items"),
        ("dbo.P", "dbo.Other"),
    }
    assert {location for edge in reads for location in _evidence_locations(edge[2])} == {
        ("declarations.sql", 8, 41),
        ("declarations.sql", 9, 37),
    }
    calls = _sql_edges(result, "sql:calls")
    assert [(edge[0].label, edge[1].label) for edge in calls] == [("dbo.P", "dbo.Calc")]
    assert _evidence_locations(calls[0][2]) == {("declarations.sql", 9, 23)}
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]


def test_return_subquery_member_contributes_its_read_without_diagnostics(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.Items (id int);
GO
CREATE PROCEDURE dbo.Q AS
BEGIN
  DECLARE @r int;
  RETURN (SELECT COUNT(*) FROM dbo.Items);
END;
"""
    result = _analyze(tmp_path, **{"returned.sql": sql})

    assert {"dbo.Items", "dbo.Q"} <= _symbols(result).keys()
    assert not result.diagnostics
    edges = _sql_edges(result, "sql:reads-from")
    assert [(edge[0].label, edge[1].label) for edge in edges] == [("dbo.Q", "dbo.Items")]
    assert _evidence_locations(edges[0][2]) == {("returned.sql", 5, 31)}


def test_bare_return_members_keep_procedure_reads_without_diagnostics(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.Items (id int);
GO
CREATE PROCEDURE dbo.R AS
BEGIN
  SELECT id FROM dbo.Items;
  RETURN;
END;
GO
CREATE PROCEDURE dbo.S @x int AS
BEGIN
  IF @x IS NULL RETURN;
  SELECT id FROM dbo.Items;
END;
"""
    result = _analyze(tmp_path, **{"returns.sql": sql})

    assert {"dbo.R", "dbo.S"} <= _symbols(result).keys()
    assert not result.diagnostics
    edges = _sql_edges(result, "sql:reads-from")
    assert {(edge[0].label, edge[1].label) for edge in edges} == {
        ("dbo.R", "dbo.Items"),
        ("dbo.S", "dbo.Items"),
    }
    assert {
        (edge[0].label, *location) for edge in edges for location in _evidence_locations(edge[2])
    } == {
        ("dbo.R", "returns.sql", 4, 17),
        ("dbo.S", "returns.sql", 11, 17),
    }


@pytest.mark.parametrize(
    ("member", "supported", "inferred"),
    [
        ("RETURN;", True, False),
        ("return;", True, False),
        ("Return;", True, False),
        ("RETURN @r;", True, False),
        ("RETURN (SELECT COUNT(*) FROM S.Hidden);", True, True),
        ("return (SELECT COUNT(*) FROM S.Hidden);", True, True),
        ("[RETURN];", False, False),
        ('"RETURN";', False, False),
        ("[RETURN] @r;", False, False),
        ("[RETURN] (SELECT COUNT(*) FROM S.Hidden);", False, False),
        ("dbo.RETURN;", False, False),
        ("a.b.RETURN;", False, False),
        ("dbo.RETURN @r;", False, False),
        ("dbo.RETURN (SELECT COUNT(*) FROM S.Hidden);", False, False),
        ("THROW;", False, False),
        ("RESULT @r;", False, False),
        ("RAISEERROR (SELECT COUNT(*) FROM S.Hidden);", False, False),
    ],
)
def test_return_member_match_requires_the_bare_unquoted_identifier(
    tmp_path: Path, member: str, supported: bool, inferred: bool
) -> None:
    sql = f"""\
CREATE TABLE S.Base (id int)
GO
CREATE TABLE S.Hidden (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
SELECT * FROM S.Base;
{member}
END
"""
    result = _analyze(tmp_path, **{"members.sql": sql})

    reads = {(edge[0].label, edge[1].label) for edge in _sql_edges(result, "sql:reads-from")}
    assert ("S.Read", "S.Base") in reads
    assert (("S.Read", "S.Hidden") in reads) == inferred
    if supported:
        assert not result.diagnostics
    else:
        assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
        assert result.diagnostics[0].location is not None


def test_procedure_roots_exclude_dml_and_temporary_sources_locally(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
SELECT * FROM S.Base;
INSERT INTO S.Target SELECT * FROM S.Hidden;
SELECT * FROM #scratch;
END
"""
    result = _analyze(tmp_path, **{"roots.sql": sql})
    symbols = _symbols(result)
    edges = _sql_edges(result, "sql:reads-from")
    unresolved = {
        node.label
        for node in result.document.nodes
        if node.node_class.value == "unresolved-reference"
    }

    assert {"S.Base", "S.Read"} <= symbols.keys()
    assert {(edge[0].label, edge[1].label) for edge in edges} == {("S.Read", "S.Base")}
    assert unresolved == set()
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_dynamic_sql_is_opaque_but_a_separate_static_root_survives(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE PROCEDURE S.Read AS BEGIN
EXEC(@sql);
SELECT * FROM S.Base;
END
"""
    result = _analyze(tmp_path, **{"dynamic.sql": sql})
    edges = _sql_edges(result, "sql:reads-from")

    assert {(edge[0].label, edge[1].label) for edge in edges} == {("S.Read", "S.Base")}
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].code is DiagnosticCode.UNSUPPORTED_SYNTAX
    assert result.diagnostics[0].location is None


def test_opaque_cursor_and_try_members_do_not_infer_text_reads(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE PROCEDURE S.CursorRead AS BEGIN
DECLARE c CURSOR FOR SELECT * FROM S.Hidden;
SELECT * FROM S.Base;
END
GO
CREATE PROCEDURE S.TryRead AS BEGIN TRY
SELECT * FROM S.HiddenTry
END TRY;
BEGIN CATCH SELECT * FROM S.HiddenCatch END CATCH;
SELECT * FROM S.Base;
END
"""
    result = _analyze(tmp_path, **{"opaque.sql": sql})
    edges = _sql_edges(result, "sql:reads-from")

    assert {"S.CursorRead", "S.TryRead"} <= _symbols(result).keys()
    assert {(edge[0].label, edge[1].label) for edge in edges} == {
        ("S.CursorRead", "S.Base"),
        ("S.TryRead", "S.Base"),
    }
    assert len(result.diagnostics) >= 2
    assert all(item.code is DiagnosticCode.UNSUPPORTED_SYNTAX for item in result.diagnostics)


def test_malformed_batch_is_atomic_and_later_procedure_batch_recovers(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE S.Base (id int)
GO
CREATE TABLE Broken (
GO
CREATE PROCEDURE S.Read AS BEGIN SELECT * FROM S.Base; END
"""
    result = _analyze(tmp_path, **{"recovery.sql": sql})
    edges = _sql_edges(result, "sql:reads-from")

    assert set(_symbols(result)) == {"S.Base", "S.Read"}
    assert {(edge[0].label, edge[1].label) for edge in edges} == {("S.Read", "S.Base")}
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.PARSE_ERROR]


def test_proc_spelling_records_the_same_procedure_facts_as_procedure(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.T (id int)
GO
CREATE FUNCTION dbo.F(@x int) RETURNS int AS RETURN @x
GO
CREATE PROC dbo.P2 AS SELECT dbo.F(id) FROM dbo.T
GO
CREATE PROCEDURE dbo.P3 AS SELECT dbo.F(id) FROM dbo.T
"""
    result = _analyze(tmp_path, **{"procedures.sql": sql})

    assert not result.diagnostics
    symbols = _symbols(result)
    assert {label: symbols[label].symbol_kind for label in ("dbo.P2", "dbo.P3")} == {
        "dbo.P2": "sql:procedure",
        "dbo.P3": "sql:procedure",
    }
    assert _edges(result, "sql:reads-from") == {("dbo.P2", "dbo.T"), ("dbo.P3", "dbo.T")}
    assert _edges(result, "sql:calls") == {("dbo.P2", "dbo.F"), ("dbo.P3", "dbo.F")}


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE OR REPLACE PROCEDURE S.P AS SELECT 1",
        "CREATE OR REPLACE PROC S.P AS SELECT 1",
        "CREATE OR REPLACE FUNCTION S.F() RETURNS int AS RETURN 1",
        "-- leading comment\nCREATE OR REPLACE PROCEDURE S.P AS SELECT 1",
        (
            "/* leading block */ CREATE /* one */ OR /* two */ REPLACE /* three */ "
            "FUNCTION S.F() RETURNS int AS RETURN 1"
        ),
        "; ; CREATE OR REPLACE FUNCTION S.F() RETURNS int AS RETURN 1",
    ],
)
def test_or_replace_declarations_remain_unsupported(tmp_path: Path, statement: str) -> None:
    result = _analyze(tmp_path, **{"replace.sql": statement})

    assert not [node for node in result.document.nodes if node.symbol_kind]
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.UNSUPPORTED_SYNTAX
    ]


def test_or_replace_view_does_not_change_later_function_modifier(tmp_path: Path) -> None:
    sql = """\
CREATE OR REPLACE VIEW S.V AS SELECT 1;
CREATE FUNCTION S.F() RETURNS int AS RETURN 1
"""
    result = _analyze(tmp_path, **{"mixed.sql": sql})

    assert set(_symbols(result)) == {"S.V", "S.F"}
    assert not result.diagnostics


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE PROCEDURE S.P",
        "CREATE PROCEDURE S.P AS",
        "CREATE PROC S.P",
        "CREATE PROC S.P AS",
        "CREATE FUNCTION S.F() RETURNS int",
        "CREATE FUNCTION S.F() RETURNS int AS",
    ],
)
def test_incomplete_declaration_headers_are_atomic_and_recover(
    tmp_path: Path, statement: str
) -> None:
    result = _analyze(tmp_path, **{"incomplete.sql": statement})

    assert not _symbols(result)
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.UNSUPPORTED_SYNTAX
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE FUNCTION S.F() RETURNS int",
        "CREATE FUNCTION S.F() RETURNS int AS",
    ],
)
def test_incomplete_function_headers_are_atomic_and_recover_same_batch(
    tmp_path: Path, statement: str
) -> None:
    result = _analyze(
        tmp_path,
        **{"incomplete.sql": f"{statement}; CREATE TABLE S.After (id int)"},
    )

    assert set(_symbols(result)) == {"S.After"}
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.UNSUPPORTED_SYNTAX
    ]


def test_unsupported_if_not_exists_block_function_has_one_diagnostic_and_recovers(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE FUNCTION IF NOT EXISTS S.F() RETURNS int AS BEGIN
RETURN 1;
END;
CREATE TABLE S.After (id int)
"""
    result = _analyze(tmp_path, **{"if-not-exists.sql": sql})

    assert set(_symbols(result)) == {"S.After"}
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.UNSUPPORTED_SYNTAX
    ]


_ITEMS_DECLARATION = "CREATE TABLE dbo.Items (id int)"
_OTHER_DECLARATION = "CREATE TABLE dbo.Other (id int)"
_AFTER_DECLARATION = "CREATE TABLE dbo.After (id int)"
_FUNCTION_HEADER = "CREATE FUNCTION dbo.M3(@f int) RETURNS int AS\nBEGIN\n"
_ITEMS_COUNT = "SELECT @r = COUNT(*) FROM dbo.Items"
_CURSOR_DECLARATION = "DECLARE c CURSOR FOR SELECT id FROM dbo.Items"
_FUNCTION_BLOCK = f"BEGIN\n{_ITEMS_COUNT};\nEND;"


def _body_with_variant(variant: str) -> str:
    """Build a body from a declare, one variant member, a return and the end."""
    return f"DECLARE @r int;\n{variant}\nRETURN @r;\nEND;"


def _function_fixture(body: str, *, separated: bool) -> str:
    """Build a batch declaring the tables, one function body and ``dbo.After``."""
    ending = "GO\n" if separated else ""
    return (
        f"{_ITEMS_DECLARATION}\nGO\n{_OTHER_DECLARATION}\nGO\n"
        f"{_FUNCTION_HEADER}{body}\n{ending}{_AFTER_DECLARATION}"
    )


_MATRIX_BODIES = [
    ("B1", _body_with_variant(_FUNCTION_BLOCK)),
    ("B2", _body_with_variant(f"BEGIN\n{_ITEMS_COUNT}\nEND;")),
    ("B3", _body_with_variant(f"BEGIN\n{_ITEMS_COUNT};\nEND")),
    ("B4", _body_with_variant(f"BEGIN\n{_ITEMS_COUNT}\nEND")),
    ("CB", f"DECLARE @r int;\nBEGIN\n{_ITEMS_COUNT}\nEND\nRETURN @r\nEND;"),
    ("B5", _body_with_variant(f"begin\n{_ITEMS_COUNT};\nend;")),
    ("B6", _body_with_variant(f"Begin\n{_ITEMS_COUNT};\nEnd;")),
    ("B7", _body_with_variant(f"BEGIN\nBEGIN\n{_ITEMS_COUNT};\nEND;\nEND;")),
    ("B8", _body_with_variant(f"BEGIN\nIF @f = 1 {_ITEMS_COUNT};\nEND;")),
    ("B9", _body_with_variant("BEGIN\nWHILE @f > 0 SELECT @f = @f - 1;\nEND;")),
    (
        "B10",
        f"DECLARE @r int;\n{_FUNCTION_BLOCK}\nRETURN (SELECT COUNT(*) FROM dbo.Items);\nEND;",
    ),
    ("B11", f"DECLARE @r int;\n{_FUNCTION_BLOCK}\nreturn @r;\nEND;"),
    ("B12", f"DECLARE @r int;\nRETURN @r;\n{_FUNCTION_BLOCK}\nEND;"),
    ("B13", f"DECLARE @r int;\nRETURN @r;\nBEGIN\n{_ITEMS_COUNT};\nEND\nEND;"),
    ("B14", f"DECLARE @r int;\n{_ITEMS_COUNT};\nRETURN @r;\nEND"),
    ("B15", f"DECLARE @r int;\nIF @f = 1 {_ITEMS_COUNT};\nRETURN @r;\nEND;"),
    ("B16", f"{_FUNCTION_BLOCK}\nRETURN @r;\nEND;"),
    ("B17", f"DECLARE @r int;\n{_FUNCTION_BLOCK}\nIF @f = 1 SELECT @f = 2;\nRETURN @r;\nEND;"),
    (
        "B18",
        f"DECLARE @r int;\n{_FUNCTION_BLOCK}\nWHILE @f > 0 SELECT @f = @f - 1;\nRETURN @r;\nEND;",
    ),
    ("N1", f"DECLARE @r int\n{_FUNCTION_BLOCK}\nRETURN @r;\nEND;"),
    ("N2", _body_with_variant(f"SET @r = 1\n{_FUNCTION_BLOCK}")),
    ("N3", f"SET @f = 1\n{_FUNCTION_BLOCK}\nRETURN @r;\nEND;"),
    ("N4", _body_with_variant(f"{_CURSOR_DECLARATION}\n{_FUNCTION_BLOCK}")),
    ("N5", f"{_CURSOR_DECLARATION}\n{_FUNCTION_BLOCK}\nRETURN @r;\nEND;"),
    ("N6", f"DECLARE @r int\n{_ITEMS_COUNT};\nRETURN @r;\nEND;"),
    ("N7", f"DECLARE @r int;\n{_CURSOR_DECLARATION};\nRETURN @r;\nEND;"),
    ("N8", _body_with_variant("BEGIN TRY SELECT 1; END TRY BEGIN CATCH SELECT 2; END CATCH;")),
    ("N9", "DECLARE @r int\nEND;"),
    ("N10", f"DECLARE @r int\n{_ITEMS_COUNT}\nRETURN @r\nEND;"),
    ("N11", f"DECLARE @r int\nBEGIN\n{_ITEMS_COUNT}\nEND\nRETURN @r\nEND;"),
    ("N12", "DECLARE @r int;\nSET @r = 1\nEND;"),
    ("N13", f"DECLARE @r int;\n{_CURSOR_DECLARATION}\nEND;"),
    ("N14", f"DECLARE @r int;\n{_ITEMS_COUNT}\nEND;"),
    ("N15", "DECLARE @r int;\nEXEC dbo.Q\nEND;"),
    ("N16", "DECLARE @r int;\nRETURN\nEND;"),
    ("N17", "RETURN\nBEGIN\nSELECT @f = 1;\nEND;\nEND;"),
    ("N18", "END;"),
    ("N19", "DECLARE @r int;\nRETURN @r;\nEND\nEND;"),
    ("N20", f"{_ITEMS_COUNT}\nEND;"),
    ("N21", "IF @f = 1 SELECT @f = 2;\nRETURN @f;\nEND;"),
    ("N22", f"DECLARE @r int;\n{_ITEMS_COUNT};\nRETURN @r;\nEND -- done"),
    ("N23", _body_with_variant(f"{_ITEMS_COUNT}; -- done")),
    ("Q1", f"DECLARE @r int;\n{_ITEMS_COUNT};\n[RETURN] @r;\nEND;"),
]
_FOLDED_ROWS = frozenset({"B14", "B15", "B17", "B18", "N21", "N22"})
_SEPARATED_FOLDED_ROWS = frozenset({"B15", "B17", "B18", "N21", "N22"})


def _assert_one_function_diagnostic(result, function) -> None:
    """Assert exactly one located unsupported declaration and no body edges."""
    assert not [
        edge for edge in _sql_edges(result, "sql:reads-from") if edge[0].label == function.label
    ]
    assert not [edge for edge in _sql_edges(result, "sql:calls") if edge[0].label == function.label]
    assert [(item.code, item.location) for item in result.diagnostics] == [
        (DiagnosticCode.UNSUPPORTED_SYNTAX, function.location)
    ]


@pytest.mark.parametrize(("row", "body"), _MATRIX_BODIES, ids=[row for row, _ in _MATRIX_BODIES])
def test_unrepresentable_function_body_keeps_one_declaration_diagnostic(
    tmp_path: Path, row: str, body: str
) -> None:
    result = _analyze(tmp_path, **{"bodies.sql": _function_fixture(body, separated=False)})
    symbols = _symbols(result)
    function = symbols["dbo.M3"]

    assert function.symbol_kind == "sql:function"
    _assert_one_function_diagnostic(result, function)
    if row in _FOLDED_ROWS:
        assert "dbo.After" not in symbols
    else:
        assert symbols["dbo.After"].symbol_kind == "sql:table"
        assert ("bodies.sql", "dbo.After") in _edges(result, "contains")


@pytest.mark.parametrize(
    ("row", "body"),
    [entry for entry in _MATRIX_BODIES if entry[0] in _SEPARATED_FOLDED_ROWS],
    ids=sorted(_SEPARATED_FOLDED_ROWS),
)
def test_separated_declaration_after_an_unrepresentable_body_is_analyzed(
    tmp_path: Path, row: str, body: str
) -> None:
    result = _analyze(tmp_path, **{"bodies.sql": _function_fixture(body, separated=True)})
    symbols = _symbols(result)
    function = symbols["dbo.M3"]

    assert function.symbol_kind == "sql:function"
    _assert_one_function_diagnostic(result, function)
    assert symbols["dbo.After"].symbol_kind == "sql:table"
    assert ("bodies.sql", "dbo.After") in _edges(result, "contains")


@pytest.mark.parametrize(
    ("body", "read", "separated"),
    [
        (f"DECLARE @r int;\n{_ITEMS_COUNT};\nRETURN @r;\nEND", True, True),
        ("INSERT INTO @t SELECT id FROM dbo.Other;\nRETURN;\nEND;", False, False),
        ("RETURN (SELECT COUNT(*) FROM dbo.Items);\nEND;", True, False),
        ("SET NOCOUNT ON;\nDECLARE @r int;\nRETURN @r;\nEND;", False, False),
        (f"{_ITEMS_COUNT}\nRETURN;\nEND;", True, False),
    ],
    ids=["R7", "R3", "R5", "R6", "R8"],
)
def test_flat_function_body_query_roots_are_read_without_diagnostics(
    tmp_path: Path, body: str, read: bool, separated: bool
) -> None:
    result = _analyze(tmp_path, **{"bodies.sql": _function_fixture(body, separated=separated)})
    symbols = _symbols(result)

    assert not result.diagnostics
    assert symbols["dbo.M3"].symbol_kind == "sql:function"
    assert symbols["dbo.After"].symbol_kind == "sql:table"
    assert {(edge[0].label, edge[1].label) for edge in _sql_edges(result, "sql:reads-from")} == (
        {("dbo.M3", "dbo.Items")} if read else set()
    )
    assert ("bodies.sql", "dbo.After") in _edges(result, "contains")


def test_multi_statement_function_body_reads_calls_and_keeps_a_later_declaration(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE dbo.Items (id int)
GO
CREATE TABLE dbo.Other (id int)
GO
CREATE FUNCTION dbo.Calc(@x int) RETURNS int AS RETURN @x
GO
CREATE FUNCTION dbo.M2(@n int) RETURNS int AS
BEGIN
  DECLARE @r int;
  SELECT @r = dbo.Calc(id) FROM dbo.Items;
  SET @r = (SELECT COUNT(*) FROM dbo.Other);
  RETURN @r;
END;
CREATE TABLE dbo.After (id int)
"""
    result = _analyze(tmp_path, **{"functions.sql": sql})
    symbols = _symbols(result)

    assert not result.diagnostics
    assert symbols["dbo.M2"].symbol_kind == "sql:function"
    assert symbols["dbo.After"].symbol_kind == "sql:table"
    reads = {(edge[0].label, edge[1].label): edge for edge in _sql_edges(result, "sql:reads-from")}
    assert set(reads) == {("dbo.M2", "dbo.Items"), ("dbo.M2", "dbo.Other")}
    assert _evidence_locations(reads[("dbo.M2", "dbo.Items")][2]) == {
        ("functions.sql", 9, sql.splitlines()[9].index("dbo.Items"))
    }
    assert _evidence_locations(reads[("dbo.M2", "dbo.Other")][2]) == {
        ("functions.sql", 10, sql.splitlines()[10].index("dbo.Other"))
    }
    calls = _sql_edges(result, "sql:calls")
    assert [(edge[0].label, edge[1].label) for edge in calls] == [("dbo.M2", "dbo.Calc")]
    assert _evidence_locations(calls[0][2]) == {
        ("functions.sql", 9, sql.splitlines()[9].index("Calc"))
    }
    assert ("functions.sql", "dbo.After") in _edges(result, "contains")


@pytest.mark.parametrize(
    "member",
    [
        "  declare @r int;\n  return (SELECT COUNT(*) FROM dbo.Items);",
        "  declare @r int;\n  SELECT @r = COUNT(*) FROM dbo.Items;\n  return @r;",
    ],
    ids=["return-subquery", "return-after-select"],
)
def test_lowercase_function_return_members_keep_their_nested_query_read(
    tmp_path: Path, member: str
) -> None:
    sql = (
        f"{_ITEMS_DECLARATION}\nGO\n"
        f"CREATE FUNCTION dbo.M4() RETURNS int AS\nBEGIN\n{member}\nEND;\n"
        f"{_AFTER_DECLARATION}"
    )
    result = _analyze(tmp_path, **{"functions.sql": sql})
    symbols = _symbols(result)

    assert not result.diagnostics
    assert symbols["dbo.M4"].symbol_kind == "sql:function"
    reads = _sql_edges(result, "sql:reads-from")
    assert [(edge[0].label, edge[1].label) for edge in reads] == [("dbo.M4", "dbo.Items")]
    assert _evidence_locations(reads[0][2]) == {
        ("functions.sql", 5, sql.splitlines()[5].index("dbo.Items"))
    }
    assert symbols["dbo.After"].symbol_kind == "sql:table"
    assert ("functions.sql", "dbo.After") in _edges(result, "contains")


def test_rejected_function_header_consumes_its_body_with_one_diagnostic(tmp_path: Path) -> None:
    sql = """\
CREATE FUNCTION IF NOT EXISTS S.F() RETURNS int AS BEGIN
DECLARE @r int;
SELECT @r = COUNT(*) FROM S.Base;
RETURN @r;
END;
CREATE TABLE S.After (id int)
"""
    result = _analyze(tmp_path, **{"if-not-exists.sql": sql})

    assert set(_symbols(result)) == {"S.After"}
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
    assert not _sql_edges(result, "sql:reads-from")
    assert ("if-not-exists.sql", "S.After") in _edges(result, "contains")


def test_function_batch_the_parser_rejects_keeps_one_parse_error(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.Items (id int)
GO

CREATE FUNCTION dbo.M(@f int) RETURNS int AS
BEGIN
  DECLARE @r int;
  RETURN dbo.Calc(@f) + 1;
END;
"""
    result = _analyze(tmp_path, **{"unparseable.sql": sql})

    assert set(_symbols(result)) == {"dbo.Items"}
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.PARSE_ERROR]
    assert not _sql_edges(result, "sql:reads-from")
    assert not _sql_edges(result, "sql:calls")


def test_table_valued_function_reads_query_roots_without_its_dml_source(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE dbo.Items (id int)
GO
CREATE TABLE dbo.Other (id int)
GO
CREATE FUNCTION dbo.T(@x int) RETURNS @t TABLE (id int) AS
BEGIN
  DECLARE @n int;
  SELECT @n = COUNT(*) FROM dbo.Items;
  INSERT INTO @t SELECT id FROM dbo.Other;
  RETURN;
END;
CREATE TABLE dbo.After (id int)
"""
    result = _analyze(tmp_path, **{"table-valued.sql": sql})
    symbols = _symbols(result)

    assert not result.diagnostics
    assert symbols["dbo.T"].symbol_kind == "sql:function"
    assert symbols["dbo.Other"].symbol_kind == "sql:table"
    reads = _sql_edges(result, "sql:reads-from")
    assert [(edge[0].label, edge[1].label) for edge in reads] == [("dbo.T", "dbo.Items")]
    assert _evidence_locations(reads[0][2]) == {
        ("table-valued.sql", 7, sql.splitlines()[7].index("dbo.Items"))
    }
    assert symbols["dbo.After"].symbol_kind == "sql:table"
    assert ("table-valued.sql", "dbo.After") in _edges(result, "contains")


def test_exec_member_keeps_the_function_read_beside_one_walker_diagnostic(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE dbo.Items (id int)
GO
CREATE FUNCTION dbo.E() RETURNS int AS
BEGIN
  DECLARE @r int;
  EXEC dbo.Q;
  SELECT @r = COUNT(*) FROM dbo.Items;
  RETURN @r;
END;
CREATE TABLE dbo.After (id int)
"""
    result = _analyze(tmp_path, **{"exec-member.sql": sql})
    symbols = _symbols(result)

    assert symbols["dbo.E"].symbol_kind == "sql:function"
    reads = _sql_edges(result, "sql:reads-from")
    assert [(edge[0].label, edge[1].label) for edge in reads] == [("dbo.E", "dbo.Items")]
    assert _evidence_locations(reads[0][2]) == {
        ("exec-member.sql", 6, sql.splitlines()[6].index("dbo.Items"))
    }
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
    assert result.diagnostics[0].location != symbols["dbo.E"].location
    assert symbols["dbo.After"].symbol_kind == "sql:table"
    assert ("exec-member.sql", "dbo.After") in _edges(result, "contains")


@pytest.mark.parametrize(
    "member",
    [
        "RETURN EXEC dbo.Q;",
        "RETURN EXEC('SELECT 1');",
        "RETURN\nEXEC dbo.Q;",
        "RETURN EXEC('SELECT COUNT(*) FROM dbo.Items');",
        "RETURN EXEC sp_executesql @sql;",
    ],
    ids=[
        "exec-function",
        "exec-string",
        "return-then-exec",
        "exec-string-read",
        "exec-system-procedure",
    ],
)
def test_return_exec_member_reports_one_diagnostic_and_no_facts(
    tmp_path: Path, member: str
) -> None:
    sql = (
        f"{_ITEMS_DECLARATION}\nGO\n"
        "CREATE FUNCTION dbo.Q() RETURNS int AS RETURN 1\nGO\n"
        f"{_FUNCTION_HEADER}{member}\nEND;\n{_AFTER_DECLARATION}"
    )
    result = _analyze(tmp_path, **{"return-exec.sql": sql})
    symbols = _symbols(result)

    assert symbols["dbo.M3"].symbol_kind == "sql:function"
    assert symbols["dbo.After"].symbol_kind == "sql:table"
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
    assert result.diagnostics[0].location != symbols["dbo.M3"].location
    assert not _sql_edges(result, "sql:reads-from")
    assert not _sql_edges(result, "sql:calls")
    assert ("return-exec.sql", "dbo.After") in _edges(result, "contains")


_TAIL_PREAMBLE = (
    f"{_ITEMS_DECLARATION}\nGO\n"
    "CREATE TABLE dbo.P (id int)\nGO\n"
    "CREATE TABLE dbo.C (id int, pid int)\nGO\n"
)


@pytest.mark.parametrize(
    ("tail", "extra_diagnostic", "foreign_key"),
    [
        (
            "ALTER TABLE dbo.C ADD CONSTRAINT fk FOREIGN KEY (pid) REFERENCES dbo.P (id)",
            False,
            True,
        ),
        ("DROP TABLE dbo.C", True, False),
        ("DROP SYNONYM dbo.X", True, False),
        ("CREATE TRIGGER dbo.T ON dbo.C AFTER INSERT AS SELECT 1", True, False),
        ("create trigger dbo.T on dbo.C after insert as select 1", True, False),
        ("ALTER PROCEDURE dbo.Q AS SELECT 1", True, False),
        ("SELECT 1", False, False),
    ],
    ids=[
        "alter-foreign-key",
        "drop-table",
        "drop-synonym",
        "create-trigger",
        "lowercase-create-trigger",
        "alter-procedure",
        "absorbed-select",
    ],
)
def test_unrepresentable_function_body_extent_ends_at_a_declaration_boundary(
    tmp_path: Path, tail: str, extra_diagnostic: bool, foreign_key: bool
) -> None:
    sql = f"{_TAIL_PREAMBLE}{_FUNCTION_HEADER}{_body_with_variant(_FUNCTION_BLOCK)}\n{tail}\n"
    result = _analyze(tmp_path, **{"tails.sql": sql})
    symbols = _symbols(result)

    function = symbols["dbo.M3"]
    assert function.symbol_kind == "sql:function"
    located = [item for item in result.diagnostics if item.location == function.location]
    assert [(item.code) for item in located] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
    further = [item for item in result.diagnostics if item.location != function.location]
    assert [item.code for item in further] == (
        [DiagnosticCode.UNSUPPORTED_SYNTAX] if extra_diagnostic else []
    )
    assert not [edge for edge in _sql_edges(result, "sql:reads-from") if edge[0].label == "dbo.M3"]
    assert not [edge for edge in _sql_edges(result, "sql:calls") if edge[0].label == "dbo.M3"]
    assert _edges(result, "sql:foreign-key-to") == ({("dbo.C", "dbo.P")} if foreign_key else set())


_CALC_READ = "SELECT @r = dbo.Calc(id) FROM dbo.Items"


@pytest.mark.parametrize(
    "body",
    [
        f"DECLARE @r int;\nBEGIN\n{_CALC_READ};\nEND;\nRETURN @r;\nEND;",
        f"DECLARE @r int;\n{_CALC_READ}; -- done\nRETURN @r;\nEND;",
        f"DECLARE @r int;\n{_CALC_READ}\nEND;",
    ],
    ids=["nested-block", "comment-after-member", "no-end-statement"],
)
def test_unrepresentable_function_body_member_calls_produce_no_edges(
    tmp_path: Path, body: str
) -> None:
    sql = (
        f"{_ITEMS_DECLARATION}\nGO\n"
        "CREATE FUNCTION dbo.Calc(@x int) RETURNS int AS RETURN @x\nGO\n"
        f"{_FUNCTION_HEADER}{body}\n{_AFTER_DECLARATION}"
    )
    result = _analyze(tmp_path, **{"bodies.sql": sql})
    symbols = _symbols(result)

    assert symbols["dbo.Calc"].symbol_kind == "sql:function"
    assert not _sql_edges(result, "sql:calls")
    _assert_one_function_diagnostic(result, symbols["dbo.M3"])
    assert symbols["dbo.After"].symbol_kind == "sql:table"


@pytest.mark.parametrize(
    ("body", "separated"),
    [
        ("DECLARE @r int;\nWHILE @f > 0 SELECT @f = @f - 1;\nRETURN @r;\nEND;", False),
        ("DECLARE @r int;\nWHILE @f > 0 SELECT @f = @f - 1;\nRETURN @r;\nEND;", True),
        ("WHILE @f > 0 SELECT @f = @f - 1;\nRETURN @f;\nEND;", False),
        ("WHILE @f > 0 SELECT @f = @f - 1;\nRETURN @f;\nEND;", True),
    ],
    ids=["later-member", "later-member-separated", "first-member", "first-member-separated"],
)
def test_while_function_body_folds_a_later_declaration_unless_separated(
    tmp_path: Path, body: str, separated: bool
) -> None:
    result = _analyze(tmp_path, **{"bodies.sql": _function_fixture(body, separated=separated)})
    symbols = _symbols(result)

    _assert_one_function_diagnostic(result, symbols["dbo.M3"])
    assert ("dbo.After" in symbols) is separated


@pytest.mark.parametrize(
    "member",
    [
        "foo(1);",
        "foo((SELECT COUNT(*) FROM dbo.Items));",
        "foo;",
        "foo @r;",
        "dbo.[RETURN];",
        "[RETURN];",
        "[RETURN](SELECT COUNT(*) FROM dbo.Items);",
    ],
    ids=[
        "function-call",
        "function-call-query",
        "column",
        "column-alias",
        "qualified-return",
        "quoted-return",
        "quoted-return-call",
    ],
)
def test_function_member_that_is_not_a_return_form_is_not_flat(tmp_path: Path, member: str) -> None:
    fixture = _function_fixture(_body_with_variant(member), separated=False)
    result = _analyze(tmp_path, **{"bodies.sql": fixture})
    symbols = _symbols(result)

    _assert_one_function_diagnostic(result, symbols["dbo.M3"])
    assert symbols["dbo.After"].symbol_kind == "sql:table"


@pytest.mark.parametrize(
    ("header", "body", "survives"),
    [
        (
            "CREATE FUNCTION IF NOT EXISTS dbo.M3() RETURNS int AS",
            _body_with_variant(_FUNCTION_BLOCK),
            True,
        ),
        (
            "CREATE FUNCTION IF NOT EXISTS dbo.M3() RETURNS int AS",
            f"DECLARE @r int;\n{_ITEMS_COUNT}\nEND;",
            True,
        ),
        (
            "CREATE FUNCTION IF NOT EXISTS dbo.M3() RETURNS int AS",
            f"DECLARE @r int;\nIF 1 = 1 {_ITEMS_COUNT};\nRETURN @r;\nEND;",
            False,
        ),
        ("CREATE FUNCTION a.b.c.M3() RETURNS int AS", _body_with_variant(_FUNCTION_BLOCK), True),
    ],
    ids=[
        "exists-nested-block",
        "exists-no-end-statement",
        "exists-folded",
        "long-name-nested-block",
    ],
)
def test_rejected_header_consumes_an_unrepresentable_body_with_one_diagnostic(
    tmp_path: Path, header: str, body: str, survives: bool
) -> None:
    sql = f"{_ITEMS_DECLARATION}\nGO\n{header}\nBEGIN\n{body}\n{_AFTER_DECLARATION}"
    result = _analyze(tmp_path, **{"bodies.sql": sql})

    assert set(_symbols(result)) == ({"dbo.Items", "dbo.After"} if survives else {"dbo.Items"})
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
    assert not _sql_edges(result, "sql:reads-from")
    assert not _sql_edges(result, "sql:calls")


def test_unrepresentable_table_valued_function_keeps_one_declaration_diagnostic(
    tmp_path: Path,
) -> None:
    sql = (
        f"{_ITEMS_DECLARATION}\nGO\n"
        "CREATE FUNCTION dbo.T(@x int) RETURNS @t TABLE (id int) AS\nBEGIN\n"
        f"BEGIN\nSELECT @x = COUNT(*) FROM dbo.Items;\nEND;\nRETURN;\nEND;\n{_AFTER_DECLARATION}"
    )
    result = _analyze(tmp_path, **{"table-valued.sql": sql})
    symbols = _symbols(result)

    assert symbols["dbo.T"].symbol_kind == "sql:function"
    _assert_one_function_diagnostic(result, symbols["dbo.T"])
    assert symbols["dbo.After"].symbol_kind == "sql:table"


@pytest.mark.parametrize(
    ("first", "second", "kept", "reader"),
    [
        (
            _body_with_variant(_FUNCTION_BLOCK),
            _body_with_variant(f"{_ITEMS_COUNT};"),
            "dbo.M3",
            "dbo.N",
        ),
        (
            _body_with_variant(f"{_ITEMS_COUNT};"),
            _body_with_variant(_FUNCTION_BLOCK),
            "dbo.N",
            "dbo.M3",
        ),
    ],
    ids=["unrepresentable-first", "representable-first"],
)
def test_each_function_in_one_batch_owns_only_its_own_body(
    tmp_path: Path, first: str, second: str, kept: str, reader: str
) -> None:
    sql = (
        f"{_ITEMS_DECLARATION}\nGO\n"
        f"{_FUNCTION_HEADER}{first}\n"
        f"CREATE FUNCTION dbo.N() RETURNS int AS\nBEGIN\n{second}\n"
        f"{_AFTER_DECLARATION}"
    )
    result = _analyze(tmp_path, **{"bodies.sql": sql})
    symbols = _symbols(result)

    assert [(item.code, item.location) for item in result.diagnostics] == [
        (DiagnosticCode.UNSUPPORTED_SYNTAX, symbols[kept].location)
    ]
    assert {(edge[0].label, edge[1].label) for edge in _sql_edges(result, "sql:reads-from")} == {
        (reader, "dbo.Items")
    }
    assert symbols["dbo.After"].symbol_kind == "sql:table"


def test_unsupported_declarations_and_parse_batches_remain_atomic(tmp_path: Path) -> None:
    sql = """\
CREATE FUNCTION S.Clr() RETURNS int AS EXTERNAL NAME a.b
GO
CREATE TRIGGER S.Trigger ON S.Base AFTER INSERT AS SELECT 1
GO
CREATE TABLE Broken (
GO
CREATE TABLE S.After (id int)
"""
    result = _analyze(tmp_path, **{"recovery.sql": sql})

    assert set(_symbols(result)) == {"S.After"}
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.PARSE_ERROR,
        DiagnosticCode.UNSUPPORTED_SYNTAX,
        DiagnosticCode.UNSUPPORTED_SYNTAX,
    ]


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

    assert [item.code for item in result.warnings] == [DiagnosticCode.ORPHANED_FOREIGN_KEY]
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


def test_orphan_fk_warnings_retain_create_alter_and_unnamed_constraint_identity(
    tmp_path: Path,
) -> None:
    result = _analyze(
        tmp_path,
        **{
            "schema.sql": (
                "CREATE TABLE Child (id int, other_id int REFERENCES Missing(id), "
                "CONSTRAINT fk_create FOREIGN KEY (id) REFERENCES Missing(id))\nGO\n"
                "ALTER TABLE Child ADD CONSTRAINT fk_alter FOREIGN KEY (id) "
                "REFERENCES Missing(id)\nGO\n"
                "CREATE TABLE Other (id int)\n"
            )
        },
    )

    warnings = [
        item for item in result.warnings if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    ]
    assert [item.extensions["minotaur-sql"] for item in warnings] == [
        {
            "source_table": "Child",
            "constraint_name": "unnamed",
            "target": "Missing",
            "reason": "undeclared",
        },
        {
            "source_table": "Child",
            "constraint_name": "fk_create",
            "target": "Missing",
            "reason": "undeclared",
        },
        {
            "source_table": "Child",
            "constraint_name": "fk_alter",
            "target": "Missing",
            "reason": "undeclared",
        },
    ]
    assert len(_sql_edges(result, "references")) == 1


def test_orphan_fk_reason_precedence_and_mapping_scope(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        settings=SqlSettings(foreign_key_target_files={"Missing": "mapped.sql"}),
        **{
            "child.sql": "CREATE TABLE Child (id int REFERENCES Missing(id))",
            "mapped.sql": "CREATE TABLE Wrong (id int)",
            "duplicate.sql": "CREATE TABLE Missing (id int)",
            "another.sql": "CREATE TABLE Missing (id int)",
        },
    )

    warning = next(
        item for item in result.warnings if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    )
    assert warning.extensions["minotaur-sql"]["reason"] == "ambiguous"

    extraction_gap = _analyze(
        tmp_path / "gap",
        settings=SqlSettings(foreign_key_target_files={"Missing": "mapped.sql"}),
        **{
            "child.sql": "CREATE TABLE Child (id int REFERENCES Missing(id))",
            "mapped.sql": "CREATE TABLE Wrong (id int)",
        },
    )
    warning = next(
        item for item in extraction_gap.warnings if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    )
    assert warning.extensions["minotaur-sql"]["reason"] == "extraction-gap"

    undeclared = _analyze(
        tmp_path / "unmapped",
        settings=SqlSettings(foreign_key_target_files={"Missing": "unselected.sql"}),
        **{"child.sql": "CREATE TABLE Child (id int REFERENCES Missing(id))"},
    )
    warning = next(
        item for item in undeclared.warnings if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    )
    assert warning.extensions["minotaur-sql"]["reason"] == "undeclared"


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


def test_ordered_parenthesized_view_cte_reads_body_and_rejects_variants(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.a (x int)
GO
CREATE VIEW v AS
WITH q AS (SELECT x FROM dbo.a) (SELECT x FROM q)
ORDER BY x OFFSET 0 ROWS
GO
CREATE VIEW self_ref AS
WITH q AS (SELECT x FROM dbo.a UNION ALL SELECT x FROM q) (SELECT x FROM q)
ORDER BY x OFFSET 0 ROWS
GO
CREATE VIEW mutual_ref AS
WITH q AS (SELECT x FROM r), r AS (SELECT x FROM q) (SELECT x FROM q)
ORDER BY x OFFSET 0 ROWS
GO
CREATE VIEW temporary_ref AS
WITH q AS (SELECT x FROM #t) (SELECT x FROM q)
ORDER BY x OFFSET 0 ROWS
"""
    result = _analyze(tmp_path, **{"ordered.sql": sql})

    assert set(_symbols(result)) == {"dbo.a", "v"}
    assert _edges(result, "sql:reads-from") == {("v", "dbo.a")}
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]
    assert [item.code for item in result.diagnostics] == [
        DiagnosticCode.UNSUPPORTED_SYNTAX,
        DiagnosticCode.UNSUPPORTED_SYNTAX,
        DiagnosticCode.UNSUPPORTED_SYNTAX,
    ]


def test_ordered_parenthesized_function_return_cte_reads_body_once(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.a (x int)
GO
CREATE FUNCTION f(@id int) RETURNS TABLE AS
RETURN WITH q AS (SELECT x FROM dbo.a) (SELECT x FROM q)
ORDER BY x OFFSET 0 ROWS
"""
    result = _analyze(tmp_path, **{"function.sql": sql})

    assert not result.diagnostics
    assert _edges(result, "sql:reads-from") == {("f", "dbo.a")}
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]


def test_ordered_parenthesized_cte_join_sources_are_read(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.a (x int)
GO
CREATE TABLE dbo.b (x int)
GO
CREATE VIEW v AS
WITH q AS (SELECT x FROM dbo.a) (SELECT x FROM q JOIN dbo.b ON 1 = 1)
ORDER BY x OFFSET 0 ROWS
"""
    result = _analyze(tmp_path, **{"join.sql": sql})

    assert not result.diagnostics
    assert _edges(result, "sql:reads-from") == {("v", "dbo.a"), ("v", "dbo.b")}


def test_ordered_parenthesized_cte_is_visible_to_wrapper_order_clause(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.a (x int)
GO
CREATE VIEW v AS
WITH q AS (SELECT x FROM dbo.a) (SELECT x FROM q)
ORDER BY (SELECT MAX(x) FROM q) OFFSET 0 ROWS
"""
    result = _analyze(tmp_path, **{"order.sql": sql})

    assert not result.diagnostics
    assert _edges(result, "sql:reads-from") == {("v", "dbo.a")}
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]


def test_parenthesized_nested_join_rejects_view_and_existence_forms(tmp_path: Path) -> None:
    forms = {
        "view": (
            "CREATE VIEW v AS SELECT a.x\n"
            "FROM dbo.a\n"
            "JOIN dbo.b\n"
            "JOIN (dbo.c c JOIN dbo.d d ON c.x = d.x) ON b.x = c.x ON a.x = b.x\n"
        ),
        "temporary": (
            "CREATE VIEW v AS SELECT a.x\n"
            "FROM dbo.a\n"
            "JOIN dbo.b\n"
            "JOIN (#t c JOIN dbo.d d ON c.x = d.x) ON b.x = c.x ON a.x = b.x\n"
        ),
        "existence": (
            "CREATE VIEW v AS SELECT 1 AS marker\n"
            "WHERE EXISTS (\n"
            " SELECT a.x FROM dbo.a\n"
            " JOIN dbo.b\n"
            " JOIN (dbo.c c JOIN dbo.d d ON c.x = d.x) ON b.x = c.x ON a.x = b.x\n"
            ")\n"
        ),
    }
    for name, sql in forms.items():
        result = _analyze(tmp_path / name, **{"parenthesized.sql": sql})
        assert not _symbols(result), name
        assert not _edges(result, "sql:reads-from"), name
        assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX], (
            name
        )


def test_parenthesized_nested_join_drops_only_the_affected_query_roots(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.keep (x int)
GO
CREATE PROCEDURE p AS BEGIN
SELECT * FROM dbo.keep;
SELECT a.x FROM dbo.a JOIN dbo.b JOIN (dbo.c c JOIN dbo.d d ON c.x = d.x) ON b.x = c.x ON a.x = b.x;
END
GO
CREATE FUNCTION f(@id int) RETURNS TABLE AS RETURN (
 SELECT a.x FROM dbo.a JOIN dbo.b JOIN (dbo.c c JOIN dbo.d d ON c.x = d.x) ON b.x = c.x ON a.x = b.x
)
"""
    result = _analyze(tmp_path, **{"roots.sql": sql})

    assert set(_symbols(result)) == {"dbo.keep", "p", "f"}
    assert _edges(result, "sql:reads-from") == {("p", "dbo.keep")}
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX] * 2


def test_nested_join_sources_are_read_at_their_own_locations(tmp_path: Path) -> None:
    sql = """\
CREATE TABLE dbo.a (x int)
GO
CREATE TABLE dbo.b (x int)
GO
CREATE TABLE dbo.c (x int)
GO
CREATE VIEW v AS
SELECT a.x FROM dbo.a
JOIN dbo.b
JOIN dbo.c ON b.x = c.x ON a.x = b.x
"""
    result = _analyze(tmp_path, **{"nested.sql": sql})
    edges = {
        (source.label, target.label): edge
        for source, target, edge in _sql_edges(result, "sql:reads-from")
    }
    lines = sql.splitlines()
    written = {
        "dbo.a": next(text for text in lines if "FROM dbo.a" in text),
        "dbo.b": next(text for text in lines if text.startswith("JOIN dbo.b")),
        "dbo.c": next(text for text in lines if text.startswith("JOIN dbo.c")),
    }

    assert not result.diagnostics
    assert set(edges) == {("v", "dbo.a"), ("v", "dbo.b"), ("v", "dbo.c")}
    for name, text in written.items():
        line = lines.index(text)
        assert _evidence_locations(edges[("v", name)]) == {("nested.sql", line, text.index(name))}
    assert [target.label for _source, target, _edge in _sql_edges(result, "sql:reads-from")] == [
        "dbo.a",
        "dbo.b",
        "dbo.c",
    ]


def test_nested_join_temporary_source_rejects_whole_view(tmp_path: Path) -> None:
    sql = """\
CREATE VIEW v AS
SELECT a.x FROM dbo.a
JOIN dbo.b
JOIN #t ON b.x = #t.x ON a.x = b.x
"""
    result = _analyze(tmp_path, **{"temporary.sql": sql})

    assert not _symbols(result)
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_exists_body_nested_join_reads_sources_and_rejects_temporary(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE dbo.a (x int)
GO
CREATE TABLE dbo.b (x int)
GO
CREATE TABLE dbo.c (x int)
GO
CREATE VIEW v AS SELECT 1 AS marker
WHERE EXISTS (
 SELECT a.x FROM dbo.a
 JOIN dbo.b
 JOIN dbo.c ON b.x = c.x ON a.x = b.x
)
GO
CREATE VIEW temporary_v AS SELECT 1 AS marker
WHERE EXISTS (
 SELECT a.x FROM dbo.a
 JOIN dbo.b
 JOIN #t ON b.x = #t.x ON a.x = b.x
)
"""
    result = _analyze(tmp_path, **{"exists.sql": sql})

    assert set(_symbols(result)) == {"dbo.a", "dbo.b", "dbo.c", "v"}
    assert _edges(result, "sql:reads-from") == {
        ("v", "dbo.a"),
        ("v", "dbo.b"),
        ("v", "dbo.c"),
    }
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


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
    assert [(d.code, d.location.range.start.line) for d in result.diagnostics] == [
        (DiagnosticCode.UNSUPPORTED_SYNTAX, 0),
        (DiagnosticCode.UNSUPPORTED_SYNTAX, 2),
        (DiagnosticCode.ORPHANED_FOREIGN_KEY, 4),
    ]
    assert dict(result.diagnostics[-1].extensions["minotaur-sql"]) == {
        "source_table": "T",
        "constraint_name": "fk",
        "target": "U",
        "reason": "undeclared",
    }
    assert _edges(result, "references") == {("unsupported.sql", "T"), ("unsupported.sql", "U")}


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
    assert [item.code for item in result.warnings] == [DiagnosticCode.ORPHANED_FOREIGN_KEY]


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


def test_column_less_references_keep_statements_and_record_no_column_payload(
    tmp_path: Path,
) -> None:
    sql = """\
CREATE TABLE dbo.Parent (id int PRIMARY KEY)
GO
CREATE TABLE dbo.Child (id int, pid int REFERENCES dbo.Parent)
GO
CREATE TABLE dbo.Child2 (id int, pid int, FOREIGN KEY (pid) REFERENCES dbo.Parent)
GO
CREATE TABLE dbo.Child3 (id int, pid int)
GO
ALTER TABLE dbo.Child3 ADD CONSTRAINT fk3 FOREIGN KEY (pid) REFERENCES dbo.Parent
GO
CREATE VIEW dbo.V AS SELECT id FROM dbo.Child
"""
    result = _analyze(tmp_path, **{"columnless.sql": sql})

    assert not result.diagnostics
    symbols = _symbols(result)
    assert {
        label: symbols[label].symbol_kind for label in ("dbo.Child", "dbo.Child2", "dbo.Child3")
    } == {
        "dbo.Child": "sql:table",
        "dbo.Child2": "sql:table",
        "dbo.Child3": "sql:table",
    }
    edges = _sql_edges(result, "sql:foreign-key-to")
    assert {(source.label, target.label) for source, target, _edge in edges} == {
        ("dbo.Child", "dbo.Parent"),
        ("dbo.Child2", "dbo.Parent"),
        ("dbo.Child3", "dbo.Parent"),
    }
    for _source, _target, edge in edges:
        assert all(record.extensions is None for record in edge.evidence)
        assert "foreign_key_columns" not in str(edge.to_dict())
    assert [component["size"] for component in _fk_components(result)] == [4]
    assert ("dbo.V", "dbo.Child") in _edges(result, "sql:reads-from")
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]


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
    assert not [d for d in result.diagnostics if d.code is DiagnosticCode.ORPHANED_FOREIGN_KEY]
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
    assert [d.code for d in result.diagnostics].count(DiagnosticCode.ORPHANED_FOREIGN_KEY) == 1


def test_standalone_fk_undeclared_source_records_orphan_and_file_origin_target(
    tmp_path: Path,
) -> None:
    sql = _go(
        "CREATE TABLE sales.Real (id int)",
        "ALTER TABLE sales.Ghost ADD CONSTRAINT fk1 FOREIGN KEY (x) REFERENCES sales.Nowhere (id)",
    )
    result = _analyze(tmp_path, **{"a.sql": sql})
    file_id = next(node.id for node in result.document.nodes if node.path == "a.sql")

    assert [(item.code, item.severity, item.path) for item in result.diagnostics] == [
        (DiagnosticCode.ORPHANED_FOREIGN_KEY, DiagnosticSeverity.WARNING, "a.sql")
    ]
    [warning] = result.diagnostics
    assert dict(warning.extensions["minotaur-sql"]) == {
        "source_table": "sales.Ghost",
        "constraint_name": "fk1",
        "target": "sales.Nowhere",
        "reason": "undeclared",
    }
    [(line, character)] = _reference_positions(sql, "ALTER TABLE sales.Ghost", "sales.Nowhere")
    assert (warning.location.range.start.line, warning.location.range.start.character) == (
        line,
        character,
    )
    references = _sql_edges(result, "references")
    assert all(source.id == file_id for source, _target, _edge in references)
    assert {target.label for _source, target, _edge in references} == {
        "sales.Ghost",
        "sales.Nowhere",
    }
    nowhere, nowhere_edge = next(
        (target, edge) for _source, target, edge in references if target.label == "sales.Nowhere"
    )
    assert nowhere.identity.basis is IdentityBasis.UNRESOLVED_REFERENCE
    assert nowhere.identity.originating_node == file_id
    assert _evidence_locations(nowhere_edge) == {("a.sql", line, character)}
    assert not _sql_edges(result, "sql:foreign-key-to")
    assert {node.label for node in result.document.nodes if node.symbol_kind} == {"sales.Real"}


def test_standalone_fk_ambiguous_source_keeps_diagnostic_and_orphan(tmp_path: Path) -> None:
    declarations = _go(
        "CREATE TABLE sales.Real (id int)",
        "CREATE TABLE sales.Ghost (x int)",
        "CREATE TABLE sales.Ghost (x int)",
    )
    sql = _go(
        "ALTER TABLE sales.Ghost ADD CONSTRAINT fk1 FOREIGN KEY (x) REFERENCES sales.Nowhere (id)"
    )
    result = _analyze(tmp_path, **{"tables.sql": declarations, "a.sql": sql})
    file_id = next(node.id for node in result.document.nodes if node.path == "a.sql")

    references = _sql_edges(result, "references")
    assert {(source.id, target.label) for source, target, _edge in references} == {
        (file_id, "sales.Ghost"),
        (file_id, "sales.Nowhere"),
    }
    assert not _sql_edges(result, "sql:foreign-key-to")
    [(line, character)] = _reference_positions(sql, "ALTER TABLE", "sales.Ghost")
    assert [
        (item.path, item.location.range.start.line, item.location.range.start.character)
        for item in result.diagnostics
        if item.code is DiagnosticCode.AMBIGUOUS_REFERENCE
    ] == [("a.sql", line, character)]
    assert [
        dict(item.extensions["minotaur-sql"])
        for item in result.diagnostics
        if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    ] == [
        {
            "source_table": "sales.Ghost",
            "constraint_name": "fk1",
            "target": "sales.Nowhere",
            "reason": "undeclared",
        }
    ]


def test_standalone_fk_undeclared_source_ambiguous_target_keeps_ambiguous_reason(
    tmp_path: Path,
) -> None:
    sql = _go(
        "CREATE TABLE sales.Nowhere (id int)",
        "CREATE TABLE sales.Nowhere (id int)",
        "ALTER TABLE sales.Ghost ADD CONSTRAINT fk1 FOREIGN KEY (x) REFERENCES sales.Nowhere (id)",
    )
    result = _analyze(tmp_path, **{"a.sql": sql})
    file_id = next(node.id for node in result.document.nodes if node.path == "a.sql")

    assert [
        dict(item.extensions["minotaur-sql"])
        for item in result.diagnostics
        if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    ] == [
        {
            "source_table": "sales.Ghost",
            "constraint_name": "fk1",
            "target": "sales.Nowhere",
            "reason": "ambiguous",
        }
    ]
    nowhere, nowhere_edge = next(
        (target, edge)
        for _source, target, edge in _sql_edges(result, "references")
        if target.label == "sales.Nowhere"
    )
    assert nowhere.identity.originating_node == file_id
    assert len(_evidence_locations(nowhere_edge)) == 1
    assert not _sql_edges(result, "sql:foreign-key-to")


def test_standalone_fk_undeclared_source_coalesces_targets_by_source_table(
    tmp_path: Path,
) -> None:
    sql = _go(
        "CREATE TABLE sales.Real (id int)",
        "ALTER TABLE sales.Ghost ADD CONSTRAINT fk1 FOREIGN KEY (x) REFERENCES sales.Nowhere (id)",
        "ALTER TABLE sales.Ghost ADD CONSTRAINT fk2 FOREIGN KEY (x) REFERENCES sales.Nowhere (id)",
        "ALTER TABLE sales.Ghost2 ADD CONSTRAINT fk3 FOREIGN KEY (x) REFERENCES sales.Nowhere (id)",
        "ALTER TABLE sales.Ghost ADD CONSTRAINT fk4 "
        "FOREIGN KEY (x) REFERENCES sales.Elsewhere (id)",
    )
    result = _analyze(tmp_path, **{"a.sql": sql})
    file_id = next(node.id for node in result.document.nodes if node.path == "a.sql")

    assert [
        (
            item.extensions["minotaur-sql"]["source_table"],
            item.extensions["minotaur-sql"]["constraint_name"],
            item.extensions["minotaur-sql"]["target"],
        )
        for item in result.diagnostics
        if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    ] == [
        ("sales.Ghost", "fk1", "sales.Nowhere"),
        ("sales.Ghost", "fk2", "sales.Nowhere"),
        ("sales.Ghost2", "fk3", "sales.Nowhere"),
        ("sales.Ghost", "fk4", "sales.Elsewhere"),
    ]
    unresolved = [
        node for node in result.document.nodes if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    ]
    assert {node.identity.originating_node for node in unresolved} == {file_id}
    nowhere_nodes = [node for node in unresolved if node.label == "sales.Nowhere"]
    elsewhere_nodes = [node for node in unresolved if node.label == "sales.Elsewhere"]
    assert len(nowhere_nodes) == 2
    assert len({node.id for node in nowhere_nodes}) == 2
    assert len(elsewhere_nodes) == 1
    target_edges = [
        (target.label, sorted(_evidence_locations(edge)))
        for _source, target, edge in _sql_edges(result, "references")
        if target.label in {"sales.Nowhere", "sales.Elsewhere"}
    ]
    ghost1 = _reference_positions(sql, "CONSTRAINT fk1", "sales.Nowhere")
    ghost2 = _reference_positions(sql, "CONSTRAINT fk2", "sales.Nowhere")
    other = _reference_positions(sql, "CONSTRAINT fk3", "sales.Nowhere")
    missing = _reference_positions(sql, "CONSTRAINT fk4", "sales.Elsewhere")
    assert sorted(target_edges) == sorted(
        [
            ("sales.Nowhere", [("a.sql", *position) for position in ghost1 + ghost2]),
            ("sales.Nowhere", [("a.sql", *position) for position in other]),
            ("sales.Elsewhere", [("a.sql", *position) for position in missing]),
        ]
    )


def test_standalone_fk_undeclared_source_keeps_mapped_target_reason(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        settings=SqlSettings(foreign_key_target_files={"sales.Nowhere": "mapped.sql"}),
        **{
            "a.sql": _go(
                "CREATE TABLE sales.Real (id int)",
                "ALTER TABLE sales.Ghost ADD CONSTRAINT fk1 "
                "FOREIGN KEY (x) REFERENCES sales.Nowhere (id)",
            ),
            "mapped.sql": "CREATE TABLE sales.Wrong (id int)",
        },
    )
    file_id = next(node.id for node in result.document.nodes if node.path == "a.sql")

    assert [
        (item.code, dict(item.extensions["minotaur-sql"]))
        for item in result.diagnostics
        if item.code is DiagnosticCode.ORPHANED_FOREIGN_KEY
    ] == [
        (
            DiagnosticCode.ORPHANED_FOREIGN_KEY,
            {
                "source_table": "sales.Ghost",
                "constraint_name": "fk1",
                "target": "sales.Nowhere",
                "reason": "extraction-gap",
            },
        )
    ]
    nowhere, _edge = next(
        (target, edge)
        for _source, target, edge in _sql_edges(result, "references")
        if target.label == "sales.Nowhere"
    )
    assert nowhere.identity.originating_node == file_id
    assert not _sql_edges(result, "sql:foreign-key-to")


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


def _go(*statements: str) -> str:
    return "\nGO\n".join(statements) + "\n"


def _reference_positions(sql: str, owner: str, target: str) -> list[tuple[int, int]]:
    """Return (line, character) of each ``target`` reference in ``owner``'s statement."""
    start = sql.index(owner)
    end = sql.find("\nGO\n", start)
    end = len(sql) if end < 0 else end
    positions = []
    for match in re.finditer(rf"{re.escape(target)}\b", sql[start:end]):
        offset = start + match.start()
        line = sql.count("\n", 0, offset)
        positions.append((line, offset - (sql.rfind("\n", 0, offset) + 1)))
    return positions


def _read_locations(result, kind: str = "sql:reads-from"):
    return {
        (source.label, target.label, line, character)
        for source, target, edge in _sql_edges(result, kind)
        for (_path, line, character) in _evidence_locations(edge)
    }


def test_each_nested_reference_is_read_once_and_keeps_every_self_join_location(
    tmp_path: Path,
) -> None:
    view = """\
CREATE VIEW v AS
SELECT (SELECT MAX(x) FROM dbo.Dup) AS s
FROM (SELECT x FROM (SELECT x FROM (SELECT x FROM
dbo.Dup) AS d3) AS d2) AS d1
WHERE x IN (SELECT x FROM (SELECT x FROM dbo.Dup) AS d4)
AND EXISTS (SELECT 1 FROM dbo.Dup)
AND x > ALL (SELECT x FROM dbo.Dup)
AND x = SOME (SELECT x FROM dbo.Dup)"""
    sql = _go("CREATE TABLE dbo.Dup (x int)", "CREATE TABLE dbo.Dup (x int)", view)
    result = _analyze(tmp_path, **{"dup.sql": sql})

    ambiguous = sorted(
        (item.location.range.start.line, item.location.range.start.character)
        for item in result.diagnostics
        if item.code is DiagnosticCode.AMBIGUOUS_REFERENCE
    )
    assert ambiguous == _reference_positions(sql, "CREATE VIEW v AS", "dbo.Dup")
    assert len(ambiguous) == 6

    self_join = _analyze(
        tmp_path / "self",
        **{
            "self.sql": _go(
                "CREATE TABLE dbo.a (x int)",
                "CREATE VIEW v_self AS SELECT a1.x FROM dbo.a AS a1 "
                "JOIN dbo.a AS a2 ON a1.x = a2.x",
            )
        },
    )
    [(_, _, edge)] = _sql_edges(self_join, "sql:reads-from")
    self_sql = (tmp_path / "self" / "self.sql").read_text(encoding="utf-8")
    assert sorted(
        (line, character) for (_path, line, character) in _evidence_locations(edge)
    ) == _reference_positions(self_sql, "CREATE VIEW v_self", "dbo.a")
    assert len(_evidence_locations(edge)) == 2
    assert _edges(self_join, "sql:reads-from") == {("v_self", "dbo.a")}


@pytest.mark.parametrize("references", [2, 10])
def test_chained_join_derived_tables_report_each_ambiguity_once(
    tmp_path: Path, references: int
) -> None:
    joins = " ".join(f"JOIN (SELECT x FROM dbo.Dup) t{index}" for index in range(1, references + 1))
    conditions = " ".join("ON 1 = 1" for _ in range(references))
    view = f"CREATE VIEW v AS SELECT 1 AS y FROM dbo.a {joins} {conditions}"
    sql = _go(
        "CREATE TABLE dbo.a (x int)",
        "CREATE TABLE dbo.Dup (x int)",
        "CREATE TABLE dbo.Dup (x int)",
        view,
    )
    result = _analyze(tmp_path / str(references), **{"chain.sql": sql})

    ambiguous = sorted(
        (item.location.range.start.line, item.location.range.start.character)
        for item in result.diagnostics
        if item.code is DiagnosticCode.AMBIGUOUS_REFERENCE
    )
    assert ambiguous == _reference_positions(sql, "CREATE VIEW v AS", "dbo.Dup")
    assert len(ambiguous) == references


def test_derived_table_join_chain_collects_each_attached_source(tmp_path: Path) -> None:
    declared = _analyze(
        tmp_path / "declared",
        **{
            "chain.sql": _go(
                "CREATE TABLE dbo.a (x int)",
                "CREATE TABLE dbo.b (x int)",
                "CREATE TABLE dbo.c (x int)",
                "CREATE VIEW v AS SELECT a.x FROM dbo.a "
                "JOIN (SELECT x FROM dbo.b) b1 JOIN dbo.c c1 ON 1 = 1 ON 1 = 1",
            )
        },
    )
    assert not declared.diagnostics
    assert _edges(declared, "sql:reads-from") == {
        ("v", "dbo.a"),
        ("v", "dbo.b"),
        ("v", "dbo.c"),
    }

    temporary = _analyze(
        tmp_path / "temporary",
        **{
            "chain.sql": "CREATE VIEW v AS SELECT a.x FROM dbo.a "
            "JOIN (SELECT x FROM dbo.b) b1 JOIN #t t ON 1 = 1 ON 1 = 1"
        },
    )
    assert not _symbols(temporary)
    assert not _edges(temporary, "sql:reads-from")
    assert [item.code for item in temporary.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_wrapper_join_position_rejects_a_non_query_body(tmp_path: Path) -> None:
    body = (
        "SELECT a.x FROM ((SELECT x FROM dbo.a) a1 "
        "JOIN (dbo.b b1 JOIN dbo.c c1 ON 1 = 1) ON a1.x = b1.x)"
    )
    view = _analyze(tmp_path / "view", **{"wrapper.sql": f"CREATE VIEW v AS {body}"})
    assert not _symbols(view)
    assert not _edges(view, "sql:reads-from")
    assert [item.code for item in view.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]

    procedure = _analyze(
        tmp_path / "procedure",
        **{"wrapper.sql": f"CREATE PROCEDURE p AS BEGIN {body}; END"},
    )
    assert set(_symbols(procedure)) == {"p"}
    assert not _edges(procedure, "sql:reads-from")
    assert [item.code for item in procedure.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_grouped_wrapper_sources_are_read_and_temporary_sources_reject(
    tmp_path: Path,
) -> None:
    declared = _analyze(
        tmp_path / "declared",
        **{
            "grouped.sql": _go(
                "CREATE TABLE dbo.a (x int)",
                "CREATE TABLE dbo.t (x int)",
                "CREATE VIEW v AS SELECT * FROM ((SELECT x FROM dbo.a) AS s JOIN dbo.t ON 1 = 1)",
            )
        },
    )
    assert not declared.diagnostics
    assert _edges(declared, "sql:reads-from") == {("v", "dbo.a"), ("v", "dbo.t")}

    temporary = _analyze(
        tmp_path / "temporary",
        **{
            "grouped.sql": "CREATE VIEW v AS SELECT * FROM "
            "((SELECT x FROM dbo.a) AS s JOIN #t ON 1 = 1)"
        },
    )
    assert not _symbols(temporary)
    assert [item.code for item in temporary.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]

    existence = _analyze(
        tmp_path / "existence",
        **{
            "grouped.sql": "CREATE VIEW v AS SELECT 1 AS y FROM dbo.a WHERE EXISTS "
            "(SELECT 1 AS y FROM ((SELECT x FROM dbo.a) a1 JOIN #t ON 1 = 1))"
        },
    )
    assert not _symbols(existence)
    assert [item.code for item in existence.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_deep_grouped_join_sources_are_read_at_their_locations(tmp_path: Path) -> None:
    sql = _go(
        "CREATE TABLE dbo.a (x int)",
        "CREATE TABLE dbo.t (x int)",
        "CREATE TABLE dbo.u (x int)",
        "CREATE VIEW v AS SELECT * FROM "
        "(((SELECT x FROM dbo.a) AS s JOIN dbo.t ON 1 = 1) JOIN dbo.u ON 1 = 1)",
    )
    result = _analyze(tmp_path, **{"deep.sql": sql})
    edges = {
        (source.label, target.label): edge
        for source, target, edge in _sql_edges(result, "sql:reads-from")
    }

    assert not result.diagnostics
    assert set(edges) == {("v", "dbo.a"), ("v", "dbo.t"), ("v", "dbo.u")}
    for name in ("dbo.a", "dbo.t", "dbo.u"):
        assert _evidence_locations(edges[("v", name)]) == {
            ("deep.sql", *position)
            for position in _reference_positions(sql, "CREATE VIEW v AS", name)
        }


def test_deep_grouped_join_temporary_source_rejects(tmp_path: Path) -> None:
    view = _analyze(
        tmp_path / "view",
        **{
            "deep.sql": "CREATE VIEW v AS SELECT * FROM "
            "(((SELECT x FROM dbo.a) AS s JOIN #t ON 1 = 1) JOIN dbo.u ON 1 = 1)"
        },
    )
    assert not _symbols(view)
    assert not _edges(view, "sql:reads-from")
    assert [item.code for item in view.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]

    existence = _analyze(
        tmp_path / "existence",
        **{
            "deep.sql": "CREATE VIEW v AS SELECT 1 AS y FROM dbo.a WHERE EXISTS "
            "(SELECT 1 AS y FROM (((SELECT x FROM dbo.a) AS s JOIN #t ON 1 = 1) "
            "JOIN dbo.u ON 1 = 1))"
        },
    )
    assert not _symbols(existence)
    assert [item.code for item in existence.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_from_prefixed_grouped_join_chain_reads_every_source(tmp_path: Path) -> None:
    declared = _analyze(
        tmp_path / "declared",
        **{
            "prefixed.sql": _go(
                "CREATE TABLE dbo.g (x int)",
                "CREATE TABLE dbo.a (x int)",
                "CREATE TABLE dbo.t (x int)",
                "CREATE TABLE dbo.u (x int)",
                "CREATE VIEW v AS SELECT * FROM dbo.g "
                "JOIN ((SELECT x FROM dbo.a) AS s JOIN dbo.t ON 1 = 1) "
                "JOIN dbo.u ON 1 = 1 ON 1 = 1",
            )
        },
    )
    assert not declared.diagnostics
    assert _edges(declared, "sql:reads-from") == {
        ("v", "dbo.g"),
        ("v", "dbo.a"),
        ("v", "dbo.t"),
        ("v", "dbo.u"),
    }

    temporary = _analyze(
        tmp_path / "temporary",
        **{
            "prefixed.sql": "CREATE VIEW v AS SELECT * FROM dbo.g "
            "JOIN ((SELECT x FROM dbo.a) AS s JOIN #t ON 1 = 1) "
            "JOIN dbo.u ON 1 = 1 ON 1 = 1"
        },
    )
    assert not _symbols(temporary)
    assert not _edges(temporary, "sql:reads-from")
    assert [item.code for item in temporary.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]


def test_grouped_wrapper_body_is_read_once(tmp_path: Path) -> None:
    sql = _go(
        "CREATE TABLE dbo.Dup (x int)",
        "CREATE TABLE dbo.Dup (x int)",
        "CREATE VIEW v AS SELECT * FROM ((SELECT x FROM dbo.Dup) AS s)",
    )
    result = _analyze(tmp_path, **{"grouped.sql": sql})

    ambiguous = sorted(
        (item.location.range.start.line, item.location.range.start.character)
        for item in result.diagnostics
        if item.code is DiagnosticCode.AMBIGUOUS_REFERENCE
    )
    assert ambiguous == _reference_positions(sql, "CREATE VIEW v AS", "dbo.Dup")
    assert len(ambiguous) == 1


def test_ordered_parenthesized_with_clauses_read_outermost_first(tmp_path: Path) -> None:
    sql = _go(
        "CREATE TABLE dbo.o (x int)",
        "CREATE TABLE dbo.i (x int)",
        "CREATE VIEW v AS WITH o AS (SELECT x FROM dbo.o) "
        "(WITH i AS (SELECT x FROM dbo.i) SELECT * FROM o JOIN i ON 1 = 1) "
        "ORDER BY x OFFSET 0 ROWS",
    )
    result = _analyze(tmp_path, **{"nested_with.sql": sql})

    assert not result.diagnostics
    assert [target.label for _source, target, _edge in _sql_edges(result, "sql:reads-from")] == [
        "dbo.o",
        "dbo.i",
    ]


def test_nested_with_and_discovered_scopes_see_enclosing_cte_names(tmp_path: Path) -> None:
    sql = _go(
        "CREATE TABLE dbo.a (x int)",
        "CREATE TABLE dbo.b (x int)",
        "CREATE VIEW v_nested AS SELECT x FROM (WITH c AS (SELECT x FROM dbo.a) "
        "SELECT x FROM (SELECT x FROM c) AS e) AS d",
        "CREATE VIEW v_recent AS WITH recent AS (SELECT x FROM dbo.a) SELECT x FROM dbo.b "
        "WHERE x IN (SELECT x FROM recent) AND EXISTS (SELECT 1 FROM recent)",
    )
    result = _analyze(tmp_path, **{"cte.sql": sql})

    assert _edges(result, "sql:reads-from") == {
        ("v_nested", "dbo.a"),
        ("v_recent", "dbo.a"),
        ("v_recent", "dbo.b"),
    }
    assert _edges(result, "references") == set()
    assert {
        node.label
        for node in result.document.nodes
        if node.node_class.value == "unresolved-reference"
    } == set()
    assert result.diagnostics == ()


# Measured 1,750 calls on Python 3.12.3; bound is 3x rounded up to 1,000.
_CTE_CHAIN_CALL_BOUND = 6_000


def test_cte_chain_analysis_is_linear_in_calls(tmp_path: Path) -> None:
    ctes = ["c0 AS (SELECT x FROM dbo.a)", "c1 AS (SELECT x FROM c0)"]
    ctes += [
        f"c{i} AS (SELECT x FROM c{i - 1} UNION ALL SELECT x FROM c{i - 2})" for i in range(2, 24)
    ]
    sql = _go(
        "CREATE TABLE dbo.a (x int)",
        "CREATE VIEW v AS WITH " + ", ".join(ctes) + " SELECT x FROM c23",
    )
    path = tmp_path / "chain.sql"
    path.write_text(sql, encoding="utf-8")

    result, calls = _count_module_calls(
        sql_interpreter.__file__, lambda: analyze_sql_files(Workspace(tmp_path), (path,))
    )

    assert _edges(result, "sql:reads-from") == {("v", "dbo.a")}  # type: ignore[attr-defined]
    assert result.diagnostics == ()  # type: ignore[attr-defined]
    assert calls <= _CTE_CHAIN_CALL_BOUND


_CALLER_BUDGET = 800
_CHAIN_TERMS = 2_000


def _frame_depth() -> int:
    depth = 0
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def test_long_boolean_and_set_operation_chains_read_under_a_caller_budget(tmp_path: Path) -> None:
    def or_chain(table: str) -> str:
        return f"SELECT x FROM dbo.{table} WHERE " + " OR ".join(
            f"x = {i}" for i in range(_CHAIN_TERMS)
        )

    def union_chain(table: str) -> str:
        return " UNION ALL ".join(f"SELECT x FROM dbo.{table}" for _ in range(_CHAIN_TERMS))

    condition = " OR ".join(f"x = {i}" for i in range(_CHAIN_TERMS))
    sql = _go(
        *(f"CREATE TABLE dbo.t{i} (x int)" for i in range(1, 6)),
        f"CREATE VIEW v1 AS WITH c AS ({or_chain('t1')}) SELECT x FROM c",
        f"CREATE PROCEDURE p2 AS BEGIN\nIF {condition}\nSELECT x FROM dbo.t2;\nEND",
        f"CREATE VIEW v3 AS {union_chain('t3')}",
        f"CREATE VIEW v4 AS WITH c AS ({union_chain('t4')}) SELECT x FROM c",
        f"CREATE VIEW v5 AS SELECT 1 AS y WHERE EXISTS ({union_chain('t5')})",
    )
    path = tmp_path / "chains.sql"
    path.write_text(sql, encoding="utf-8")

    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(_frame_depth() + _CALLER_BUDGET)
    try:
        result = analyze_sql_files(Workspace(tmp_path), (path,))
    finally:
        sys.setrecursionlimit(previous)

    assert result.diagnostics == ()
    edges = {(s.label, t.label): e for s, t, e in _sql_edges(result, "sql:reads-from")}
    assert set(edges) == {
        ("v1", "dbo.t1"),
        ("p2", "dbo.t2"),
        ("v3", "dbo.t3"),
        ("v4", "dbo.t4"),
        ("v5", "dbo.t5"),
    }
    for owner in ("v3", "v4", "v5"):
        [target] = [key for key in edges if key[0] == owner]
        assert len(_evidence_locations(edges[target])) == _CHAIN_TERMS


def test_nested_parenthesized_and_table_function_argument_queries_contribute_reads(
    tmp_path: Path,
) -> None:
    statements = [
        "CREATE TABLE dbo.a (x int)",
        "CREATE TABLE dbo.b (x int)",
        "CREATE TABLE dbo.c (x int)",
        "CREATE FUNCTION dbo.tf(@v int) RETURNS TABLE AS RETURN SELECT @v AS x",
        "CREATE VIEW v_exists AS SELECT x FROM dbo.a WHERE EXISTS (SELECT 1 FROM dbo.b)",
        "CREATE VIEW v_all AS SELECT x FROM dbo.a WHERE x > ALL (SELECT x FROM dbo.b)",
        "CREATE VIEW v_some AS SELECT x FROM dbo.a WHERE x = SOME (SELECT x FROM dbo.b)",
        "CREATE VIEW v_any AS SELECT x FROM dbo.a WHERE x = ANY (SELECT x FROM dbo.b)",
        "CREATE VIEW v_join AS SELECT a.x FROM dbo.a JOIN dbo.b ON a.x IN (SELECT x FROM dbo.c)",
        "CREATE VIEW v_paren AS (SELECT x FROM dbo.a)",
        "CREATE VIEW v_paren_union AS (SELECT x FROM dbo.a) UNION (SELECT x FROM dbo.b)",
        "CREATE VIEW v_cte_union AS WITH u AS ((SELECT x FROM dbo.a) UNION (SELECT x FROM dbo.b)) "
        "SELECT x FROM u",
        "CREATE VIEW v_tvf_from AS SELECT t.x FROM dbo.tf((SELECT MAX(x) FROM dbo.c)) AS t",
        "CREATE VIEW v_tvf_join AS SELECT a.x FROM dbo.a JOIN dbo.tf((SELECT MAX(x) FROM dbo.c)) "
        "AS t ON a.x = t.x",
        "CREATE VIEW v_split AS SELECT value FROM "
        "STRING_SPLIT((SELECT MAX(CAST(x AS varchar(10))) FROM dbo.c), ',')",
        "CREATE PROCEDURE p AS BEGIN\n(SELECT x FROM dbo.a) UNION (SELECT x FROM dbo.b);\n"
        "SELECT x FROM dbo.c;\nEND",
    ]
    sql = _go(*statements)
    result = _analyze(tmp_path, **{"nested.sql": sql})

    expected_reads = {
        (owner, target, line, character)
        for owner, tables in {
            "v_exists": ("a", "b"),
            "v_all": ("a", "b"),
            "v_some": ("a", "b"),
            "v_any": ("a", "b"),
            "v_join": ("a", "b", "c"),
            "v_paren": ("a",),
            "v_paren_union": ("a", "b"),
            "v_cte_union": ("a", "b"),
            "v_tvf_from": ("c",),
            "v_tvf_join": ("a", "c"),
            "v_split": ("c",),
            "p": ("a", "b", "c"),
        }.items()
        for table in tables
        for target in [f"dbo.{table}"]
        for line, character in _reference_positions(
            sql, f"CREATE {'PROCEDURE' if owner == 'p' else 'VIEW'} {owner} AS", target
        )
    }
    assert _read_locations(result) == expected_reads
    expected_calls = {
        (owner, "dbo.tf", line, character)
        for owner in ("v_tvf_from", "v_tvf_join")
        for line, character in _reference_positions(sql, f"CREATE VIEW {owner} AS", "tf")
    }
    assert _read_locations(result, "sql:calls") == expected_calls
    assert _edges(result, "sql:calls") == {("v_tvf_from", "dbo.tf"), ("v_tvf_join", "dbo.tf")}
    assert result.diagnostics == ()


def test_nested_exists_read_composes_into_view_depth_warning(tmp_path: Path) -> None:
    sql = _go(
        "CREATE TABLE dbo.a (x int)",
        "CREATE VIEW mid AS SELECT x FROM dbo.a",
        "CREATE VIEW top_v AS SELECT 1 AS y WHERE EXISTS (SELECT 1 FROM mid)",
    )
    result = _analyze(tmp_path, settings=SqlSettings(view_depth_threshold=1), **{"depth.sql": sql})

    [warning] = result.warnings
    assert warning.code is DiagnosticCode.VIEW_DEPTH_WARNING
    assert warning.message == "view dependency depth 2 exceeds threshold 1: top_v -> mid -> dbo.a"


def test_nested_queries_follow_the_same_rejection_rule_in_every_position(tmp_path: Path) -> None:
    views = {
        "v_exists_temp": "SELECT x FROM dbo.a WHERE EXISTS (SELECT 1 FROM #t)",
        "v_all_temp": "SELECT x FROM dbo.a WHERE x > ALL (SELECT x FROM #t)",
        "v_some_temp": "SELECT x FROM dbo.a WHERE x = SOME (SELECT x FROM #t)",
        "v_exists_values": "SELECT x FROM dbo.a WHERE EXISTS "
        "(SELECT t.x FROM (VALUES (1)) AS t(x))",
        "v_exists_apply": "SELECT x FROM dbo.a WHERE EXISTS "
        "(SELECT b.x FROM dbo.b CROSS APPLY (SELECT x FROM dbo.c) AS t)",
        "v_exists_into": "SELECT x FROM dbo.a WHERE EXISTS (SELECT x INTO #z FROM dbo.b)",
        "v_from_values": "SELECT x FROM (VALUES (1)) AS t(x)",
        "v_cross_apply": "SELECT a.x FROM dbo.a CROSS APPLY (SELECT x FROM dbo.b) AS t",
        "v_in_temp": "SELECT x FROM dbo.a WHERE x IN (SELECT x FROM #t)",
        "v_tvf_temp": "SELECT t.x FROM dbo.tf((SELECT MAX(x) FROM #t)) AS t",
        "v_tvf_join_temp": "SELECT a.x FROM dbo.a JOIN dbo.tf((SELECT MAX(x) FROM #t)) AS t "
        "ON a.x = t.x",
    }
    procedure = """\
CREATE PROCEDURE p AS BEGIN
SELECT dbo.f(x) FROM dbo.a WHERE EXISTS (SELECT 1 FROM #t);
SELECT x FROM dbo.b WHERE x > ALL (SELECT x FROM #t);
SELECT x FROM dbo.c WHERE x = SOME (SELECT x FROM #t);
(SELECT x FROM dbo.a);
SELECT x FROM dbo.d WHERE x IN (SELECT x FROM #t);
SELECT x FROM dbo.d;
END"""
    sql = _go(
        *(f"CREATE TABLE dbo.{name} (x int)" for name in "abcd"),
        "CREATE FUNCTION dbo.f(@v int) RETURNS int AS RETURN @v",
        "CREATE FUNCTION dbo.tf(@v int) RETURNS TABLE AS RETURN SELECT @v AS x",
        procedure,
        *(f"CREATE VIEW {name} AS {body}" for name, body in views.items()),
    )
    result = _analyze(tmp_path, **{"reject.sql": sql})

    lines = sql.split("\n")
    last_root_line = max(i for i, text in enumerate(lines) if text == "SELECT x FROM dbo.d;")
    assert _read_locations(result) == {("p", "dbo.d", last_root_line, 14)}
    assert _edges(result, "sql:calls") == set()
    assert not [label for label in _symbols(result) if label.startswith("v_")]
    procedure_start = lines.index("CREATE PROCEDURE p AS BEGIN")
    expected_lines = sorted(
        [procedure_start + offset for offset in range(1, 6)]
        + [lines.index(f"CREATE VIEW {name} AS {body}") for name, body in views.items()]
    )
    assert (
        sorted(
            item.location.range.start.line
            for item in result.diagnostics
            if item.code is DiagnosticCode.UNSUPPORTED_SYNTAX
        )
        == expected_lines
    )
    assert len(result.diagnostics) == 16


def test_repeated_parentheses_unwrap_and_non_query_scopes_reject(tmp_path: Path) -> None:
    views = {
        "v_double": "((SELECT x FROM dbo.a))",
        "v_cte_triple": "WITH c AS (((SELECT x FROM dbo.a))) SELECT x FROM c",
        "v_cte_values": "WITH c AS ((VALUES (1))) SELECT x FROM c",
        "v_derived_values": "SELECT t.x FROM ((VALUES (1))) AS t(x)",
    }
    sql = _go(
        "CREATE TABLE dbo.a (x int)",
        *(f"CREATE VIEW {name} AS {body}" for name, body in views.items()),
    )
    result = _analyze(tmp_path, **{"paren.sql": sql})

    assert _read_locations(result) == {
        (owner, "dbo.a", line, character)
        for owner in ("v_double", "v_cte_triple")
        for line, character in _reference_positions(sql, f"CREATE VIEW {owner} AS", "dbo.a")
    }
    assert {label for label in _symbols(result) if label.startswith("v_")} == {
        "v_double",
        "v_cte_triple",
    }
    lines = sql.split("\n")
    assert [(item.code, item.location.range.start.line) for item in result.diagnostics] == [
        (DiagnosticCode.UNSUPPORTED_SYNTAX, lines.index(f"CREATE VIEW {name} AS {views[name]}"))
        for name in ("v_cte_values", "v_derived_values")
    ]


_PAGED_UNION = (
    "SELECT x FROM dbo.a UNION SELECT x FROM dbo.b ORDER BY x OFFSET (SELECT COUNT(*) FROM {}) ROWS"
)
_PAGED_WRAPPER = "(SELECT x FROM dbo.a) ORDER BY x OFFSET (SELECT COUNT(*) FROM {}) ROWS"


@pytest.mark.parametrize(
    ("view", "tables"),
    [
        ("SELECT d.x FROM (" + _PAGED_UNION + ") AS d", {"a", "b", "p"}),
        ("WITH c AS (" + _PAGED_UNION + ") SELECT x FROM c", {"a", "b", "p"}),
        ("SELECT x FROM dbo.a WHERE x IN (" + _PAGED_UNION + ")", {"a", "b", "p"}),
        ("SELECT d.x FROM (" + _PAGED_WRAPPER + ") AS d", {"a", "p"}),
        (_PAGED_UNION, {"a", "b", "p"}),
        (
            "WITH r AS (SELECT x FROM dbo.a) SELECT x FROM r UNION SELECT x FROM r "
            "ORDER BY x OFFSET (SELECT COUNT(*) FROM r) ROWS",
            {"a"},
        ),
    ],
)
def test_paging_clause_queries_of_set_operations_and_wrappers_contribute_reads(
    tmp_path: Path, view: str, tables: set[str]
) -> None:
    source = _go(
        *(f"CREATE TABLE dbo.{name} (x int)" for name in "abp"),
        "CREATE VIEW v AS " + view.format("dbo.p"),
    )
    result = _analyze(tmp_path, **{"paging.sql": source})

    assert _edges(result, "sql:reads-from") == {("v", f"dbo.{name}") for name in tables}
    assert result.diagnostics == ()
    assert not [
        node for node in result.document.nodes if node.node_class.value == "unresolved-reference"
    ]


def test_paging_clause_queries_in_a_procedure_derived_table_contribute_reads(
    tmp_path: Path,
) -> None:
    source = _go(
        *(f"CREATE TABLE dbo.{name} (x int)" for name in "abp"),
        "CREATE PROCEDURE q AS BEGIN\nSELECT d.x FROM ("
        + _PAGED_UNION.format("dbo.p")
        + ") AS d;\nEND",
    )
    result = _analyze(tmp_path, **{"proc.sql": source})

    assert _edges(result, "sql:reads-from") == {("q", f"dbo.{name}") for name in "abp"}
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "view",
    [
        "SELECT d.x FROM (" + _PAGED_UNION + ") AS d",
        "WITH c AS (" + _PAGED_UNION + ") SELECT x FROM c",
        "SELECT x FROM dbo.a WHERE x IN (" + _PAGED_UNION + ")",
        "SELECT d.x FROM (" + _PAGED_WRAPPER + ") AS d",
        _PAGED_WRAPPER,
        _PAGED_UNION,
    ],
)
def test_temporary_table_in_a_paging_clause_rejects_the_statement(
    tmp_path: Path, view: str
) -> None:
    source = _go(
        *(f"CREATE TABLE dbo.{name} (x int)" for name in "ab"),
        "CREATE VIEW v AS " + view.format("#t"),
    )
    result = _analyze(tmp_path, **{"paging.sql": source})

    assert "v" not in _symbols(result)
    assert _edges(result, "sql:reads-from") == set()
    assert [item.code for item in result.diagnostics] == [DiagnosticCode.UNSUPPORTED_SYNTAX]
