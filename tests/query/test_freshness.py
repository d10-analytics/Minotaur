"""Behavioral coverage for content-based graph freshness."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_file
from minotaur.query.freshness import drift


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _analyze(root: Path, output: Path, *targets: Path) -> int:
    return cli.main(
        [
            "analyze",
            "--root",
            str(root),
            "--output",
            str(output),
            *(str(target) for target in targets),
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


def test_query_refresh_removes_deleted_direct_selection_and_preserves_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    source = _write(root, "deleted.py", "def deleted():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, source) == 0
    source.unlink()

    document, diagnostics, observed = cli._load_and_refresh_graph(output, root, False)

    assert observed.missing == ("deleted.py",)
    assert diagnostics == ()
    assert not any(node.path == "deleted.py" for node in document.nodes)
    assert json.loads(output.read_text(encoding="utf-8"))["extensions"] == {
        "minotaur": {"selection": ["deleted.py"]}
    }


def test_query_no_refresh_keeps_graph_and_warns_with_stale_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    assert _analyze(root, output, root) == 0
    before = output.read_bytes()
    source.write_text("def app():\n    return 2\n", encoding="utf-8")

    document, diagnostics, observed = cli._load_and_refresh_graph(output, root, True)
    captured = capsys.readouterr()

    assert observed.changed == ("app.py",)
    assert diagnostics == ()
    assert document == load_graph_file(output).document
    assert output.read_bytes() == before
    assert "stale: app.py" in captured.err


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
    assert output.stat().st_mtime_ns != before_refresh
    assert old_hash != next(
        node["extensions"]["minotaur-python"]["content_sha256"]
        for node in refreshed_graph["nodes"]
        if node["node_class"] == "file"
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
