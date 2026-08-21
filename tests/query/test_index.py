"""Unit coverage for the single symbol-resolution point on ``GraphIndex``."""

from __future__ import annotations

from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_file
from minotaur.query.index import AmbiguousSymbol, GraphIndex, UnknownSymbol


def _index(root: Path, sources: dict[str, str]) -> GraphIndex:
    for relative, content in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    graph = root / "graph.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(graph), str(root)]) == 0
    return GraphIndex.build(load_graph_file(graph).document)


def test_resolve_returns_the_only_matching_symbol(tmp_path: Path) -> None:
    index = _index(tmp_path, {"mod.py": "def target():\n    pass\n"})
    assert index.resolve("mod.target").label == "mod.target"


def test_resolve_reports_unknown_labels_with_suggestions(tmp_path: Path) -> None:
    index = _index(tmp_path, {"mod.py": "def target():\n    pass\n"})
    with pytest.raises(UnknownSymbol) as excinfo:
        index.resolve("mod.targte")
    assert "mod.target" in excinfo.value.suggestions
    assert str(excinfo.value).startswith("unknown symbol: mod.targte; nearest labels: mod.target")


def test_resolve_reports_every_candidate_site_for_a_duplicate_label(tmp_path: Path) -> None:
    """Candidates are ordered by file then line, so the list is reproducible."""
    index = _index(
        tmp_path,
        {"mod.py": "def dup():\n    return 1\n\n\n" + "\n" * 8 + "def dup():\n    return 2\n"},
    )
    with pytest.raises(AmbiguousSymbol) as excinfo:
        index.resolve("mod.dup")
    assert excinfo.value.candidates == ("mod.py:1", "mod.py:13")
    assert str(excinfo.value) == ("ambiguous symbol: mod.dup; candidates: mod.py:1, mod.py:13")
