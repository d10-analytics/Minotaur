"""Behavioral coverage for inbound impact queries."""

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


def test_impact_includes_module_scope_caller_as_module_symbol(
    tmp_path: Path, capsys: object
) -> None:
    # A call made directly in a module body (not inside a function) has the
    # module itself as its inbound dependant. impact.py deliberately keeps
    # module-kind nodes as real results, unlike diff.py, which drops them.
    _write(
        tmp_path,
        "boot.py",
        "def start():\n    pass\n\nstart()\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "impact",
            "boot.start",
            "--depth",
            "1",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    result = capsys.readouterr()
    assert status == 0
    assert result.out.splitlines() == [
        "depth 0: boot.start",
        "depth 1: boot",
    ]

    status = cli.main(
        [
            "query",
            "impact",
            "boot.start",
            "--depth",
            "1",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert {"boundary": False, "depth": 1, "kind": "module", "symbol": "boot"} in payload["results"]


def test_impact_rejects_a_negative_depth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A negative depth is a usage error, not an empty result.

    ``impact`` always lists the queried symbol itself at depth 0, so an empty
    answer would be indistinguishable from a query that never ran; rejecting
    the depth keeps that distinction visible.
    """
    _write(tmp_path, "lonely.py", "def unused():\n    return 1\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "impact",
            "lonely.unused",
            "--depth",
            "-1",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "depth must be non-negative, got -1" in captured.err


def _edge_kinds(document: dict[str, object], source_label: str, target_label: str) -> list[str]:
    nodes = document["nodes"]
    relationships = document["relationships"]
    assert isinstance(nodes, list)
    assert isinstance(relationships, list)
    ids = {node["label"]: node["id"] for node in nodes if isinstance(node, dict)}
    return [
        relationship["kind"]
        for relationship in relationships
        if isinstance(relationship, dict)
        and relationship["source"] == ids[source_label]
        and relationship["target"] == ids[target_label]
    ]


def test_dotted_import_call_graph_drives_inbound_impact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/sub.py", "def go():\n    return 1\n")
    _write(
        tmp_path,
        "caller.py",
        "import pkg.sub\n\ndef owner():\n    pkg.sub.go()\n",
    )
    graph = tmp_path / "call-only.json"
    assert _analyze(tmp_path, graph) == 0

    document = json.loads(graph.read_text(encoding="utf-8"))
    assert _edge_kinds(document, "caller.owner", "pkg.sub.go") == ["calls"]

    status = cli.main(
        [
            "query",
            "impact",
            "pkg.sub.go",
            "--depth",
            "1",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out.splitlines() == [
        "depth 0: pkg.sub.go",
        "depth 1: caller.owner",
    ]


def test_dotted_import_load_graph_does_not_create_impact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/sub.py", "def go():\n    return 1\n")
    _write(
        tmp_path,
        "loader.py",
        "import pkg.sub\n\ndef owner():\n    loaded = pkg.sub.go\n    return loaded\n",
    )
    graph = tmp_path / "load-only.json"
    assert _analyze(tmp_path, graph) == 0

    document = json.loads(graph.read_text(encoding="utf-8"))
    assert _edge_kinds(document, "loader.owner", "pkg.sub.go") == ["references"]

    callers_status = cli.main(
        [
            "query",
            "callers",
            "pkg.sub.go",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    callers = capsys.readouterr()
    impact_status = cli.main(
        [
            "query",
            "impact",
            "pkg.sub.go",
            "--depth",
            "1",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    impact_output = capsys.readouterr()

    assert callers_status == 0
    assert callers.out == "no callers\n"
    assert impact_status == 0
    assert impact_output.out == "depth 0: pkg.sub.go\n"
    assert "loader.owner" not in callers.out
    assert "loader.owner" not in impact_output.out
