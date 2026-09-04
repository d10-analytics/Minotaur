"""Behavioral coverage for semantic graph snapshot diffs."""

from __future__ import annotations

import json
from pathlib import Path

from minotaur import cli
from minotaur.graph_model.location import Location, Position, Range
from minotaur.query.diff import DiffResult, Relocation, render_text


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
