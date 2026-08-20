"""Behavioral coverage for callers and definitions queries."""

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


def test_callers_prints_each_call_site_and_matching_unresolved_reference(
    tmp_path: Path, capsys: object
) -> None:
    _write(tmp_path, "pkg/mod.py", "def target():\n    pass\n")
    _write(
        tmp_path,
        "use.py",
        "from pkg.mod import target\n"
        "def caller():\n"
        "    unknown.target()\n"
        "    target()\n"
        "    target()\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    document = json.loads(graph.read_text(encoding="utf-8"))
    target_id = next(node["id"] for node in document["nodes"] if node["label"] == "pkg.mod.target")
    call_edge = next(
        relationship
        for relationship in document["relationships"]
        if relationship["kind"] == "calls" and relationship["target"] == target_id
    )
    call_site = call_edge["evidence"][0]["locations"][0]
    call_edge["evidence"].append(
        {
            "provenance": "imported-graph",
            "producer": {"name": "test-fixture"},
            "locations": [call_site],
        }
    )
    graph.write_text(json.dumps(document), encoding="utf-8")

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
    assert captured.out.splitlines() == [
        "use.py:4:5  use.caller",
        "use.py:5:5  use.caller",
        "use.py:3:5  unknown.target [unresolved]",
    ]


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


@pytest.mark.parametrize(
    ("query_name", "query_args"),
    [
        ("callers", ("callers", "mod.target")),
        ("definitions", ("definitions", "target")),
        ("impact", ("impact", "mod.target")),
        ("unreferenced", ("unreferenced",)),
        ("diff", ("diff",)),
        ("context", ("context", "--site", "mod.py:1")),
    ],
)
def test_all_query_renderers_hide_graph_internals_in_text_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], query_name: str, query_args: tuple[str, ...]
) -> None:
    """Every public query keeps opaque graph facts out of its agent output."""
    _write(
        tmp_path,
        "mod.py",
        "def target():\n    pass\n\ndef caller():\n    target()\n\ndef orphan():\n    pass\n",
    )
    graph = tmp_path / "graph.json"
    old_graph = tmp_path / "old.json"
    assert _analyze(tmp_path, graph) == 0
    old_graph.write_bytes(graph.read_bytes())

    common = ("--graph", str(graph), "--root", str(tmp_path))
    if query_name == "diff":
        invocation = (*query_args, str(old_graph), str(graph))
        json_invocation = (*invocation, "--json")
    else:
        invocation = (*query_args, *common)
        json_invocation = (*invocation, "--json")

    assert cli.main(["query", *invocation]) == 0
    text_output = capsys.readouterr().out
    for internal in ("node:sha256:", "sha256", "digest", "provenance", "evidence"):
        assert internal not in text_output

    assert cli.main(["query", *json_invocation]) == 0
    json_output = capsys.readouterr().out
    for internal in ("node:sha256:", "sha256", "digest", "provenance", "evidence"):
        assert internal not in json_output
    payload = json.loads(json_output)
    if query_name == "diff":
        assert set(payload) == {
            "query",
            "added",
            "removed",
            "relocated",
            "relationships_added",
            "relationships_removed",
        }
        assert payload["query"] == "diff"
        assert all(isinstance(payload[key], list) for key in payload if key != "query")
    else:
        assert set(payload) == {"query", "results"}
        assert payload["query"] == query_name
        assert isinstance(payload["results"], list)
