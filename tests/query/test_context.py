"""Behavioral coverage for bounded source context queries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur import cli


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _analyze(root: Path, output: Path) -> int:
    return cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])


def test_context_marks_target_and_changed_file_without_refresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "\n".join(f"value_{i} = {i}" for i in range(1, 20)) + "\n")
    graph = tmp_path / "graph.json"

    assert _analyze(root, graph) == 0
    assert cli.main(
        ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:12"]
    ) == 0
    output = capsys.readouterr().out
    assert "app.py:9-15" in output
    assert "> 12: value_12 = 12" in output
    assert all(f"{line}:" in output for line in range(9, 16))

    source.write_text(source.read_text(encoding="utf-8") + "changed = True\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_load_and_refresh_graph", lambda *args: pytest.fail("refresh called"))
    assert cli.main(
        ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:12"]
    ) == 0
    stale = capsys.readouterr().out
    assert stale.startswith("[file changed since analysis]\n")
    assert "> 12: value_12 = 12" in stale


def test_context_json_contains_same_bounded_lines_and_stale_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "\n".join(f"value_{i} = {i}" for i in range(1, 8)) + "\n")
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0
    source.write_text(source.read_text(encoding="utf-8").replace("value_4", "changed"), encoding="utf-8")

    assert cli.main(
        [
            "query",
            "context",
            "--graph",
            str(graph),
            "--root",
            str(root),
            "--site",
            "app.py:4",
            "--before",
            "1",
            "--after",
            "1",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "context"
    result = payload["results"]
    assert len(result) == 1
    assert result[0]["path"] == "app.py"
    assert result[0]["stale"] is True
    assert [line["line"] for line in result[0]["lines"]] == [3, 4, 5]
    assert result[0]["lines"][1] == {"line": 4, "target": True, "text": "changed = 4"}


def test_context_rejects_unknown_or_out_of_range_sites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0

    assert cli.main(
        ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "other.py:1"]
    ) == 2
    assert "not present in graph" in capsys.readouterr().err
    assert cli.main(
        ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:2"]
    ) == 2
    assert "outside source file" in capsys.readouterr().err
