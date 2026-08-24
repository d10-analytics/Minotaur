"""Pin the intentionally bounded edges of the graph-freshness contract."""

from __future__ import annotations

import json
from pathlib import Path

from minotaur import cli
from minotaur.graph_model.loading import graph_digest, load_graph_file, stamp_path
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
            "--force",
            *(str(target) for target in targets),
        ]
    )


def _document(output: Path):
    return load_graph_file(output).document


def test_new_python_outside_recorded_target_is_not_detected(tmp_path: Path) -> None:
    """Freshness row: a new .py outside recorded targets is intentionally ignored."""
    root = tmp_path / "source"
    package = _write(root, "pkg/known.py", "value = 1\n").parent
    output = tmp_path / "graph.json"
    assert _analyze(root, output, package) == 0

    _write(root, "outside.py", "value = 2\n")

    observed = drift(_document(output), root)
    assert observed.is_clean
    assert observed.added == ()


def test_non_python_edit_is_not_detected(tmp_path: Path) -> None:
    """Freshness row: edits to unsupported non-.py files do not create drift."""
    root = tmp_path / "source"
    source = _write(root, "app.py", "value = 1\n")
    notes = _write(root, "notes.txt", "before\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0

    notes.write_text("after\n", encoding="utf-8")

    observed = drift(_document(output), root)
    assert source.exists()
    assert observed.is_clean


def test_excluded_and_hidden_directory_edits_are_not_detected(tmp_path: Path) -> None:
    """Freshness row: excluded and hidden directories remain outside discovery."""
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0

    _write(root, ".hidden/hidden.py", "value = 2\n")
    _write(root, ".venv/venv.py", "value = 3\n")

    observed = drift(_document(output), root)
    assert observed.is_clean


def test_out_of_root_symlink_edit_is_not_detected(tmp_path: Path) -> None:
    """Freshness row: a file reached only through an escaping symlink is ignored."""
    root = tmp_path / "source"
    outside = tmp_path / "outside"
    _write(root, "app.py", "value = 1\n")
    external = _write(outside, "external.py", "value = 2\n")
    link = root / "linked.py"
    link.symlink_to(external)
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0

    external.write_text("value = 3\n", encoding="utf-8")

    observed = drift(_document(output), root)
    assert observed.is_clean


def test_parse_failed_file_has_no_changed_or_missing_finding_but_new_file_is_added(
    tmp_path: Path,
) -> None:
    """Freshness row: parse failures have no hash, while a new directory file is added."""
    root = tmp_path / "source"
    broken = _write(root, "broken.py", "def unfinished(\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 1

    broken.write_text("def unfinished():\n    return 2\n", encoding="utf-8")
    added = _write(root, "new.py", "value = 3\n")

    observed = drift(_document(output), root)
    assert observed.changed == ()
    assert observed.missing == ()
    assert set(observed.added) == {broken.name, added.name}


def test_graph_without_recorded_selection_refuses_automatic_refresh(tmp_path: Path, capsys) -> None:
    """Freshness row: a graph without selection metadata exits 2 instead of guessing."""
    root = tmp_path / "source"
    source = _write(root, "app.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0

    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["extensions"].pop("minotaur")
    output.write_text(json.dumps(raw), encoding="utf-8")
    stamp_path(output).unlink()
    source.write_text("def foo():\n    return 2\n", encoding="utf-8")

    status = cli.main(
        [
            "query",
            "definitions",
            "foo",
            "--graph",
            str(output),
            "--root",
            str(root),
        ]
    )
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "graph has no recorded source selection; cannot refresh" in captured.err


def test_regenerated_sidecar_trusts_hand_edited_graph_until_validate(
    tmp_path: Path, capsys
) -> None:
    """Freshness row: a regenerated sidecar trusts edits that --validate exposes."""
    root = tmp_path / "source"
    _write(root, "app.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0

    raw = json.loads(output.read_text(encoding="utf-8"))
    original_id = raw["nodes"][0]["id"]
    edited_id = "node:sha256:" + "0" * 64
    raw["nodes"][0]["id"] = edited_id
    for relationship in raw["relationships"]:
        if relationship["source"] == original_id:
            relationship["source"] = edited_id
        if relationship["target"] == original_id:
            relationship["target"] = edited_id
    output.write_text(json.dumps(raw), encoding="utf-8")
    stamp_path(output).write_text(f"{graph_digest(output.read_bytes())}\n", encoding="ascii")

    trusted = load_graph_file(output)
    assert trusted.validated is False
    assert (
        cli.main(
            [
                "query",
                "definitions",
                "foo",
                "--graph",
                str(output),
                "--root",
                str(root),
            ]
        )
        == 0
    )
    capsys.readouterr()

    status = cli.main(
        [
            "query",
            "definitions",
            "foo",
            "--graph",
            str(output),
            "--root",
            str(root),
            "--validate",
        ]
    )
    captured = capsys.readouterr()
    assert status == 2
    assert "does not match the digest recomputed from its identity" in captured.err


def test_query_ignores_selection_mismatch_but_analyze_reconciles_it(tmp_path: Path, capsys) -> None:
    """Freshness row: query drift ignores targets, while analyze reconciles them."""
    root = tmp_path / "source"
    selected = _write(root, "selected.py", "def foo():\n    return 1\n")
    package = _write(root, "pkg/other.py", "def bar():\n    return 2\n").parent
    output = tmp_path / "graph.json"
    assert _analyze(root, output, selected) == 0

    # The graph bytes are clean, so the query answers from its snapshot without
    # consulting the analyze target set or rewriting the graph.
    assert (
        cli.main(
            [
                "query",
                "definitions",
                "foo",
                "--graph",
                str(output),
                "--root",
                str(root),
            ]
        )
        == 0
    )
    query_capture = capsys.readouterr()
    assert query_capture.out == "selected.py:1  selected.foo  function\n"
    assert query_capture.err == ""

    # Analyze has a stricter clean-skip probe: a changed target selection
    # forces reconciliation even though the source bytes themselves are clean.
    assert (
        cli.main(
            [
                "analyze",
                "--root",
                str(root),
                "--output",
                str(output),
                str(package),
            ]
        )
        == 0
    )
    analyze_capture = capsys.readouterr()
    assert "graph is up to date, skipping analysis" not in analyze_capture.err
    assert json.loads(output.read_text(encoding="utf-8"))["extensions"]["minotaur"][
        "selection"
    ] == ["pkg"]


def test_edit_after_drift_is_not_detected_between_drift_and_answer(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Freshness row: an edit after drift leaves the public answer on the old snapshot."""
    root = tmp_path / "source"
    source = _write(root, "app.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0

    real_drift = cli.drift

    def drift_then_edit(document, drift_root):
        observed = real_drift(document, drift_root)
        source.write_text("# edit landed after drift\ndef foo():\n    return 2\n", encoding="utf-8")
        return observed

    # This patches only the public CLI's drift seam: the edit is introduced
    # after the real comparison returns, before the query builds its answer.
    monkeypatch.setattr(cli, "drift", drift_then_edit)
    status = cli.main(
        [
            "query",
            "definitions",
            "foo",
            "--graph",
            str(output),
            "--root",
            str(root),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "app.py:1  app.foo  function\n"
    assert captured.err == ""
    assert source.read_text(encoding="utf-8").startswith("# edit landed after drift")


def test_diff_does_not_call_source_drift(tmp_path: Path, monkeypatch, capsys) -> None:
    """Freshness row: diff compares snapshots and never calls the source drift guard."""
    root = tmp_path / "source"
    source = _write(root, "app.py", "def foo():\n    return 1\n")
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    assert _analyze(root, old, root) == 0
    source.write_text("def foo():\n    return 2\n", encoding="utf-8")
    assert _analyze(root, new, root) == 0

    def fail(*_args, **_kwargs):
        raise AssertionError("diff must not call drift")

    monkeypatch.setattr(cli, "drift", fail)
    assert cli.main(["query", "diff", str(old), str(new)]) == 0
    assert capsys.readouterr().err == ""


def test_context_does_not_call_source_drift_and_no_refresh_is_a_noop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Freshness row: context ignores drift and its no-refresh flag changes nothing."""
    root = tmp_path / "source"
    source = _write(root, "app.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0
    source.write_text("def foo():\n    return 2\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise AssertionError("context must not call drift")

    monkeypatch.setattr(cli, "drift", fail)
    common = [
        "query",
        "context",
        "--graph",
        str(output),
        "--root",
        str(root),
        "--site",
        "app.py:1",
    ]
    assert cli.main(common) == 0
    without_flag = capsys.readouterr()
    assert cli.main([*common, "--no-refresh"]) == 0
    with_flag = capsys.readouterr()
    assert (with_flag.out, with_flag.err) == (without_flag.out, without_flag.err)
