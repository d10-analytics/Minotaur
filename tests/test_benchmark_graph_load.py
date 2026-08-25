"""Behavioral coverage for the graph-loading benchmark utility."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "scripts" / "benchmark_graph_load.py"


def _run_benchmark(
    tmp_path: Path, source: str, *, symbol: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "app.py").write_text(source, encoding="utf-8")

    graph = tmp_path / "minotaur-graph.json"
    graph.write_bytes(b"caller-owned graph\n")
    old_temp_graph = graph.with_name(graph.name + ".bench.json")
    old_temp_sidecar = old_temp_graph.with_name(old_temp_graph.name + ".sha256")
    old_temp_graph.write_bytes(b"caller-owned old benchmark graph\n")
    old_temp_sidecar.write_bytes(b"caller-owned old benchmark sidecar\n")

    temp_root = tmp_path / "temporary-directories"
    temp_root.mkdir(parents=True)
    environment = os.environ.copy()
    environment["TMPDIR"] = str(temp_root)
    command = [
        sys.executable,
        str(BENCHMARK),
        "--graph",
        str(graph),
        "--root",
        str(source_root),
        "--repeats",
        "1",
    ]
    if symbol is not None:
        command.extend(["--symbol", symbol])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed, graph, old_temp_graph, old_temp_sidecar, temp_root


@pytest.mark.parametrize(
    ("source", "expected_returncode"),
    [
        ("def main():\n    return 1\n", 0),
        ("def broken(:\n", 1),
    ],
    ids=["success", "analyze-failure"],
)
def test_benchmark_owns_temporary_artifacts_without_touching_siblings(
    tmp_path: Path, source: str, expected_returncode: int
) -> None:
    completed, graph, old_temp_graph, old_temp_sidecar, temp_root = _run_benchmark(tmp_path, source)

    assert completed.returncode == expected_returncode, completed.stderr
    assert graph.read_bytes() == b"caller-owned graph\n"
    assert old_temp_graph.read_bytes() == b"caller-owned old benchmark graph\n"
    assert old_temp_sidecar.read_bytes() == b"caller-owned old benchmark sidecar\n"
    assert list(temp_root.iterdir()) == []


def test_benchmark_rejects_query_that_matches_nothing(tmp_path: Path) -> None:
    completed, _graph, _old_graph, _old_sidecar, _temp_root = _run_benchmark(
        tmp_path,
        "def main():\n    return 1\n",
        symbol="definitely_missing",
    )

    assert completed.returncode != 0
    assert "definitely_missing" in completed.stderr


@pytest.mark.parametrize(
    "symbol",
    [None, "main", "definitely_missing"],
    ids=["default-main", "explicit-main", "missing"],
)
def test_benchmark_query_symbol_guard_and_artifact_ownership(
    tmp_path: Path, symbol: str | None
) -> None:
    completed, graph, old_temp_graph, old_temp_sidecar, temp_root = _run_benchmark(
        tmp_path,
        "def main():\n    return 1\n",
        symbol=symbol,
    )

    expected_returncode = 1 if symbol == "definitely_missing" else 0
    assert completed.returncode == expected_returncode, completed.stderr
    if symbol is None:
        assert "query definitions main --no-refresh" in completed.stdout
    if symbol == "definitely_missing":
        assert "Query for symbol 'definitely_missing' matched no definitions" in completed.stderr
    assert graph.read_bytes() == b"caller-owned graph\n"
    assert old_temp_graph.read_bytes() == b"caller-owned old benchmark graph\n"
    assert old_temp_sidecar.read_bytes() == b"caller-owned old benchmark sidecar\n"
    assert list(temp_root.iterdir()) == []


def test_benchmark_accepts_explicit_matching_symbol(tmp_path: Path) -> None:
    completed, _graph, _old_graph, _old_sidecar, _temp_root = _run_benchmark(
        tmp_path,
        "def main():\n    return 1\n",
        symbol="main",
    )

    assert completed.returncode == 0, completed.stderr


def test_benchmark_has_no_dead_query_symbol_scanner() -> None:
    scripts = ROOT / "scripts"
    source_files = tuple(scripts.rglob("*.py"))
    assert BENCHMARK in source_files
    assert not any(
        "_find_query_symbol" in path.read_text(encoding="utf-8") for path in source_files
    )


def _load_benchmark_module() -> ModuleType:
    """Import the benchmark script as a module for in-process inspection."""
    spec = importlib.util.spec_from_file_location("benchmark_graph_load", BENCHMARK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_reports_both_trusted_and_untrusted_validate_rows(tmp_path: Path) -> None:
    """H-2: the row labelled trusted must be measured the way a trusted load runs."""
    completed, _graph, _old_graph, _old_sidecar, _temp_root = _run_benchmark(
        tmp_path, "def main():\n    return 1\n"
    )

    assert completed.returncode == 0, completed.stderr
    assert "validate_document (trusted)" in completed.stdout
    assert "validate_document (untrusted)" in completed.stdout


def test_components_sum_excludes_the_untrusted_validate_row() -> None:
    """The sum must mirror what the end-to-end trusted query row actually pays."""
    module = _load_benchmark_module()
    expected = frozenset({"validate_document (untrusted)"})
    assert expected == module._SUM_EXCLUDED_COMPONENTS


def test_trusted_validate_row_passes_verify_node_ids_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record the kwargs each timed ``validate_document`` call is given."""
    module = _load_benchmark_module()

    from minotaur.graph_model import validation
    from minotaur.graph_model.loading import graph_digest, stamp_path

    graph_source = ROOT / "examples/synthetic-graphs/small-workflow.json"
    graph_path = tmp_path / "minotaur-graph.json"
    data = graph_source.read_bytes()
    graph_path.write_bytes(data)
    stamp_path(graph_path).write_text(graph_digest(data) + "\n", encoding="utf-8")
    root = tmp_path / "source"
    root.mkdir()

    calls: list[bool] = []
    real = validation.validate_document

    def _recording(document: object, **kwargs: object) -> object:
        calls.append(bool(kwargs.get("verify_node_ids", True)))
        return real(document, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(validation, "validate_document", _recording)

    components = module._benchmark_components(graph_path, root, 1)

    assert calls == [False, True]
    assert set(components) >= {"validate_document (trusted)", "validate_document (untrusted)"}
