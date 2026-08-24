"""Behavioral coverage for the graph-loading benchmark utility."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "scripts" / "benchmark_graph_load.py"
EXAMPLE_GRAPH = ROOT / "examples" / "python-workflow" / "minotaur-graph.json"


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


def _run_benchmark_against_checked_in_example(
    tmp_path: Path, *, symbol: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    temp_root = tmp_path / "temporary-directories"
    temp_root.mkdir(parents=True)
    environment = os.environ.copy()
    environment["TMPDIR"] = str(temp_root)
    command = [
        sys.executable,
        str(BENCHMARK),
        "--graph",
        str(EXAMPLE_GRAPH),
        "--root",
        str(ROOT),
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
    return completed, temp_root


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


def test_benchmark_query_guard_uses_checked_in_example_graph(tmp_path: Path) -> None:
    matching, matching_temp_root = _run_benchmark_against_checked_in_example(tmp_path / "main")
    assert matching.returncode == 0, matching.stderr
    assert list(matching_temp_root.iterdir()) == []

    missing, missing_temp_root = _run_benchmark_against_checked_in_example(
        tmp_path / "missing", symbol="definitely_missing"
    )
    assert missing.returncode != 0
    assert "definitely_missing" in missing.stderr
    assert list(missing_temp_root.iterdir()) == []


def test_benchmark_accepts_explicit_matching_symbol(tmp_path: Path) -> None:
    completed, _graph, _old_graph, _old_sidecar, _temp_root = _run_benchmark(
        tmp_path,
        "def main():\n    return 1\n",
        symbol="main",
    )

    assert completed.returncode == 0, completed.stderr


def test_benchmark_has_no_dead_query_symbol_scanner() -> None:
    scripts = ROOT / "scripts"
    assert not any(
        "_find_query_symbol" in path.read_text(encoding="utf-8")
        for path in scripts.rglob("*")
        if path.is_file()
    )
