"""Persisted SQL facts remain visible only to their existing query vocabularies."""

from __future__ import annotations

from pathlib import Path

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import load_graph_blob
from minotaur.graph_model.serialization import serialize
from minotaur.language_interpreter.sql import analyze_sql_files
from minotaur.language_interpreter.workspace import Workspace
from minotaur.query import system as system_query
from minotaur.query.diff import diff
from minotaur.query.impact import ImpactRecord, impact
from minotaur.query.index import AmbiguousSymbol, GraphIndex
from minotaur.query.sql import CURRENT_SQL_DEPENDENCY_KINDS
from minotaur.query.symbols import callers, definitions
from minotaur.query.unreferenced import unreferenced
from minotaur.system import load_systems_data

_CATALOG = """\
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
CREATE VIEW WrongKind AS SELECT 1
GO
CREATE TABLE ChildRef (id int REFERENCES WrongKind(id))
GO
CREATE TABLE UnrelatedRef (id int REFERENCES WrongKindish(id))
GO
CREATE VIEW MissingReader AS SELECT * FROM MissingSchema.Target
"""


def _persisted_index(root: Path, content: str = _CATALOG) -> GraphIndex:
    return GraphIndex.build(_persisted_document(root, content))


def _persisted_document(root: Path, content: str = _CATALOG) -> GraphDocument:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "catalog.sql"
    path.write_text(content, encoding="utf-8")
    result = analyze_sql_files(Workspace(root), (path,))
    assert result.diagnostics == ()

    encoded = serialize(result.document)
    loaded = load_graph_blob(encoded)
    assert serialize(loaded.document) == encoded
    return loaded.document


def _sql_facts(index: GraphIndex) -> set[tuple[str, str, str]]:
    return {
        (edge.kind, index.nodes[edge.source].label, index.nodes[edge.target].label)
        for edges in index.relationships_by_kind.values()
        for edge in edges
        if edge.kind.startswith("sql:")
    }


def test_persisted_sql_facts_use_definitions_and_diff_without_generic_query_aliases(
    tmp_path: Path,
) -> None:
    index = _persisted_index(tmp_path / "current")

    records = definitions(index, "Parent")
    assert [(record.symbol, record.kind) for record in records] == [("S.Parent", "sql:table")]
    assert [(record.symbol, record.kind) for record in definitions(index, "Reader")] == [
        ("S.Reader", "sql:view")
    ]
    assert [(record.symbol, record.kind) for record in definitions(index, "S")] == [
        ("S", "sql:schema")
    ]
    assert _sql_facts(index) == {
        ("sql:foreign-key-to", "S.Child", "S.Parent"),
        ("sql:reads-from", "S.Reader", "S.Parent"),
    }

    changes = diff(
        _persisted_document(
            tmp_path / "old",
            "CREATE SCHEMA S\nGO\nCREATE TABLE S.Parent (id int)\n",
        ),
        _persisted_document(
            tmp_path / "new",
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
""",
        ),
    )

    assert {(change.kind, change.symbol) for change in changes.added} == {
        ("sql:table", "S.Child"),
        ("sql:view", "S.Reader"),
    }
    assert {
        (change.kind, change.source, change.target) for change in changes.relationships_added
    } == {
        ("sql:foreign-key-to", "S.Child", "S.Parent"),
        ("sql:reads-from", "S.Reader", "S.Parent"),
    }


def test_sql_relationships_feed_callers_and_core_recall_remains(
    tmp_path: Path,
) -> None:
    index = _persisted_index(tmp_path / "current")

    assert CURRENT_SQL_DEPENDENCY_KINDS == ("sql:reads-from", "sql:foreign-key-to")
    assert [(record.symbol, record.kind) for record in definitions(index, "parent")] == [
        ("S.Parent", "sql:table")
    ]
    assert {
        (record.caller, record.kind, record.unresolved) for record in callers(index, "s.parent")
    } == {
        ("S.Child", "sql:foreign-key-to", False),
        ("S.Reader", "sql:reads-from", False),
    }
    impact_index = _persisted_index(
        tmp_path / "impact",
        """\
CREATE SCHEMA S
GO
CREATE TABLE S.Parent (id int)
GO
CREATE VIEW S.Reader AS SELECT * FROM S.Parent
GO
CREATE VIEW S.Downstream AS SELECT * FROM S.Reader
""",
    )
    assert impact(impact_index, "s.parent") == (
        ImpactRecord(depth=0, symbol="S.Parent", kind="sql:table"),
        ImpactRecord(depth=1, symbol="S.Reader", kind="sql:view"),
        ImpactRecord(depth=2, symbol="S.Downstream", kind="sql:view"),
    )
    assert impact(impact_index, "s.parent", max_depth=1) == (
        ImpactRecord(depth=0, symbol="S.Parent", kind="sql:table"),
        ImpactRecord(depth=1, symbol="S.Reader", kind="sql:view"),
        ImpactRecord(depth=2, symbol="S.Downstream", kind="sql:view", boundary=True),
    )
    assert unreferenced(index, tmp_path, ("catalog.sql",)) == ()

    systems = load_systems_data(
        {
            Path("sql.toml"): {
                "schema_version": 1,
                "name": "sql",
                "files": ["catalog.sql"],
            }
        }
    )
    target = systems[0]
    assert system_query.surface(systems, index, target) == ()
    assert system_query.consumers(systems, index, target) == ()
    assert system_query.system_deps(systems, index, target) == ()

    unresolved = callers(index, "wrongkind")
    assert len(unresolved) == 1
    assert unresolved[0].caller == "ChildRef"
    assert unresolved[0].kind == "references"
    assert unresolved[0].reference == "WrongKind"
    assert unresolved[0].unresolved is True


def test_sql_resolution_is_exact_first_and_ambiguous_after_casefold_fallback(
    tmp_path: Path,
) -> None:
    index = _persisted_index(tmp_path / "current")
    assert index.resolve("S.Parent").label == "S.Parent"
    assert index.resolve("s.parent").label == "S.Parent"

    root = tmp_path / "ambiguous"
    root.mkdir()
    path = root / "catalog.sql"
    path.write_text(
        "CREATE TABLE S.Parent (id int)\nGO\nCREATE TABLE s.PARENT (id int)\n",
        encoding="utf-8",
    )
    result = analyze_sql_files(Workspace(root), (path,))
    loaded = load_graph_blob(serialize(result.document))
    ambiguous = GraphIndex.build(loaded.document)

    with pytest.raises(AmbiguousSymbol) as excinfo:
        ambiguous.resolve("S.pArEnT")
    assert excinfo.value.candidates == ("catalog.sql:1", "catalog.sql:3")


def test_fk_mapping_survives_roundtrip_without_changing_query_or_diff_identity(
    tmp_path: Path,
) -> None:
    old_content = """\
CREATE TABLE Parent (x int, y int)
GO
CREATE TABLE Child (a int, b int)
GO
ALTER TABLE Child ADD CONSTRAINT FK_child_parent FOREIGN KEY (a,b) REFERENCES Parent(x,y)
"""
    new_content = old_content.replace(
        "FOREIGN KEY (a,b) REFERENCES Parent(x,y)",
        "FOREIGN KEY (b,a) REFERENCES Parent(y,x)",
    )
    old_document = _persisted_document(tmp_path / "old", old_content)
    new_document = _persisted_document(tmp_path / "new", new_content)
    index = GraphIndex.build(new_document)

    edge = next(
        relationship
        for relationship in new_document.relationships
        if relationship.kind == "sql:foreign-key-to"
    )
    assert edge.evidence[0].to_dict()["extensions"] == {
        "minotaur-sql": {
            "foreign_key_columns": [
                {"local": "b", "referenced": "y"},
                {"local": "a", "referenced": "x"},
            ]
        }
    }
    assert [(record.symbol, record.kind) for record in definitions(index, "Parent")] == [
        ("Parent", "sql:table")
    ]
    assert [(record.caller, record.kind) for record in callers(index, "parent")] == [
        ("Child", "sql:foreign-key-to")
    ]
    assert impact(index, "Parent", max_depth=1) == (
        ImpactRecord(depth=0, symbol="Parent", kind="sql:table"),
        ImpactRecord(depth=1, symbol="Child", kind="sql:table"),
    )

    changes = diff(old_document, new_document)
    assert changes.relationships_added == ()
    assert changes.relationships_removed == ()
