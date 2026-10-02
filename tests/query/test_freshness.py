"""Behavioral coverage for content-based graph freshness."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_file, stamp_path
from minotaur.query.freshness import content_sha256, drift


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _analyze(root: Path, output: Path, *targets: Path, force: bool = False) -> int:
    return cli.main(
        [
            "analyze",
            "--root",
            str(root),
            "--output",
            str(output),
            *(str(target) for target in targets),
            *(["--force"] if force else []),
        ]
    )


def _query_definitions(root: Path, output: Path, *, no_refresh: bool = False) -> int:
    arguments = [
        "query",
        "definitions",
        "foo",
        "--graph",
        str(output),
        "--root",
        str(root),
    ]
    if no_refresh:
        arguments.append("--no-refresh")
    return cli.main(arguments)


def _query_definitions_json(root: Path, output: Path, *, no_refresh: bool = False) -> int:
    arguments = [
        "query",
        "definitions",
        "foo",
        "--graph",
        str(output),
        "--root",
        str(root),
        "--json",
    ]
    if no_refresh:
        arguments.append("--no-refresh")
    return cli.main(arguments)


def test_drift_uses_bytes_not_mtime(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root) == 0
    original = load_graph_file(output).document
    original_mtime = source.stat().st_mtime_ns
    os.utime(source, ns=(original_mtime, original_mtime + 1_000_000))

    assert drift(original, root).is_clean


def test_drift_reports_changed_missing_and_directory_additions(tmp_path: Path) -> None:
    root = tmp_path / "source"
    changed = _write(root, "pkg/changed.py", "value = 1\n")
    missing = _write(root, "pkg/missing.py", "value = 2\n")
    _write(root, "outside.py", "value = 3\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root / "pkg") == 0
    graph = load_graph_file(output).document
    changed.write_text("value = 4\n", encoding="utf-8")
    missing.unlink()
    added = _write(root, "pkg/added.py", "value = 5\n")
    _write(root, "other/ignored.py", "value = 6\n")

    observed = drift(graph, root)

    assert observed.changed == ("pkg/changed.py",)
    assert observed.missing == ("pkg/missing.py",)
    assert observed.added == ("pkg/added.py",)
    assert added.exists()


def test_content_sha256_resolves_python_javascript_and_unknown_extensions(
    tmp_path: Path,
) -> None:
    python_root = tmp_path / "python"
    python_source = _write(python_root, "app.py", "value = 1\n")
    python_graph = tmp_path / "python.json"
    assert _analyze(python_root, python_graph, python_root) == 0
    python_node = next(
        node for node in load_graph_file(python_graph).document.nodes if node.path == "app.py"
    )

    javascript_root = tmp_path / "javascript"
    javascript_source = _write(javascript_root, "app.js", "const value = 1;\n")
    javascript_graph = tmp_path / "javascript.json"
    assert _analyze(javascript_root, javascript_graph, javascript_root) == 0
    javascript_node = next(
        node for node in load_graph_file(javascript_graph).document.nodes if node.path == "app.js"
    )

    assert content_sha256(python_node) == hashlib.sha256(python_source.read_bytes()).hexdigest()
    assert (
        content_sha256(javascript_node)
        == hashlib.sha256(javascript_source.read_bytes()).hexdigest()
    )
    assert content_sha256(replace(javascript_node, path="app.txt")) is None


def test_javascript_drift_reports_all_file_freshness_dimensions(tmp_path: Path) -> None:
    root = tmp_path / "source"
    changed = _write(root, "pkg/changed.js", "const value = 1;\n")
    missing = _write(root, "pkg/missing.js", "const value = 2;\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root / "pkg") == 0
    graph = load_graph_file(output).document
    assert drift(graph, root).is_clean

    changed.write_text("const value = 4;\n", encoding="utf-8")
    missing.unlink()
    _write(root, "pkg/added.js", "const value = 5;\n")

    observed = drift(graph, root)

    assert observed.changed == ("pkg/changed.js",)
    assert observed.missing == ("pkg/missing.js",)
    assert observed.added == ("pkg/added.js",)


@pytest.mark.parametrize("rename", [False, True])
def test_query_refuses_missing_direct_selection_and_force_recovers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rename: bool
) -> None:
    root = tmp_path / "source"
    source = _write(root, "deleted.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, source) == 0
    before = output.read_bytes()
    sidecar = stamp_path(output).read_bytes()
    if rename:
        source.rename(root / "renamed.py")
    else:
        source.unlink()
    capsys.readouterr()

    assert _query_definitions(root, output) == 2
    refused = capsys.readouterr()
    assert refused.out == ""
    assert "1 recorded files" in refused.err
    assert "analyze --force" in refused.err
    assert "refreshing graph" not in refused.err
    assert output.read_bytes() == before
    assert stamp_path(output).read_bytes() == sidecar

    assert _query_definitions(root, output, no_refresh=True) == 0
    stale = capsys.readouterr()
    assert "deleted.py:1  deleted.foo  function" in stale.out
    assert "stale: deleted.py" in stale.err
    assert output.read_bytes() == before
    assert stamp_path(output).read_bytes() == sidecar

    if not rename:
        _write(root, "replacement.py", "def foo():\n    return 2\n")
    assert _analyze(root, output, root, force=True) == 0
    capsys.readouterr()
    assert _query_definitions_json(root, output) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["refreshed"] is False
    assert recovered["stale"] == []
    assert [result["path"] for result in recovered["results"]] == [
        "renamed.py" if rename else "replacement.py"
    ]


def test_query_no_refresh_keeps_graph_and_warns_with_stale_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root) == 0
    before = output.read_bytes()
    source.write_text("def app():\n    return 2\n", encoding="utf-8")

    graph = cli._load_and_refresh_graph(output, root, True)
    captured = capsys.readouterr()

    assert graph.drift.changed == ("app.py",)
    assert graph.refreshed is False
    assert graph.diagnostics == ()
    assert graph.document == load_graph_file(output).document
    assert output.read_bytes() == before
    assert "stale: app.py" in captured.err
    assert "refreshed graph" not in captured.err


def test_public_query_refreshes_changed_definition_and_no_refresh_keeps_old_answer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root) == 0
    old_graph = json.loads(output.read_text(encoding="utf-8"))
    old_hash = next(
        node["extensions"]["minotaur-python"]["content_sha256"]
        for node in old_graph["nodes"]
        if node["node_class"] == "file"
    )
    source.write_text("# inserted before definition\n" + source.read_text(encoding="utf-8"))
    before_refresh = output.stat().st_mtime_ns

    assert _query_definitions(root, output) == 0
    refreshed = capsys.readouterr()
    refreshed_graph = json.loads(output.read_text(encoding="utf-8"))

    assert "app.py:2  app.foo  function" in refreshed.out
    assert refreshed.err == (
        "minotaur: refreshing graph (1 drifted paths)\nminotaur: stale: app.py\n"
    )
    assert output.stat().st_mtime_ns != before_refresh
    assert old_hash != next(
        node["extensions"]["minotaur-python"]["content_sha256"]
        for node in refreshed_graph["nodes"]
        if node["node_class"] == "file"
    )
    assert (
        stamp_path(output).read_text(encoding="ascii").strip()
        == hashlib.sha256(output.read_bytes()).hexdigest()
    )

    source.write_text("# inserted again\n" + source.read_text(encoding="utf-8"))
    before_no_refresh = output.stat().st_mtime_ns
    assert _query_definitions(root, output, no_refresh=True) == 0
    stale = capsys.readouterr()

    assert "app.py:2  app.foo  function" in stale.out
    assert "app.py:3  app.foo  function" not in stale.out
    assert "stale: app.py" in stale.err
    assert output.stat().st_mtime_ns == before_no_refresh


def test_public_query_refresh_removes_deleted_and_adds_directory_selection_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    package = root / "pkg"
    deleted = _write(root, "pkg/deleted.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, package) == 0
    deleted.unlink()
    before = output.read_bytes()
    sidecar = stamp_path(output).read_bytes()
    capsys.readouterr()
    assert _query_definitions(root, output) == 2
    refused = capsys.readouterr()
    assert "analyze --force" in refused.err
    assert "refreshing graph" not in refused.err
    assert output.read_bytes() == before
    assert stamp_path(output).read_bytes() == sidecar
    assert _analyze(root, output, package, force=True) == 0
    capsys.readouterr()
    assert _query_definitions(root, output) == 0
    removed = capsys.readouterr()
    removed_graph = json.loads(output.read_text(encoding="utf-8"))

    assert removed.out == "no definitions\n"
    assert not any(node.get("path") == "pkg/deleted.py" for node in removed_graph["nodes"])

    _write(root, "pkg/added.py", "def foo():\n    return 2\n")
    assert _query_definitions(root, output) == 0
    added = capsys.readouterr()
    added_graph = json.loads(output.read_text(encoding="utf-8"))

    assert "pkg/added.py:1  pkg.added.foo  function" in added.out
    assert any(node.get("path") == "pkg/added.py" for node in added_graph["nodes"])


def test_recorded_hash_is_sha256_of_exact_source_bytes(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "print('exact')\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root) == 0
    graph = json.loads(output.read_text(encoding="utf-8"))
    file_node = next(node for node in graph["nodes"] if node["node_class"] == "file")

    assert (
        file_node["extensions"]["minotaur-python"]["content_sha256"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )


def test_public_query_refresh_announces_rewrite_and_stale_paths_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "def foo():\n    return 1\n")
    _write(root, "other.py", "def bar():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root) == 0
    source.write_text("# edited\ndef foo():\n    return 2\n", encoding="utf-8")
    (root / "other.py").unlink()

    assert _query_definitions(root, output) == 0
    captured = capsys.readouterr()

    # A successful refresh rewrites the file the agent analyzed, so its attempt
    # is announced with the same per-path lines the --no-refresh path prints.
    assert captured.err.splitlines() == [
        "minotaur: refreshing graph (2 drifted paths)",
        "minotaur: stale: app.py",
        "minotaur: stale: other.py",
    ]


def test_query_json_reports_refreshed_state_and_drifted_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root) == 0
    assert _query_definitions_json(root, output) == 0
    assert json.loads(capsys.readouterr().out) == {
        "query": "definitions",
        "refreshed": False,
        "results": [
            {
                "duplicate": False,
                "kind": "function",
                "line": 1,
                "path": "app.py",
                "symbol": "app.foo",
            }
        ],
        "stale": [],
        "stale_analyzer": None,
    }

    source.write_text("# edited\ndef foo():\n    return 2\n", encoding="utf-8")
    assert _query_definitions_json(root, output) == 0
    # The refreshed answer still reports the paths that had drifted: an agent
    # reading JSON has no stderr and needs to know the graph was rewritten.
    assert json.loads(capsys.readouterr().out) == {
        "query": "definitions",
        "refreshed": True,
        "results": [
            {
                "duplicate": False,
                "kind": "function",
                "line": 2,
                "path": "app.py",
                "symbol": "app.foo",
            }
        ],
        "stale": ["app.py"],
        "stale_analyzer": None,
    }

    source.write_text("# edited again\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    assert _query_definitions_json(root, output, no_refresh=True) == 0
    assert json.loads(capsys.readouterr().out) == {
        "query": "definitions",
        "refreshed": False,
        "results": [
            {
                "duplicate": False,
                "kind": "function",
                "line": 2,
                "path": "app.py",
                "symbol": "app.foo",
            }
        ],
        "stale": ["app.py"],
        "stale_analyzer": None,
    }


def test_public_query_partial_deletion_refreshes_then_full_deletion_requires_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    first = _write(root, "first.py", "def foo():\n    return 1\n")
    second = _write(root, "second.py", "def foo():\n    return 2\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0
    first.unlink()
    capsys.readouterr()

    assert _query_definitions_json(root, output) == 0
    partial = capsys.readouterr()
    answer = json.loads(partial.out)
    assert answer["refreshed"] is True
    assert answer["stale"] == ["first.py"]
    assert [result["path"] for result in answer["results"]] == ["second.py"]
    assert "refreshing graph" in partial.err

    before = output.read_bytes()
    sidecar = stamp_path(output).read_bytes()
    second.unlink()
    assert _query_definitions_json(root, output) == 2
    refused = capsys.readouterr()
    assert refused.out == ""
    assert str(root) in refused.err
    assert "1 recorded files" in refused.err
    assert "analyze --force" in refused.err
    assert "refreshing graph" not in refused.err
    assert output.read_bytes() == before
    assert stamp_path(output).read_bytes() == sidecar

    assert _analyze(root, output, root, force=True) == 0
    capsys.readouterr()
    assert _query_definitions_json(root, output) == 0
    assert json.loads(capsys.readouterr().out) == {
        "query": "definitions",
        "refreshed": False,
        "results": [],
        "stale": [],
        "stale_analyzer": None,
    }


@pytest.mark.parametrize("target", ["pkg", "."])
def test_public_query_wrong_root_preserves_graph_even_with_new_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    root = tmp_path / "src"
    _write(root, "pkg/app.py", "def foo():\n    return 1\n")
    _write(root, "pkg/other.py", "value = 2\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root / target) == 0
    before = output.read_bytes()
    sidecar = stamp_path(output).read_bytes()
    document = load_graph_file(output).document
    observed = drift(document, tmp_path)
    assert observed.missing == ("pkg/app.py", "pkg/other.py")
    if target == ".":
        assert observed.added == ("src/pkg/app.py", "src/pkg/other.py")
    else:
        assert observed.added == ()
    capsys.readouterr()

    assert _query_definitions_json(tmp_path, output) == 2
    refused = capsys.readouterr()
    assert refused.out == ""
    assert str(tmp_path) in refused.err
    assert "2 recorded files" in refused.err
    for recovery in ("--root", "--no-refresh", "analyze --force"):
        assert recovery in refused.err
    assert "refreshing graph" not in refused.err
    assert output.read_bytes() == before
    assert stamp_path(output).read_bytes() == sidecar

    assert _query_definitions_json(tmp_path, output, no_refresh=True) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["refreshed"] is False
    assert saved["stale"] == ["pkg/app.py", "pkg/other.py", *observed.added]
    assert [result["symbol"] for result in saved["results"]] == ["pkg.app.foo"]
    assert output.read_bytes() == before
    assert stamp_path(output).read_bytes() == sidecar
