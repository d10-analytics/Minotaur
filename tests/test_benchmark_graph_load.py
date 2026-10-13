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


def test_components_sum_owner_excludes_the_untrusted_validate_row() -> None:
    """Drive the sum owner with synthetic medians, including the untrusted row.

    The owner, not a pinned excluded-name constant, must drop the untrusted
    ``validate_document`` row: the trusted query path never pays that cost.
    """
    module = _load_benchmark_module()

    components = {
        "utf-8 decode": [1.0],
        "sidecar_read+digest": [2.0],
        "orjson.loads": [4.0],
        "GraphDocument.from_dict": [8.0],
        "validate_document (trusted)": [16.0],
        "validate_document (untrusted)": [1000.0],
        "drift (--no-refresh)": [32.0],
        "GraphIndex.build": [64.0],
    }

    # 1+2+4+8+16+32+64 = 127; the untrusted row's 1000 must not be counted.
    assert module._components_sum(components) == pytest.approx(127.0)


def _reported_components_sum(table: str) -> float:
    """Return the median printed on the ``components sum`` row."""
    for line in table.splitlines():
        fields = line.split()
        if fields[:2] == ["components", "sum"]:
            return float(fields[2].removesuffix("s"))
    raise AssertionError("benchmark output has no 'components sum' row")


def test_main_reports_the_components_sum_owner_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main()`` must report the value returned by its components-sum owner."""
    module = _load_benchmark_module()

    graph = tmp_path / "minotaur-graph.json"
    graph.write_bytes(b"unused\n")
    root = tmp_path / "source"
    root.mkdir()

    components = {
        "utf-8 decode": [1.0],
        "sidecar_read+digest": [2.0],
        "orjson.loads": [4.0],
        "GraphDocument.from_dict": [8.0],
        "validate_document (trusted)": [16.0],
        "validate_document (untrusted)": [1000.0],
        "drift (--no-refresh)": [32.0],
        "GraphIndex.build": [64.0],
    }

    monkeypatch.setattr(module, "_benchmark_analyze", lambda *args, **kwargs: [1.0])
    monkeypatch.setattr(module, "_benchmark_query", lambda *args, **kwargs: [1.0])
    monkeypatch.setattr(module, "_benchmark_components", lambda *args, **kwargs: components)
    monkeypatch.setattr(module, "_benchmark_serialize", lambda *args, **kwargs: ([1.0], "0" * 64))

    owner_calls: list[float] = []
    real_owner = module._components_sum

    def _spy(cmps: dict[str, list[float]]) -> float:
        value = real_owner(cmps)
        owner_calls.append(value)
        return value

    monkeypatch.setattr(module, "_components_sum", _spy)

    assert module.main(["--graph", str(graph), "--root", str(root), "--repeats", "1"]) == 0

    assert owner_calls == [pytest.approx(127.0)]
    assert _reported_components_sum(capsys.readouterr().out) == pytest.approx(127.0)


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
