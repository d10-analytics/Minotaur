"""Persisted SQL facts remain visible only to their existing query vocabularies."""

from __future__ import annotations

from pathlib import Path

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import load_graph_blob
from minotaur.graph_model.serialization import serialize
from minotaur.language_interpreter.sql import analyze_sql_files
from minotaur.language_interpreter.workspace import Workspace
from minotaur.query import system as system_query
from minotaur.query.diff import diff
from minotaur.query.impact import ImpactRecord, impact
from minotaur.query.index import GraphIndex
from minotaur.query.symbols import callers, definitions
from minotaur.query.unreferenced import unreferenced
from minotaur.system import load_systems_data

_CATALOG = """\
CREATE SCHEMA S
GO
CREATE TABLE S.Parent (id int)
GO
CREATE TABLE S.Child (parent_id int REFERENCES S.Parent(id))
GO
CREATE VIEW S.Reader AS SELECT * FROM S.Parent
GO
CREATE VIEW WrongKind AS SELECT 1
GO
CREATE TABLE ChildRef (id int REFERENCES WrongKind(id))
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
CREATE TABLE S.Child (parent_id int REFERENCES S.Parent(id))
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


def test_sql_relationships_are_fenced_from_generic_consumers_but_core_recall_remains(
    tmp_path: Path,
) -> None:
    index = _persisted_index(tmp_path / "current")

    assert callers(index, "S.Parent") == ()
    assert impact(index, "S.Parent", max_depth=1) == (
        ImpactRecord(depth=0, symbol="S.Parent", kind="sql:table"),
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

    unresolved = callers(index, "WrongKind")
    assert len(unresolved) == 1
    assert unresolved[0].caller == "ChildRef"
    assert unresolved[0].reference == "WrongKind"
    assert unresolved[0].unresolved is True
