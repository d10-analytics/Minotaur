"""Behavioral coverage for inbound impact queries."""

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


def test_impact_groups_depths_and_marks_cutoff_boundary(tmp_path: Path, capsys: object) -> None:
    _write(
        tmp_path,
        "chain.py",
        "def c():\n"
        "    pass\n\n"
        "def d():\n"
        "    c()\n\n"
        "def e():\n"
        "    d()\n\n"
        "def callback_only():\n"
        "    c\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "impact",
            "chain.c",
            "--depth",
            "2",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    full = capsys.readouterr()
    assert status == 0
    assert full.out.splitlines() == [
        "depth 0: chain.c",
        "depth 1: chain.d",
        "depth 2: chain.e",
    ]
    assert "callback_only" not in full.out

    status = cli.main(
        [
            "query",
            "impact",
            "chain.c",
            "--depth",
            "1",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
            "--json",
        ]
    )
    cut = capsys.readouterr()
    assert status == 0
    payload = json.loads(cut.out)
    assert payload["results"] == [
        {"boundary": False, "depth": 0, "kind": "function", "symbol": "chain.c"},
        {"boundary": False, "depth": 1, "kind": "function", "symbol": "chain.d"},
        {"boundary": True, "depth": 2, "kind": "function", "symbol": "chain.e"},
    ]
