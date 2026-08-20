"""Behavioral coverage for callers and definitions queries."""

from __future__ import annotations

import json
from pathlib import Path

from minotaur import cli


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _analyze(root: Path, output: Path) -> int:
    return cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])


def test_callers_prints_each_call_site_and_matching_unresolved_reference(
    tmp_path: Path, capsys: object
) -> None:
    _write(tmp_path, "pkg/mod.py", "def target():\n    pass\n")
    _write(
        tmp_path,
        "use.py",
        "from pkg.mod import target\n"
        "def caller():\n"
        "    target()\n"
        "    target()\n"
        "    unknown.target()\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "callers",
            "pkg.mod.target",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert status == 0
    assert "use.py:3:5  use.caller\n" in captured.out
    assert "use.py:4:5  use.caller\n" in captured.out
    assert "use.py:5:5  unknown.target [unresolved]\n" in captured.out


def test_definitions_marks_duplicate_bare_names(tmp_path: Path, capsys: object) -> None:
    _write(tmp_path, "one.py", "def parse():\n    pass\n")
    _write(tmp_path, "two.py", "def parse():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "definitions",
            "parse",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert status == 0
    assert "one.py:1  one.parse  function [duplicate-name]" in captured.out
    assert "two.py:1  two.parse  function [duplicate-name]" in captured.out


def test_unknown_callers_name_suggests_labels_and_lonely_is_success(
    tmp_path: Path, capsys: object
) -> None:
    _write(tmp_path, "mod.py", "def lonely():\n    pass\n\ndef target():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    unknown_status = cli.main(
        [
            "query",
            "callers",
            "mod.targte",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    unknown = capsys.readouterr()
    assert unknown_status == 2
    assert "nearest labels:" in unknown.err
    assert "mod.target" in unknown.err

    lonely_status = cli.main(
        [
            "query",
            "callers",
            "mod.lonely",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    lonely = capsys.readouterr()
    assert lonely_status == 0
    assert lonely.out == "no callers\n"


def test_symbol_queries_json_uses_same_records_without_graph_internals(
    tmp_path: Path, capsys: object
) -> None:
    _write(tmp_path, "mod.py", "def target():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    assert (
        cli.main(
            [
                "query",
                "definitions",
                "target",
                "--graph",
                str(graph),
                "--root",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["query"] == "definitions"
    assert payload["results"] == [
        {
            "duplicate": False,
            "kind": "function",
            "line": 1,
            "path": "mod.py",
            "symbol": "mod.target",
        }
    ]
    assert "node:sha256:" not in output
