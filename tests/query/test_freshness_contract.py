"""Pin the intentionally bounded edges of the graph-freshness contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

from minotaur import cli
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import graph_digest, load_graph_file, stamp_path
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.query.freshness import drift, recorded_selection, recorded_selection_view


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


def test_recorded_selection_view_preserves_refresh_normalization_and_presence() -> None:
    cases = (
        ({"minotaur": {"selection": ["z", 2, "a"]}}, True, ("a", "z")),
        ({"minotaur": {"selection": ("z", "a")}}, True, ("a", "z")),
        ({}, False, ()),
        ({"minotaur": {"selection": "a.py"}}, False, ()),
        ({"minotaur": {"selection": []}}, True, ()),
        ({"minotaur": {"selection": [1, None]}}, True, ()),
    )
    for extensions, present, targets in cases:
        document = GraphDocument(
            coordinate_encoding=CoordinateEncoding.UTF_8,
            extensions=extensions,
        )
        observed = recorded_selection_view(document)
        assert (observed.recorded, observed.targets) == (present, targets)
        assert recorded_selection(document) == targets


def test_new_python_outside_recorded_target_is_not_detected(tmp_path: Path) -> None:
    """docs/concepts/freshness.md — `analyze` a target, add a new `.py` outside every recorded directory."""  # noqa: E501
    root = tmp_path / "source"
    package = _write(root, "pkg/known.py", "value = 1\n").parent
    output = tmp_path / "graph.json"
    assert _analyze(root, output, package) == 0

    _write(root, "outside.py", "value = 2\n")

    observed = drift(_document(output), root)
    assert observed.is_clean
    assert observed.added == ()


def test_javascript_edit_is_detected_and_refreshed(tmp_path: Path, capsys) -> None:
    """docs/concepts/freshness.md — edit a tracked `.js` file, then query."""
    root = tmp_path / "source"
    source = _write(root, "app.js", "function value() {}\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0
    capsys.readouterr()

    source.write_text("function value() { return 1; }\n", encoding="utf-8")

    status = cli.main(
        [
            "query",
            "definitions",
            "value",
            "--graph",
            str(output),
            "--root",
            str(root),
        ]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert captured.out == "app.js:1  app.value  function\n"
    assert captured.err == (
        "minotaur: refreshed graph (1 drifted paths)\nminotaur: stale: app.js\n"
    )


def test_unsupported_extension_edit_is_not_detected(tmp_path: Path) -> None:
    """docs/concepts/freshness.md — `analyze`, edit an unsupported extension."""
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
    """docs/concepts/freshness.md — `analyze`, then add or edit a file under an excluded or hidden directory that was never explicitly selected."""  # noqa: E501
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0

    _write(root, ".hidden/hidden.py", "value = 2\n")
    _write(root, ".venv/venv.py", "value = 3\n")

    observed = drift(_document(output), root)
    assert observed.is_clean


def test_out_of_root_symlink_edit_is_not_detected(tmp_path: Path) -> None:
    """docs/concepts/freshness.md — `analyze`, edit a file reached only through an out-of-root symlink."""  # noqa: E501
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
    """docs/concepts/freshness.md — `analyze` a file with a parse failure, then edit that file."""
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


def test_query_refresh_returns_one_without_reprinting_parse_diagnostic(
    tmp_path: Path, capsys
) -> None:
    """docs/concepts/freshness.md — `analyze` a file with a parse failure, then edit that file."""  # noqa: E501
    root = tmp_path / "source"
    _write(root, "app.py", "def foo():\n    return 1\n")
    output = tmp_path / "graph.json"
    assert _analyze(root, output, root) == 0
    capsys.readouterr()

    _write(root, "broken.py", "def broken(\n")
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

    assert status == 1
    assert captured.out == "app.py:1  app.foo  function\n"
    assert captured.err == (
        "minotaur: refreshed graph (1 drifted paths)\nminotaur: stale: broken.py\n"
    )
    assert "parse-error" not in captured.err


def test_graph_without_recorded_selection_refuses_automatic_refresh(tmp_path: Path, capsys) -> None:
    """docs/concepts/freshness.md — Load a graph with no recorded selection and query after source drift."""  # noqa: E501
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
    """docs/concepts/freshness.md — Hand-edit graph bytes and regenerate its sidecar, then read it."""  # noqa: E501
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


def test_stale_sidecar_detects_schema_shape_and_node_identity_but_not_labels(
    tmp_path: Path, capsys
) -> None:
    """docs/concepts/freshness.md — Hand-edit graph bytes and leave the sidecar untouched, then read it."""  # noqa: E501
    root = tmp_path / "source"
    _write(root, "app.py", "def foo():\n    return 1\n")

    def graph_with_stale_sidecar(name: str) -> tuple[Path, dict[str, object]]:
        output = tmp_path / name
        assert _analyze(root, output, root) == 0
        raw = json.loads(output.read_text(encoding="utf-8"))
        stamp_path(output).write_text(f"{graph_digest(output.read_bytes())}\n", encoding="ascii")
        return output, raw

    label_graph, label_raw = graph_with_stale_sidecar("label.json")
    label_raw["nodes"][0]["label"] = "hand-edited-label"
    label_graph.write_text(json.dumps(label_raw), encoding="utf-8")
    assert (
        cli.main(
            [
                "query",
                "definitions",
                "foo",
                "--graph",
                str(label_graph),
                "--root",
                str(root),
                "--no-refresh",
            ]
        )
        == 0
    )
    label_capture = capsys.readouterr()
    assert label_capture.err == ""
    assert (
        stamp_path(label_graph).read_text(encoding="ascii")
        == f"{graph_digest(label_graph.read_bytes())}\n"
    )

    identity_graph, identity_raw = graph_with_stale_sidecar("identity.json")
    original_id = identity_raw["nodes"][0]["id"]
    edited_id = "node:sha256:" + "0" * 64
    identity_raw["nodes"][0]["id"] = edited_id
    for relationship in identity_raw["relationships"]:
        if relationship["source"] == original_id:
            relationship["source"] = edited_id
        if relationship["target"] == original_id:
            relationship["target"] = edited_id
    identity_graph.write_text(json.dumps(identity_raw), encoding="utf-8")
    assert (
        cli.main(
            [
                "query",
                "definitions",
                "foo",
                "--graph",
                str(identity_graph),
                "--root",
                str(root),
                "--no-refresh",
            ]
        )
        == 2
    )
    assert "does not match the digest recomputed from its identity" in capsys.readouterr().err

    shape_graph, shape_raw = graph_with_stale_sidecar("shape.json")
    del shape_raw["nodes"][0]["label"]
    shape_graph.write_text(json.dumps(shape_raw), encoding="utf-8")
    assert (
        cli.main(
            [
                "query",
                "definitions",
                "foo",
                "--graph",
                str(shape_graph),
                "--root",
                str(root),
                "--no-refresh",
            ]
        )
        == 2
    )
    assert "'label' is a required property" in capsys.readouterr().err


def test_query_ignores_selection_mismatch_but_analyze_reconciles_it(tmp_path: Path, capsys) -> None:
    """docs/concepts/freshness.md — Query a graph whose bytes are clean but its selection metadata differs from the requested analyze targets."""  # noqa: E501
    root = tmp_path / "source"
    selected = _write(root, "selected.py", "def foo():\n    return 1\n")
    package = _write(root, "pkg/other.py", "def bar():\n    return 2\n").parent
    output = tmp_path / "graph.json"
    assert _analyze(root, output, selected) == 0
    before_query = output.read_bytes()

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
    assert output.read_bytes() == before_query
    assert json.loads(output.read_text(encoding="utf-8"))["extensions"]["minotaur"][
        "selection"
    ] == ["selected.py"]

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
    """docs/concepts/freshness.md — An edit lands after `drift()` and before the answer is printed."""  # noqa: E501
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
    """docs/concepts/freshness.md — Run `query diff`."""
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
    """docs/concepts/freshness.md — Run `query context` and Run `query context --no-refresh`."""
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
    # The marker is the documented observable; equality alone would still
    # pass if the marker were dropped from both invocations.
    assert without_flag.out.startswith("[file changed since analysis]\n")
    assert cli.main([*common, "--no-refresh"]) == 0
    with_flag = capsys.readouterr()
    assert (with_flag.out, with_flag.err) == (without_flag.out, without_flag.err)


def test_visualize_source_root_does_not_call_source_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """docs/concepts/freshness.md — Run `visualize --source-root` after source drift."""
    root = tmp_path / "source"
    source = _write(
        root,
        "app.py",
        "def bar():\n    return 1\n\ndef foo():\n    return bar()\n",
    )
    graph = tmp_path / "graph.json"
    html = tmp_path / "graph.html"
    assert _analyze(root, graph, root) == 0
    source.write_text(
        "# current source is stale\n" + source.read_text(encoding="utf-8"), encoding="utf-8"
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("visualize must not call drift")

    monkeypatch.setattr(cli, "drift", fail)
    assert (
        cli.main(
            [
                "visualize",
                "--input",
                str(graph),
                "--output",
                str(html),
                "--source-root",
                str(root),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "current source is stale" in html.read_text(encoding="utf-8")


def test_freshness_document_links_and_first_read_anchor_resolve() -> None:
    """Pin freshness, language-guide, and interpreter-guide documentation contracts."""
    repository = Path(__file__).resolve().parents[2]
    freshness = repository / "docs/concepts/freshness.md"
    javascript = repository / "docs/guides/analyze-javascript.md"
    links = {
        repository / "README.md": "docs/concepts/freshness.md",
        repository / "docs/guides/query-reference.md": "../concepts/freshness.md",
        repository / "docs/guides/analyze-python.md": "../concepts/freshness.md",
    }

    for document, relative_link in links.items():
        assert relative_link in document.read_text(encoding="utf-8")
        assert (document.parent / relative_link).resolve() == freshness.resolve()
    assert "### First-read validation cost" in freshness.read_text(encoding="utf-8")

    readme = (repository / "README.md").read_text(encoding="utf-8")
    freshness_text = freshness.read_text(encoding="utf-8")
    python_guide = (repository / "docs/guides/analyze-python.md").read_text(encoding="utf-8")
    assert "docs/guides/analyze-javascript.md" in readme
    assert "currently `.py` only" not in readme
    assert "does not yet include C#, JavaScript" not in readme
    assert "Non-Python edit" not in freshness_text
    assert "registry currently has one `.py` registration" not in freshness_text
    assert "pure `.js` selection boundary" in python_guide

    interpreter_guide = (repository / "docs/guides/create-a-language-interpreter.md").read_text(
        encoding="utf-8"
    )
    registration = (
        '".example",\n    analyze_example_language_files,\n    namespace="minotaur-example"'
    )
    assert registration in interpreter_guide
    assert "JavaScript" not in interpreter_guide

    javascript_text = javascript.read_text(encoding="utf-8")
    assert "## Supported declarations" in javascript_text
    assert "## Supported module imports" in javascript_text
    assert "## Unsupported module syntax" in javascript_text
    assert "## Explicit exclusions" in javascript_text
    assert "import { helper as localHelper } from './lib.js';" in javascript_text
    assert "all-or-nothing" in javascript_text
    assert "minotaur_python_scope_resolution" not in javascript_text
    inbound_links = {
        repository / "README.md": "docs/guides/analyze-javascript.md",
        repository / "docs/guides/query-reference.md": "analyze-javascript.md",
    }
    for document, relative_link in inbound_links.items():
        assert relative_link in document.read_text(encoding="utf-8")
        assert (document.parent / relative_link).resolve() == javascript.resolve()


def test_freshness_documents_content_keyed_reuse_and_last_generation_provenance() -> None:
    """Pin the content-keyed freshness wording across freshness and language guides."""
    repository = Path(__file__).resolve().parents[2]

    def collapsed(path: Path) -> str:
        return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).strip()

    freshness = collapsed(repository / "docs/concepts/freshness.md")
    python_guide = collapsed(repository / "docs/guides/analyze-python.md")
    javascript_guide = collapsed(repository / "docs/guides/analyze-javascript.md")

    assert "source content is clean" in freshness
    assert "requested target selection equals the graph's recorded selection" in freshness
    assert "Git `source_control` is not a gate" in freshness
    assert "last-generation provenance" in freshness
    assert "stamp lagging `HEAD`" in freshness
    assert "content digests are the freshness authority" in python_guide
    assert "last real generation in `source_control`" in python_guide
    assert "freshness gate or substitute for content digests" in javascript_guide
    assert (
        "Explicit `diff OLD NEW` remains graph-only: it compares two supplied graph documents "
        "and does not inspect a source root or configuration" in freshness
    )
    assert (
        "Committed-reference `diff` reads the committed graph and sidecar from `HEAD`, analyzes "
        "the current configured selection in memory, and compares the two structures" in freshness
    )
    assert "it does not write or stamp a graph or sidecar" in freshness
    assert (
        "`diff` compares two graph documents and does not inspect a source root." not in freshness
    )

    assert "recorded commit and branch still match the current checkout" not in python_guide
    assert "A Git commit or branch change also causes re-analysis" not in python_guide
    assert "Git snapshot context are current" not in javascript_guide
    assert "Git branch/commit metadata equals the graph's metadata" not in freshness
    assert "checks three conditions" not in freshness
