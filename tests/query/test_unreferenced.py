"""Behavioral coverage for unreferenced-symbol queries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_unreferenced_no_refresh_uses_deleted_graph_path_without_text_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "deleted.py"
    _write(
        tmp_path,
        "deleted.py",
        "def orphan():\n    pass\n\nmessage = 'orphan'\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    before = graph.read_bytes()
    source.unlink()

    status = cli.main(
        [
            "query",
            "unreferenced",
            "deleted.py",
            "--text-fallback",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "deleted.py:1  deleted.orphan  function\n"
    assert "text-mention" not in captured.out
    assert "minotaur: stale: deleted.py" in captured.err
    assert graph.read_bytes() == before


def test_unreferenced_no_refresh_uses_graph_path_when_source_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "unreadable.py"
    _write(tmp_path, "unreadable.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    source.unlink()
    source.mkdir()

    status = cli.main(
        [
            "query",
            "unreferenced",
            "unreadable.py",
            "--text-fallback",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "unreadable.py:1  unreadable.orphan  function\n"
    assert "minotaur: stale: unreadable.py" in captured.err


def test_unreferenced_no_refresh_root_path_filters_saved_graph_without_filesystem_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "present.py"
    _write(tmp_path, "present.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    source.unlink()

    status = cli.main(
        [
            "query",
            "unreferenced",
            ".",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "present.py:1  present.orphan  function\n"
    assert "minotaur: stale: present.py" in captured.err


def test_unreferenced_clean_graph_still_validates_missing_query_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "present.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "missing.py",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "query path does not exist: missing.py" in captured.err


def test_unreferenced_stale_graph_still_rejects_query_path_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "present.py"
    _write(tmp_path, "present.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    source.unlink()

    status = cli.main(
        [
            "query",
            "unreferenced",
            "../outside.py",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "query path escapes root: ../outside.py" in captured.err
