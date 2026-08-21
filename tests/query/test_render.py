"""Exact-output coverage for the shared query JSON envelope."""

from __future__ import annotations

from pathlib import Path

import pytest

from minotaur import cli

# Every query serializes through one helper, so the envelope is asserted as an
# exact string: compact separators, sorted keys, and a single trailing newline
# are the contract agents parse, and a per-module renderer must not drift from
# it.  The record queries also carry the freshness of the answer itself
# (`refreshed`, `stale`); the snapshot queries `context` and `diff` do not
# refresh anything and keep their own envelopes.
EXPECTED = {
    "definitions": (
        '{"query":"definitions","refreshed":false,"results":[{"duplicate":false,'
        '"kind":"function","line":1,"path":"mod.py","symbol":"mod.target"}],"stale":[]}\n'
    ),
    "callers": '{"query":"callers","refreshed":false,"results":[],"stale":[]}\n',
    "impact": (
        '{"query":"impact","refreshed":false,"results":[{"boundary":false,"depth":0,'
        '"kind":"function","symbol":"mod.target"}],"stale":[]}\n'
    ),
    "unreferenced": (
        '{"query":"unreferenced","refreshed":false,"results":[{"kind":"function","line":1,'
        '"path":"mod.py","symbol":"mod.target","text_mention":false}],"stale":[]}\n'
    ),
    "context": (
        '{"query":"context","results":[{"hash_available":true,"lines":[{"line":1,'
        '"target":true,"text":"def target():"}],"path":"mod.py","stale":false}]}\n'
    ),
    "diff": (
        '{"added":[],"query":"diff","relationships_added":[],"relationships_removed":[],'
        '"relocated":[],"removed":[]}\n'
    ),
}


@pytest.mark.parametrize(
    ("query_name", "query_args"),
    [
        ("definitions", ("definitions", "target")),
        ("callers", ("callers", "mod.target")),
        ("impact", ("impact", "mod.target")),
        ("unreferenced", ("unreferenced",)),
        ("context", ("context", "--site", "mod.py:1", "--before", "0", "--after", "0")),
        ("diff", ("diff",)),
    ],
)
def test_query_json_output_is_byte_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    query_name: str,
    query_args: tuple[str, ...],
) -> None:
    (tmp_path / "mod.py").write_text("def target():\n    pass\n", encoding="utf-8")
    graph = tmp_path / "graph.json"
    assert (
        cli.main(["analyze", "--root", str(tmp_path), "--output", str(graph), str(tmp_path)]) == 0
    )
    old_graph = tmp_path / "old.json"
    old_graph.write_bytes(graph.read_bytes())

    if query_name == "diff":
        invocation = (*query_args, str(old_graph), str(graph), "--json")
    else:
        invocation = (*query_args, "--graph", str(graph), "--root", str(tmp_path), "--json")

    assert cli.main(["query", *invocation]) == 0
    assert capsys.readouterr().out == EXPECTED[query_name]
