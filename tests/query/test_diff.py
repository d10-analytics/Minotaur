"""Behavioral coverage for semantic graph snapshot diffs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from minotaur import cli
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.query.diff import DiffResult, Relocation, diff, render_text


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _analyze(root: Path, output: Path) -> int:
    return cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])


def test_diff_matches_symbols_by_kind_and_label_and_reports_call_edge(
    tmp_path: Path, capsys: object
) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    _write(old_root, "mod.py", "def f():\n    pass\n\ndef h():\n    pass\n")
    _write(
        new_root,
        "mod.py",
        "\ndef f():\n    g()\n\ndef g():\n    pass\n",
    )
    old_graph = tmp_path / "old.json"
    new_graph = tmp_path / "new.json"
    assert _analyze(old_root, old_graph) == 0
    assert _analyze(new_root, new_graph) == 0

    status = cli.main(["query", "diff", str(old_graph), str(new_graph)])
    output = capsys.readouterr().out  # type: ignore[attr-defined]

    assert status == 1
    assert output == "+ mod.g\n- mod.h\n~ mod.f (relocated mod.py:1→2)\n+ calls mod.f → mod.g\n"
    assert "node:sha256:" not in output


def _location(path: str, line: int) -> Location:
    return Location(path=path, range=Range(Position(line, 0), Position(line, 1)))


def test_relocation_text_shows_cross_file_move_with_one_based_lines() -> None:
    relocation = Relocation(
        kind="function",
        symbol="pkg.helper",
        old_location=_location("pkg/old.py", 4),
        new_location=_location("pkg/new.py", 9),
    )

    text = render_text(DiffResult(relocated=(relocation,)))

    assert text == "~ pkg.helper (relocated pkg/old.py:5→pkg/new.py:10)\n"


def test_relocation_text_falls_back_when_a_location_is_missing() -> None:
    relocation = Relocation(
        kind="function",
        symbol="pkg.helper",
        old_location=_location("pkg/mod.py", 0),
        new_location=None,
    )

    text = render_text(DiffResult(relocated=(relocation,)))

    assert text == "~ pkg.helper (relocated)\n"


def test_diff_keeps_unresolved_relationship_when_origin_id_relocates(
    tmp_path: Path, capsys: object
) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    _write(old_root, "mod.py", "def caller():\n    unknown.target()\n")
    _write(new_root, "mod.py", "\ndef caller():\n    unknown.target()\n")
    old_graph = tmp_path / "old.json"
    new_graph = tmp_path / "new.json"
    assert _analyze(old_root, old_graph) == 0
    assert _analyze(new_root, new_graph) == 0

    assert cli.main(["query", "diff", str(old_graph), str(new_graph), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["relationships_added"] == []
    assert payload["relationships_removed"] == []
    assert any(item["symbol"] == "mod.caller" for item in payload["relocated"])


class _CountingNodes(tuple[Node, ...]):
    yielded = 0

    def __iter__(self) -> Iterator[Node]:
        for node in super().__iter__():
            self.yielded += 1
            yield node


def test_diff_unresolved_origin_lookup_has_linear_node_iteration() -> None:
    from test_system_diff import _call, _symbol, _unresolved

    origins = tuple(_symbol(f"origin_{index}", "mod.py", index) for index in range(100))
    unresolved = tuple(
        _unresolved(origin, "mod.py", 100 + index) for index, origin in enumerate(origins)
    )
    nodes = _CountingNodes((*unresolved, *origins))
    document = GraphDocument(
        CoordinateEncoding.UTF_8,
        nodes,
        tuple(_call(origin, target) for origin, target in zip(origins, unresolved, strict=True)),
    )
    # Exclude the model's construction-time type validation from query work.
    nodes.yielded = 0

    result = diff(document, document)

    assert result == DiffResult()
    assert nodes.yielded <= 8 * len(nodes)


def test_diff_unresolved_origin_uses_first_duplicate_id_label() -> None:
    from test_system_diff import _call, _symbol, _unresolved

    first = _symbol("a", "mod.py", 0)
    duplicate = replace(first, label="b")
    parent = _symbol("parent", "mod.py", 1)
    unresolved = _unresolved(first, "mod.py", 2)
    relationships = (_call(parent, unresolved),)
    old = GraphDocument(
        CoordinateEncoding.UTF_8, (first, duplicate, parent, unresolved), relationships
    )
    new = GraphDocument(CoordinateEncoding.UTF_8, (first, parent, unresolved), relationships)

    result = diff(old, new)

    assert result.relationships_added == ()
    assert result.relationships_removed == ()


def test_diff_missing_unresolved_origins_keep_distinct_raw_ids() -> None:
    from test_system_diff import _call, _symbol, _unresolved

    parent = _symbol("parent", "mod.py", 0)
    old_origin = _symbol("origin", "mod.py", 1)
    new_origin = _symbol("origin", "mod.py", 2)
    old_target = _unresolved(old_origin, "mod.py", 3)
    new_target = _unresolved(new_origin, "mod.py", 3)
    old = GraphDocument(
        CoordinateEncoding.UTF_8, (parent, old_target), (_call(parent, old_target),)
    )
    new = GraphDocument(
        CoordinateEncoding.UTF_8, (parent, new_target), (_call(parent, new_target),)
    )

    result = diff(old, new)

    assert len(result.relationships_added) == 1
    assert len(result.relationships_removed) == 1
    assert result.relationships_added == result.relationships_removed


def _run_diff(
    tmp_path: Path, capsys: object, old: dict[str, str], new: dict[str, str], *, json_out: bool
) -> tuple[int, str]:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    for root, files in ((old_root, old), (new_root, new)):
        for name, content in files.items():
            _write(root, name, content)
    old_graph = tmp_path / "old.json"
    new_graph = tmp_path / "new.json"
    assert _analyze(old_root, old_graph) == 0
    assert _analyze(new_root, new_graph) == 0
    capsys.readouterr()  # type: ignore[attr-defined]
    args = ["query", "diff", str(old_graph), str(new_graph)]
    status = cli.main([*args, "--json"] if json_out else args)
    return status, capsys.readouterr().out  # type: ignore[attr-defined]


_SETTER = (
    "class C:\n"
    "    @property\n    def x(self):\n        return 1\n\n"
    "    @x.setter\n    def x(self, v):\n        pass\n"
)


def test_removing_one_of_two_same_name_functions_is_a_removal(
    tmp_path: Path, capsys: object
) -> None:
    status, out = _run_diff(
        tmp_path,
        capsys,
        {"mod.py": "def f():\n    pass\n\ndef f():\n    pass\n"},
        {"mod.py": "def f():\n    pass\n"},
        json_out=False,
    )

    assert status == 1
    assert out == "- mod.f\n"


def test_replacing_setter_with_deleter_reports_roles_in_text_and_json(
    tmp_path: Path, capsys: object
) -> None:
    old = {"mod.py": _SETTER}
    new = {"mod.py": _SETTER.replace("x.setter", "x.deleter")}

    status, out = _run_diff(tmp_path, capsys, old, new, json_out=False)
    assert status == 1
    assert out == "+ mod.C.x (deleter)\n- mod.C.x (setter)\n"

    status, out = _run_diff(tmp_path / "j", capsys, old, new, json_out=True)
    payload = json.loads(out)
    assert status == 1
    assert payload["added"] == [{"kind": "method", "role": "deleter", "symbol": "mod.C.x"}]
    assert payload["removed"] == [{"kind": "method", "role": "setter", "symbol": "mod.C.x"}]
    assert payload["relocated"] == []


def test_duplicate_inserted_before_existing_definition_adds_without_edge_churn(
    tmp_path: Path, capsys: object
) -> None:
    old = {"mod.py": "def g():\n    pass\n\ndef f():\n    g()\n"}
    new = {"mod.py": "def g():\n    pass\n\ndef f():\n    pass\n\ndef f():\n    g()\n"}
    status, out = _run_diff(tmp_path, capsys, old, new, json_out=False)

    assert status == 1
    assert out == "+ mod.f\n"


def test_shifted_property_pair_relocates_each_role(tmp_path: Path, capsys: object) -> None:
    old = {"mod.py": _SETTER}
    new = {"mod.py": "# header\n" + _SETTER}

    status, out = _run_diff(tmp_path, capsys, old, new, json_out=False)
    assert status == 1
    assert out == (
        "~ mod.C (relocated mod.py:1→2)\n"
        "~ mod.C.x (property) (relocated mod.py:3→4)\n"
        "~ mod.C.x (setter) (relocated mod.py:7→8)\n"
    )

    _, out = _run_diff(tmp_path / "j", capsys, old, new, json_out=True)
    records = [r for r in json.loads(out)["relocated"] if r["symbol"] == "mod.C.x"]
    assert [(r["role"], r["from"]["line"], r["to"]["line"]) for r in records] == [
        ("property", 3, 4),
        ("setter", 7, 8),
    ]


def test_roleless_records_keep_their_exact_shape(tmp_path: Path, capsys: object) -> None:
    old = {"mod.py": "def f():\n    pass\n"}
    new = {"mod.py": "\ndef f():\n    pass\n\ndef h():\n    f()\n"}
    _, out = _run_diff(tmp_path, capsys, old, new, json_out=True)
    payload = json.loads(out)

    assert payload["added"] == [{"kind": "function", "symbol": "mod.h"}]
    assert payload["relocated"] == [
        {
            "from": {"path": "mod.py", "line": 1, "column": 1},
            "kind": "function",
            "symbol": "mod.f",
            "to": {"path": "mod.py", "line": 2, "column": 1},
        }
    ]
    assert payload["relationships_added"] == [
        {"kind": "calls", "source": "mod.h", "target": "mod.f"}
    ]


def test_unresolved_relationship_is_marked_in_text_and_json(tmp_path: Path, capsys: object) -> None:
    old = {"mod.py": "def caller():\n    pass\n"}
    new = {"mod.py": "def caller():\n    other.f()\n"}

    status, out = _run_diff(tmp_path, capsys, old, new, json_out=False)
    assert status == 1
    assert out.splitlines()[-1].endswith(" [unresolved]")
    assert out.count("[unresolved]") == 1
    line = out.splitlines()[-1]
    assert line.startswith("+ ") and "mod.caller → " in line

    _, out = _run_diff(tmp_path / "j", capsys, old, new, json_out=True)
    records = json.loads(out)["relationships_added"]
    assert len(records) == 1
    assert records[0]["unresolved"] is True
    assert set(records[0]) == {"kind", "source", "target", "unresolved"}


def test_relocation_start_projection_is_flat_one_based() -> None:
    relocation = Relocation(
        kind="function",
        symbol="mod.f",
        old_location=_location("mod.py", 0),
        new_location=_location("mod.py", 2),
    )

    assert relocation.to_dict() == {
        "from": {"path": "mod.py", "line": 1, "column": 1},
        "kind": "function",
        "symbol": "mod.f",
        "to": {"path": "mod.py", "line": 3, "column": 1},
    }
    missing = Relocation("function", "mod.f", _location("mod.py", 0), None)
    assert missing.to_dict()["to"] is None


def test_body_shrink_at_same_start_line_is_not_a_relocation(tmp_path: Path, capsys: object) -> None:
    status, out = _run_diff(
        tmp_path,
        capsys,
        {"mod.py": "def f():\n    a = 1\n    b = 2\n"},
        {"mod.py": "def f():\n    a = 1\n"},
        json_out=False,
    )

    assert (status, out) == (0, "no changes\n")


_TABLE = "CREATE TABLE dbo.T (id int)\n"


def test_sql_column_only_shift_is_not_a_relocation(tmp_path: Path, capsys: object) -> None:
    status, out = _run_diff(
        tmp_path, capsys, {"a.sql": _TABLE}, {"a.sql": "   " + _TABLE}, json_out=False
    )

    assert (status, out) == (0, "no changes\n")


def test_sql_cross_file_move_at_same_line_is_a_relocation(tmp_path: Path, capsys: object) -> None:
    status, out = _run_diff(tmp_path, capsys, {"a.sql": _TABLE}, {"b.sql": _TABLE}, json_out=False)
    assert status == 1
    assert out == "~ dbo.T (relocated a.sql:1→b.sql:1)\n"

    _, out = _run_diff(tmp_path / "j", capsys, {"a.sql": _TABLE}, {"b.sql": _TABLE}, json_out=True)
    record = json.loads(out)["relocated"][0]
    assert record["from"] == {"path": "a.sql", "line": 1, "column": 14}
    assert record["to"] == {"path": "b.sql", "line": 1, "column": 14}


def test_removed_relationship_prints_exact_edge_line(tmp_path: Path, capsys: object) -> None:
    status, out = _run_diff(
        tmp_path,
        capsys,
        {"mod.py": "def g():\n    pass\n\ndef f():\n    g()\n"},
        {"mod.py": "def g():\n    pass\n\ndef f():\n    pass\n"},
        json_out=False,
    )

    assert status == 1
    assert out == "- calls mod.f → mod.g\n"
