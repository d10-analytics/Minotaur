"""Behavioral coverage for unreferenced-symbol queries."""

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


def test_unreferenced_excludes_called_callback_test_dunder_and_container_calls(
    tmp_path: Path, capsys: object
) -> None:
    _write(
        tmp_path,
        "fixture.py",
        "def called():\n"
        "    pass\n\n"
        "def callback_only():\n"
        "    pass\n\n"
        "def test_fixture():\n"
        "    pass\n\n"
        "def __repr__():\n"
        "    pass\n\n"
        "def orphan():\n"
        "    pass\n\n"
        "def register(callback):\n"
        "    pass\n\n"
        "def use():\n"
        "    called()\n"
        "    register(callback_only)\n\n"
        "container_orphan = orphan\n",
    )
    _write(
        tmp_path,
        "caller.py",
        "from fixture import called, use\ncalled()\nuse()\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert status == 0
    assert captured.out == "fixture.py:13  fixture.orphan  function\n"
    assert "callback_only" not in captured.out
    assert "called" not in captured.out
    assert "test_fixture" not in captured.out
    assert "__repr__" not in captured.out


def test_unreferenced_text_fallback_tags_string_mentions_and_supports_paths_and_excludes(
    tmp_path: Path, capsys: object
) -> None:
    _write(
        tmp_path,
        "one.py",
        "def orphan():\n    pass\n\nmessage = 'orphan'\n\ndef excluded():\n    pass\n",
    )
    _write(tmp_path, "two.py", "def other_orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "one.py",
            "--exclude",
            "excluded",
            "--text-fallback",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert status == 0
    assert captured.out == "one.py:1  one.orphan  function [text-mention]\n"
    assert "other_orphan" not in captured.out
    assert "excluded" not in captured.out

    exclusions = tmp_path / "exclude.json"
    exclusions.write_text(json.dumps({"fixture": ["other_orphan"]}), encoding="utf-8")
    status = cli.main(
        [
            "query",
            "unreferenced",
            "--exclude-file",
            str(exclusions),
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert [result["symbol"] for result in payload["results"]] == [
        "one.orphan",
        "one.excluded",
    ]
