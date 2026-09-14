"""Execute beginner examples and verify the facts their explanations promise."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import load_graph_file
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.language_interpreter.reading import ParsedSource
from minotaur.query.freshness import drift

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "examples/run_walkthrough.py"


@pytest.mark.parametrize(
    ("scenario", "document"),
    [
        ("python", "examples/getting-started/README.md"),
        ("javascript", "examples/javascript-workflow/README.md"),
        ("bindings", "docs/guides/python-binding-examples.md"),
        ("malformed", "docs/guides/source-error-recovery.md"),
    ],
)
def test_walkthrough_prints_documented_transcript(scenario: str, document: str) -> None:
    expected = re.findall(r"```text\n(.*?)```", (ROOT / document).read_text(), re.DOTALL)
    assert len(expected) == 1
    result = subprocess.run(
        [sys.executable, str(RUNNER), scenario, "--no-pause"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""

    # Parser messages differ between supported Python versions. Keep the path,
    # diagnostic code and status exact, normalizing only parser wording/location.
    def normalize(text: str) -> str:
        return re.sub(r"broken.py:\d+:\d+: parse-error: [^\n]+", "broken.py: parse-error", text)

    assert normalize(result.stdout) == normalize(expected[0])


def test_javascript_member_call_remains_unresolved(tmp_path: Path) -> None:
    runner = runpy.run_path(str(RUNNER))
    runner["javascript"](tmp_path)
    graph = load_graph_file(tmp_path / "graph.json").document
    nodes = {node.id: node for node in graph.nodes}
    unresolved = [node for node in graph.nodes if node.label == "console.log"]
    assert len(unresolved) == 1
    assert unresolved[0].node_class.value == "unresolved-reference"
    assert any(
        edge.kind == "references" and edge.target == unresolved[0].id
        for edge in graph.relationships
    )
    assert not any(
        edge.kind == "calls" and edge.target == unresolved[0].id for edge in graph.relationships
    )
    assert any(
        edge.kind == "calls"
        and nodes[edge.source].label == "app.welcome"
        and nodes[edge.target].label == "lib.greet"
        for edge in graph.relationships
    )


def test_system_example_reports_changes_without_comparison_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = runpy.run_path(str(RUNNER))
    original = runner["command"]
    observed = []

    def state() -> dict[str, bytes]:
        return {
            str(path.relative_to(tmp_path)): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

    def checked(root: Path, *arguments: str, expected: int = 0) -> None:
        before = state()
        original(root, *arguments, expected=expected)
        output = capsys.readouterr().out
        observed.append((arguments, expected, output))
        if arguments[:2] == ("query", "diff"):
            assert state() == before, "comparison changed source, artifacts, definitions, or Git"

    monkeypatch.setitem(runner["systems"].__globals__, "command", checked)
    runner["systems"](tmp_path)
    diffs = [(args, status, output) for args, status, output in observed if args[0] == "query"]
    assert [status for _, status, _ in diffs] == [0, 1, 1, 1, 1, 1]
    assert "no system differences\n" in diffs[0][2]
    rows = re.findall(
        r"```text\n(.*?)```",
        (ROOT / "examples/system-walkthrough/comparison.md").read_text(),
        re.DOTALL,
    )
    assert len(rows) == 1
    assert rows[0] in diffs[1][2]
    assert rows[0] in diffs[2][2]
    assert "old evidence" in diffs[3][2] and "new evidence" in diffs[3][2]
    payload = json.loads(diffs[4][2].splitlines()[1])
    assert payload["exit_code"] == 1
    assert [
        (change["kind"], change["key"], change["involved_systems"])
        for change in payload["surface_changes"]
    ] == [("added", ["billing", "shop/billing.py", "shop.billing.refund"], ["billing", "orders"])]
    added = payload["surface_changes"][0]
    assert added["old"] is None
    relationship = added["new"]["relationships"][0]
    assert relationship["source"]["label"] == "shop.orders.cancel_order"
    assert relationship["target"]["label"] == "shop.billing.refund"
    assert relationship["evidence"][0]["sites"][0]["path"] == "shop/orders.py"
    assert "membership changed: shop/ledger.py — unassigned -> billing\n" in diffs[5][2]
    baseline = subprocess.run(
        ["git", "show", "HEAD:graph.json"], cwd=tmp_path, capture_output=True, check=True
    ).stdout
    assert (tmp_path / "graph.json").read_bytes() == baseline
    for name in ("billing.py", "orders.py"):
        assert (tmp_path / "shop" / name).read_bytes() == (
            ROOT / "examples/system-walkthrough/shop" / name
        ).read_bytes()


def test_documented_file_node_hashes_original_bytes_and_detects_edit(tmp_path: Path) -> None:
    guide = (ROOT / "docs/guides/create-a-language-interpreter.md").read_text()
    snippet = next(
        block
        for block in re.findall(r"```python\n(.*?)```", guide, re.DOTALL)
        if "def source_file_node" in block
    )
    namespace: dict[str, object] = {}
    exec(compile(snippet, "<documented source_file_node>", "exec"), namespace)
    content = b"\xef\xbb\xbfvalue = 1\r\n"
    source = tmp_path / "sample.py"
    source.write_bytes(content)
    parsed = ParsedSource("sample.py", content, content.decode("utf-8-sig"), None)
    node = namespace["source_file_node"](parsed, "minotaur-python", "python")
    assert (
        node.extensions["minotaur-python"]["content_sha256"] == hashlib.sha256(content).hexdigest()
    )
    graph = GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8, nodes=(node,))
    assert drift(graph, tmp_path).is_clean
    source.write_bytes(content + b"# changed\r\n")
    assert drift(graph, tmp_path).changed == ("sample.py",)
