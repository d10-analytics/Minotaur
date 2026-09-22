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
    checkout_before = {
        path.relative_to(ROOT): path.read_bytes()
        for path in (ROOT / "examples/system-walkthrough/shop").rglob("*")
        if path.is_file()
    }

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
            after = state()
            requested = (
                {arguments[arguments.index("--html") + 1]} if "--html" in arguments else set()
            )
            changed = {
                name for name in set(before) | set(after) if before.get(name) != after.get(name)
            }
            assert changed <= requested, "comparison changed source, artifacts, definitions, or Git"

    monkeypatch.setitem(runner["systems"].__globals__, "command", checked)
    runner["systems"](tmp_path)
    diffs = [(args, status, output) for args, status, output in observed if args[0] == "query"]
    # A clean HEAD comparison, then working-tree comparisons that write a
    # report, then the historical pair and its report. Both workflows exit 1.
    assert [status for _, status, _ in diffs] == [0, 1, 1, 1, 1, 1, 1]
    assert "no system differences\n" in diffs[0][2]

    blocks = re.findall(
        r"```text\n(.*?)```",
        (ROOT / "examples/system-walkthrough/comparison.md").read_text(),
        re.DOTALL,
    )
    rows = [block for block in blocks if "boundary added" in block]
    assert len(rows) == 1
    changes = rows[0]
    assert changes in diffs[1][2], "the working-tree report must match the walkthrough rows"
    assert changes in diffs[4][2], "the historical report must match the walkthrough rows"
    assert changes in diffs[6][2], "the HTML report must print the same rows"
    assert "old evidence" in diffs[5][2] and "new evidence" in diffs[5][2]

    # The documented historical rows are byte-equal to the real standard
    # output, and that output carries no revision-identity header at all: the
    # identities live in the saved report, not on the command line.
    historical = diffs[4][2].splitlines()[1:-1]
    assert "\n".join(historical[:19]) + "\n" == changes
    assert historical[19].startswith("old coverage: ")
    assert historical[20].startswith("new coverage: ")
    assert historical[21] == 'old selection: {"status":"recorded","targets":["shop"]}'
    assert historical[22] == 'new selection: {"status":"recorded","targets":["shop"]}'
    for token in ("Before:", "After:", "134b138", "a2b78dd"):
        assert token not in diffs[4][2]
    assert historical == diffs[1][2].splitlines()[1:-1]

    working_tree = json.loads(diffs[3][2].splitlines()[1])
    assert working_tree["exit_code"] == 1
    assert working_tree["revisions"]["new"] == "Working tree at report generation"
    assert working_tree["revisions"]["old"].startswith("HEAD · ")
    assert working_tree["comparison"]["after"]["commit"] is None
    assert [
        (change["kind"], change["key"])
        for change in working_tree["surface_changes"]
        if change["kind"] == "added"
    ] == [("added", ["billing", "shop/billing.py", "shop.billing.refund"])]
    assert [
        change["reasons"]
        for change in working_tree["call_changes"]
        if change["status"] == "changed"
    ] == [["expression_changed"]]
    moved = {
        (change["status"], side, change[side]["node"]["label"])
        for change in working_tree["nodes"]
        for side in ("before", "after")
        if isinstance(change.get(side), dict)
    }
    assert ("removed", "before", "shop.orders.complete_order") in moved
    assert ("added", "after", "shop.order_ops.complete_order") in moved

    # The comparison analyzes captured source directly, so the fixture commits
    # no graph or sidecar at all and still reports the complete change set.
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=tmp_path, text=True, capture_output=True, check=True
    ).stdout.splitlines()
    assert "graph.json" not in tracked
    assert "graph.json.sha256" not in tracked
    assert checkout_before == {
        path.relative_to(ROOT): path.read_bytes()
        for path in (ROOT / "examples/system-walkthrough/shop").rglob("*")
        if path.is_file()
    }, "the walkthrough must not modify the checked-in example sources"


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
