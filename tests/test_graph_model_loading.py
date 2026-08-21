"""Behavioral coverage for the graph-loading boundary.

AC-12: ``LoadedGraph.canonical`` is a lazy cached property.  The query path
never accesses it, so monkeypatching ``canonicalize`` to raise does not
affect ``query`` on a clean or ``--no-refresh`` graph, but ``visualize``
(which reads ``.canonical``) propagates the error.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_bytes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, path: str, source: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def _analyze(root: Path, graph_path: Path, *targets: Path) -> None:
    """Run ``analyze`` to produce a graph JSON at *graph_path*."""
    args = [
        "analyze",
        "--root",
        str(root),
        "--output",
        str(graph_path),
        "--force",
        *(str(t) for t in targets),
    ]
    status = cli.main(args)
    assert status == 0, f"analyze failed with exit {status}"


def _symbol_name(graph_path: Path) -> str:
    """Return the label of the first symbol node in the graph."""
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    for node in graph["nodes"]:
        if node["node_class"] == "symbol":
            return node["label"]
    raise AssertionError("graph has no symbol nodes")


# ---------------------------------------------------------------------------
# AC-12 proof
# ---------------------------------------------------------------------------


class TestLazyCanonical:
    """Prove that ``LoadedGraph.canonical`` is computed lazily (AC-12)."""

    @pytest.fixture()
    def workspace(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """Analyze a minimal source tree and return (root, graph_path, symbol)."""
        root = tmp_path / "repo"
        root.mkdir()
        source = _write(root, "mod.py", "def helper():\n    return 1\n")
        graph_path = tmp_path / "graph.json"
        _analyze(root, graph_path, source)
        symbol = _symbol_name(graph_path)
        return root, graph_path, symbol

    def test_clean_graph_query_does_not_canonicalize(
        self,
        workspace: tuple[Path, Path, str],
        capsys: object,
    ) -> None:
        """A query on a clean graph exits 0 even when canonicalize would raise."""
        root, graph_path, symbol = workspace
        with patch(
            "minotaur.graph_model.loading.canonicalize",
            side_effect=AssertionError("canonicalize must not be called"),
        ):
            status = cli.main(
                [
                    "query",
                    "definitions",
                    symbol,
                    "--graph",
                    str(graph_path),
                    "--root",
                    str(root),
                ]
            )
        assert status == 0

    def test_no_refresh_query_does_not_canonicalize(
        self,
        workspace: tuple[Path, Path, str],
        capsys: object,
    ) -> None:
        """A --no-refresh query on a drifted graph exits 0 without canonicalize."""
        root, graph_path, symbol = workspace
        # Introduce drift: modify the source after analysis.
        time.sleep(0.05)  # ensure mtime advances
        _write(root, "mod.py", "def helper():\n    return 2\n")
        with patch(
            "minotaur.graph_model.loading.canonicalize",
            side_effect=AssertionError("canonicalize must not be called"),
        ):
            status = cli.main(
                [
                    "query",
                    "definitions",
                    symbol,
                    "--graph",
                    str(graph_path),
                    "--root",
                    str(root),
                    "--no-refresh",
                ]
            )
        # Exit 1 is acceptable here: it means "answered, but graph is stale".
        assert status in (0, 1)

    def test_visualize_accesses_canonical(
        self,
        workspace: tuple[Path, Path, str],
    ) -> None:
        """``visualize`` reads ``.canonical``, so a poisoned canonicalize propagates."""
        _root, graph_path, _symbol = workspace
        html_path = graph_path.with_suffix(".html")
        with (
            patch(
                "minotaur.graph_model.loading.canonicalize",
                side_effect=AssertionError("canonicalize called"),
            ),
            pytest.raises(AssertionError, match="canonicalize called"),
        ):
            cli.main(
                [
                    "visualize",
                    "--input",
                    str(graph_path),
                    "--output",
                    str(html_path),
                    "--force",
                ]
            )

    def test_canonical_value_matches_eager_computation(self) -> None:
        """The cached property returns the same value as an eager call would."""
        from minotaur.graph_model.serialization import canonicalize

        path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
        content = path.read_bytes()
        loaded = load_graph_bytes(content)
        expected = canonicalize(loaded.document)
        assert loaded.canonical == expected

    def test_cached_property_is_computed_once(self) -> None:
        """Repeated reads return the same object without recomputing."""
        path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
        loaded = load_graph_bytes(path.read_bytes())
        first = loaded.canonical
        second = loaded.canonical
        assert first is second

    def test_dataclass_eq_compares_document_only(self) -> None:
        """Dropping ``canonical`` from fields means equality uses ``document`` only."""
        path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
        content = path.read_bytes()
        a = load_graph_bytes(content)
        b = load_graph_bytes(content)
        # Both have the same document but independent cached-property state.
        assert a == b
        # Access canonical on only one side — equality still holds.
        _ = a.canonical
        assert a == b
