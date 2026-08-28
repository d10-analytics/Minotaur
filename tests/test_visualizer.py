"""Behavioral coverage for the portable HTML visualizer boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import GraphLoadError, load_graph_bytes, stamp_path
from minotaur.graph_visualizer.html.render import render_html
from minotaur.graph_visualizer.presentation import build_presentation
from minotaur.graph_visualizer.source import prepare_excerpts

ROOT = Path(__file__).parents[1]
VENDOR = ROOT / "src/minotaur/graph_visualizer/html/vendor"
EXAMPLE = ROOT / "examples/python-workflow"


def _graph() -> dict[str, object]:
    path = ROOT / "examples/synthetic-graphs/small-workflow.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_load_boundary_rejects_schema_and_semantic_failures_before_canonicalization() -> None:
    invalid = _graph()
    invalid["coordinate_encoding"] = "bad"
    with pytest.raises(GraphLoadError, match="coordinate_encoding"):
        load_graph_bytes(json.dumps(invalid).encode())

    dangling = _graph()
    relationships = dangling["relationships"]
    assert isinstance(relationships, list)
    relationships[0]["source"] = "node:sha256:" + "0" * 64
    with pytest.raises(GraphLoadError, match="semantic validation failed"):
        load_graph_bytes(json.dumps(dangling).encode())


def test_source_excerpts_are_contained_merged_and_explicit_when_unavailable(tmp_path: Path) -> None:
    graph = _graph()
    loaded = load_graph_bytes(json.dumps(graph).encode())
    source_root = tmp_path / "root"
    (source_root / "src").mkdir(parents=True)
    (source_root / "src/checkout.py").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    escaped = source_root / "src/escape.py"
    escaped.symlink_to(tmp_path / "outside.py")
    (tmp_path / "outside.py").write_text("secret\n", encoding="utf-8")
    excerpts = prepare_excerpts(loaded.canonical, source_root)
    paths = excerpts["paths"]
    assert paths["src/checkout.py"]["status"] == "available"
    assert paths["src/checkout.py"]["spans"] == [
        {"start": 0, "lines": ["one", "two", "three", "four", "five"]}
    ]
    assert excerpts["call_sites"]["0"][0]["caller_start"] == 2

    missing = prepare_excerpts(loaded.canonical, None)
    assert missing["paths"]["src/checkout.py"] == {
        "status": "unavailable",
        "reason": "no source root was provided",
    }
    escaped_graph = _graph()
    relationships = escaped_graph["relationships"]
    assert isinstance(relationships, list)
    relationships[0]["evidence"][0]["locations"][0]["path"] = "src/escape.py"
    escaped = prepare_excerpts(
        load_graph_bytes(json.dumps(escaped_graph).encode()).canonical, source_root
    )
    assert escaped["paths"]["src/escape.py"] == {
        "status": "unavailable",
        "reason": "path is missing or escapes the source root",
    }


def test_call_site_associations_keep_all_provenance_at_one_physical_location(
    tmp_path: Path,
) -> None:
    # Two producers can report the same range. Keep this at the extraction
    # boundary so UI regressions cannot be hidden by a pre-collapsed fixture.
    graph = _graph()
    relationships = graph["relationships"]
    assert isinstance(relationships, list)
    relationship = relationships[0]
    relationship["evidence"].append(
        {
            "provenance": "curated-rule",
            "rule": {"id": "test-rule"},
            "locations": [relationship["evidence"][0]["locations"][0]],
        }
    )
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src/checkout.py").write_text("\n".join(str(i) for i in range(80)), encoding="utf-8")
    excerpts = prepare_excerpts(load_graph_bytes(json.dumps(graph).encode()).canonical, root)
    sites = excerpts["call_sites"]["0"]
    assert [site["provenance"] for site in sites] == ["static-analysis", "curated-rule"]
    assert all(site["caller_start"] == 2 for site in sites)


def test_renderer_is_self_contained_and_json_safe() -> None:
    loaded = load_graph_bytes(json.dumps(_graph()).encode())
    presentation = build_presentation(loaded.canonical, {"<unsafe>": {"status": "unavailable"}})
    html = render_html(presentation).decode("utf-8")
    assert "<script src=" not in html
    assert "<link " not in html
    assert "cytoscape-dagre" in html
    assert 'id="minotaur-presentation"' in html
    assert "textContent" in html


def test_renderer_keeps_template_markers_inside_embedded_source_data() -> None:
    """A source excerpt must not trigger a second template substitution.

    This is intentionally a renderer-level test: a marker in ordinary source
    text is realistic, while checking only trusted presentation data would miss
    the ordering mistake that could alter the self-contained document.
    """
    loaded = load_graph_bytes(json.dumps(_graph()).encode())
    presentation = build_presentation(
        loaded.canonical,
        {
            "src/minotaur/graph_visualizer/html/render.py": {
                "status": "available",
                "spans": [{"start": 0, "lines": ["/*__VIEWER_JS__*/"]}],
            }
        },
    )
    html = render_html(presentation).decode("utf-8")
    prefix = '<script id="minotaur-presentation" type="application/json">'
    embedded = html.split(prefix, 1)[1].split("</script>", 1)[0]
    payload = json.loads(embedded)

    assert payload["excerpts"]["src/minotaur/graph_visualizer/html/render.py"]["spans"] == [
        {"start": 0, "lines": ["/*__VIEWER_JS__*/"]}
    ]


def test_vendored_asset_checksums_match_the_recorded_release() -> None:
    assert sha256(VENDOR.joinpath("cytoscape-3.34.0.min.js").read_bytes()).hexdigest() == (
        "9c2a3bf2592e0b14a1f7bec07c03a54f16dedf32af9cd0af155c716aa6c87bc3"
    )
    assert sha256(VENDOR.joinpath("cytoscape-dagre-4.0.0.js").read_bytes()).hexdigest() == (
        "91f342cc2705aa9cad6a26f468d9ee5faa9e057d9172c3f9e732548fc61c660d"
    )


def test_presentation_preserves_all_provenance_records_on_one_relationship() -> None:
    path = ROOT / "examples/synthetic-graphs/provenance-demo.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    relationships = raw["relationships"]
    assert isinstance(relationships, list)
    relationships[0]["evidence"].append({"provenance": "curated-rule", "rule": {"id": "test-rule"}})
    loaded = load_graph_bytes(json.dumps(raw).encode())
    presentation = build_presentation(loaded.canonical)
    assert presentation["provenance"] == ["curated-rule", "imported-graph", "static-analysis"]
    graph = presentation["graph"]
    assert isinstance(graph, dict)
    relationships = graph["relationships"]
    assert isinstance(relationships, list)
    assert len(relationships) == 1
    assert len(relationships[0]["evidence"]) == 3


def test_visualize_cli_refuses_alias_and_writes_atomically(tmp_path: Path) -> None:
    input_path = tmp_path / "graph.json"
    input_path.write_text(json.dumps(_graph()), encoding="utf-8")
    output = tmp_path / "view.html"
    assert cli.main(["visualize", "--input", str(input_path), "--output", str(input_path)]) == 2
    assert cli.main(["visualize", "--input", str(input_path), "--output", str(output)]) == 0
    assert output.read_bytes().startswith(b"<!doctype html>")
    assert cli.main(["visualize", "--input", str(input_path), "--output", str(output)]) == 2
    assert (
        cli.main(["visualize", "--input", str(input_path), "--output", str(output), "--force"]) == 0
    )


def test_visualize_cli_warns_but_writes_large_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    input_path = tmp_path / "graph.json"
    input_path.write_text(json.dumps(_graph()), encoding="utf-8")
    output = tmp_path / "large.html"
    monkeypatch.setattr(cli, "render_html", lambda _: b"x" * (10 * 1024 * 1024 + 1))
    assert cli.main(["visualize", "--input", str(input_path), "--output", str(output)]) == 0
    assert output.stat().st_size > 10 * 1024 * 1024
    assert "exceeds 10 MiB" in capsys.readouterr().err


def test_checked_in_python_workflow_artifacts_match_fresh_cli_output(tmp_path: Path) -> None:
    """Keep the public example synchronized with analyzer and renderer behavior.

    Byte equality is purposeful here. The example is a distributable offline
    artifact, so a semantic-only comparison would permit stale embedded UI
    code even when the graph JSON itself still appears correct.
    """
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/generate_example_output.py"),
            "--output-directory",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
    )
    generated_graph = tmp_path / "minotaur-graph.json"
    generated_html = tmp_path / "minotaur-graph.html"
    checked_in_graph = EXAMPLE / generated_graph.name
    checked_in_html = EXAMPLE / generated_html.name

    loaded = load_graph_bytes(generated_graph.read_bytes())
    assert loaded.document.nodes
    assert {
        evidence.provenance.value
        for relationship in loaded.document.relationships
        for evidence in relationship.evidence
    } == {"static-analysis"}
    embedded = (
        generated_html.read_text(encoding="utf-8")
        .split('<script id="minotaur-presentation" type="application/json">', 1)[1]
        .split("</script>", 1)[0]
    )
    presentation = json.loads(embedded)
    excerpts = presentation["excerpts"]["paths"]
    assert excerpts
    assert {excerpt["status"] for excerpt in excerpts.values()} == {"available"}
    assert generated_graph.read_bytes() == checked_in_graph.read_bytes()
    assert generated_html.read_bytes() == checked_in_html.read_bytes()

    generated_sidecar = stamp_path(generated_graph)
    checked_in_sidecar = stamp_path(checked_in_graph)
    assert generated_sidecar.exists(), "generate_example_output.py must produce a sidecar"
    assert checked_in_sidecar.exists(), "checked-in sidecar missing from examples/"
    assert generated_sidecar.read_bytes() == checked_in_sidecar.read_bytes()


def test_renderer_keeps_resolved_reference_edges_from_analyzed_source(tmp_path: Path) -> None:
    """A callback passed by name must still render as a ``references`` edge.

    The checked-in visualizer fixtures predate resolved reference edges, so the
    graph here is analyzed from source instead of hand-written: that keeps the
    renderer contract tied to what the analyzer actually emits for
    ``register(handler)`` rather than to a fixture that could drift from it.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "fixture.py").write_text(
        "def handler():\n"
        "    return 1\n\n\n"
        "def register(callback):\n"
        "    return callback\n\n\n"
        "register(handler)\n",
        encoding="utf-8",
    )
    graph_path = tmp_path / "graph.json"
    assert (
        cli.main(["analyze", "--root", str(tmp_path), "--output", str(graph_path), str(source)])
        == 0
    )

    loaded = load_graph_bytes(graph_path.read_bytes())
    labels = {node.id: node.label for node in loaded.document.nodes}
    assert [
        (labels[relationship.source], labels[relationship.target])
        for relationship in loaded.document.relationships
        if relationship.kind == "references"
    ] == [
        ("src.fixture", "src.fixture.handler"),
    ]

    presentation = build_presentation(
        loaded.canonical, prepare_excerpts(loaded.canonical, tmp_path)
    )
    assert "references" in presentation["relationship_kinds"]
    html = render_html(presentation).decode("utf-8")
    assert html.startswith("<!doctype html>")

    prefix = '<script id="minotaur-presentation" type="application/json">'
    payload = json.loads(html.split(prefix, 1)[1].split("</script>", 1)[0])
    graph = payload["graph"]
    embedded_labels = {node["id"]: node["label"] for node in graph["nodes"]}
    assert [
        (embedded_labels[relationship["source"]], embedded_labels[relationship["target"]])
        for relationship in graph["relationships"]
        if relationship["kind"] == "references"
    ] == [
        ("src.fixture", "src.fixture.handler"),
    ]
