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
