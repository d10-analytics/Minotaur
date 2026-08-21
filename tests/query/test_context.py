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
    assert (
        cli.main(
            ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:12"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "app.py:9-15" in output
    assert "> 12: value_12 = 12" in output
    assert all(f"{line}:" in output for line in range(9, 16))

    source.write_text(source.read_text(encoding="utf-8") + "changed = True\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_load_and_refresh_graph", lambda *args: pytest.fail("refresh called"))
    assert (
        cli.main(
            ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:12"]
        )
        == 0
    )
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
    source.write_text(
        source.read_text(encoding="utf-8").replace("value_4", "changed"), encoding="utf-8"
    )

    assert (
        cli.main(
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
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "context"
    result = payload["results"]
    assert len(result) == 1
    assert result[0]["path"] == "app.py"
    assert result[0]["stale"] is True
    assert [line["line"] for line in result[0]["lines"]] == [3, 4, 5]
    assert result[0]["lines"][1] == {"line": 4, "target": True, "text": "changed = 4"}


def test_context_rejects_unknown_or_out_of_range_sites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0

    assert (
        cli.main(
            ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "other.py:1"]
        )
        == 2
    )
    assert "not present in graph" in capsys.readouterr().err
    assert (
        cli.main(
            ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:2"]
        )
        == 2
    )
    assert "outside source file" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("site", "message"),
    [
        ("app.py", "site must have the form path:line"),
        (":1", "site must have the form path:line"),
        ("app.py:0", "site line must be a positive integer"),
        ("app.py:-1", "site line must be a positive integer"),
        ("app.py:twelve", "site line must be a positive integer"),
        ("../app.py:1", "site path must be a repository-relative path"),
    ],
)
def test_context_rejects_each_malformed_site_individually(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], site: str, message: str
) -> None:
    """Every site rejection is asserted separately, message and exit code.

    A single combined case would let one branch mask another: a missing colon
    and a non-integer line reach different raises but the same exit status.
    """
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0

    status = cli.main(
        ["query", "context", "--graph", str(graph), "--root", str(root), "--site", site]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert message in captured.err


def test_context_rejects_negative_window_sizes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0

    status = cli.main(
        [
            "query",
            "context",
            "--graph",
            str(graph),
            "--root",
            str(root),
            "--site",
            "app.py:1",
            "--before",
            "-1",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "before and after must be non-negative" in captured.err


def test_context_labels_an_excerpt_whose_graph_records_no_content_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a recorded hash the excerpt is labeled, not silently trusted.

    Graphs produced before content hashes were recorded (or by a producer that
    omits them) still answer ``context``; the marker tells an agent the
    staleness question was not answered rather than answered "fresh".
    """
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0
    document = json.loads(graph.read_text(encoding="utf-8"))
    for node in document["nodes"]:
        node.get("extensions", {}).get("minotaur-python", {}).pop("content_sha256", None)
    graph.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

    status = cli.main(
        [
            "query",
            "context",
            "--graph",
            str(graph),
            "--root",
            str(root),
            "--site",
            "app.py:1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["results"][0]["hash_available"] is False
    assert payload["results"][0]["stale"] is False

    assert (
        cli.main(
            ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:1"]
        )
        == 0
    )
    assert capsys.readouterr().out.startswith("[file hash unavailable]\n")


def test_context_rejects_sites_that_left_the_source_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path recorded in the graph is still re-checked against the tree.

    The graph is not the authority on what may be read: between analysis and
    the query the file can be deleted, replaced by a symlink pointing outside
    the root, or replaced by a directory, and each case must be refused rather
    than served from wherever the path now leads.
    """
    root = tmp_path / "source"
    source = _write(root, "app.py", "value = 1\n")
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0
    site = ["query", "context", "--graph", str(graph), "--root", str(root), "--site", "app.py:1"]

    source.unlink()
    assert cli.main(site) == 2
    assert "site path is missing or escapes the source root: app.py" in capsys.readouterr().err

    (tmp_path / "outside.py").write_text("secret = 1\n", encoding="utf-8")
    source.symlink_to(tmp_path / "outside.py")
    assert cli.main(site) == 2
    captured = capsys.readouterr()
    assert "site path is missing or escapes the source root: app.py" in captured.err
    assert "secret" not in captured.out

    source.unlink()
    source.mkdir()
    assert cli.main(site) == 2
    assert "site path is not a file: app.py" in capsys.readouterr().err
