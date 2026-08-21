#!/usr/bin/env python3
"""Benchmark Minotaur graph loading, querying, and analysis.

Produces a fixed-width table of wall-clock measurements for:
  1. ``minotaur analyze --root <root> .`` end-to-end (AC-09)
  2. ``minotaur query definitions <symbol> --no-refresh`` end-to-end (AC-05)
  3. In-process component breakdown of one ``load_graph_file`` call:
     sidecar read + ``graph_digest``, ``json.loads``, ``GraphDocument.from_dict``
     (AC-14), ``validate_document`` (AC-13)
  4. ``serialize`` on the loaded document with SHA-256 of output (AC-10)

Run from any directory with the .venv's Python:

    .venv/bin/python scripts/benchmark_graph_load.py \\
        --graph /path/to/minotaur-graph.json \\
        --root /path/to/source/root
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path


def _time_subprocess(args: list[str], cwd: Path, repeats: int) -> list[float]:
    """Run *args* as a subprocess *repeats* times, returning wall-clock seconds."""
    times: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = subprocess.run(args, cwd=cwd, capture_output=True)
        elapsed = time.perf_counter() - start
        if result.returncode != 0:
            sys.stderr.write(
                f"Command failed (exit {result.returncode}):\n"
                f"  {' '.join(args)}\n"
                f"  stderr: {result.stderr.decode('utf-8', errors='replace')[:500]}\n"
            )
            sys.exit(1)
        times.append(elapsed)
    return times


def _find_query_symbol(graph_path: Path) -> str:
    """Pick a symbol to query from the graph's nodes.

    Prefers 'main' if present, otherwise uses the first symbol-kind node.
    """
    raw = json.loads(graph_path.read_bytes())
    for node in raw.get("nodes", []):
        identity = node.get("identity", {})
        if identity.get("symbol") == "main":
            return "main"
    # Fall back to any symbol
    for node in raw.get("nodes", []):
        identity = node.get("identity", {})
        symbol = identity.get("symbol")
        if symbol:
            return symbol
    return "main"


def _benchmark_analyze(python: str, root: Path, graph_path: Path, repeats: int) -> list[float]:
    """Time ``minotaur analyze --root <root> --output <graph> .`` as a subprocess."""
    args = [
        python,
        "-m",
        "minotaur",
        "analyze",
        "--root",
        str(root),
        "--output",
        str(graph_path),
        "--force",
        ".",
    ]
    return _time_subprocess(args, cwd=root, repeats=repeats)


def _benchmark_query(
    python: str, graph_path: Path, root: Path, symbol: str, repeats: int
) -> list[float]:
    """Time ``minotaur query definitions <symbol> --no-refresh`` as a subprocess."""
    args = [
        python,
        "-m",
        "minotaur",
        "query",
        "definitions",
        symbol,
        "--graph",
        str(graph_path),
        "--root",
        str(root),
        "--no-refresh",
    ]
    return _time_subprocess(args, cwd=root, repeats=repeats)


def _benchmark_components(graph_path: Path, repeats: int) -> dict[str, list[float]]:
    """Time in-process loading components individually."""
    from minotaur.graph_model.document import GraphDocument
    from minotaur.graph_model.loading import graph_digest, stamp_path
    from minotaur.graph_model.validation import validate_document

    data = graph_path.read_bytes()
    components: dict[str, list[float]] = {
        "sidecar_read+digest": [],
        "json.loads": [],
        "GraphDocument.from_dict": [],
        "validate_document": [],
    }

    for _ in range(repeats):
        # Sidecar read + digest
        start = time.perf_counter()
        sp = stamp_path(graph_path)
        try:
            with sp.open("rb") as fh:
                _stamp = fh.read(4096).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            _stamp = ""
        _digest = graph_digest(data)
        components["sidecar_read+digest"].append(time.perf_counter() - start)

        # json.loads
        decoded = data.decode("utf-8")
        start = time.perf_counter()
        raw = json.loads(decoded)
        components["json.loads"].append(time.perf_counter() - start)

        # GraphDocument.from_dict
        start = time.perf_counter()
        document = GraphDocument.from_dict(raw)
        components["GraphDocument.from_dict"].append(time.perf_counter() - start)

        # validate_document
        start = time.perf_counter()
        report = validate_document(document)
        components["validate_document"].append(time.perf_counter() - start)

        if not report.is_valid:
            sys.stderr.write(f"Warning: graph has {len(report.issues)} validation issues\n")

    return components


def _benchmark_serialize(graph_path: Path, repeats: int) -> tuple[list[float], str]:
    """Time ``serialize`` and return its SHA-256."""
    from minotaur.graph_model.loading import load_graph_file
    from minotaur.graph_model.serialization import serialize

    loaded = load_graph_file(graph_path)
    times: list[float] = []
    output_bytes = b""
    for _ in range(repeats):
        start = time.perf_counter()
        output_bytes = serialize(loaded.document)
        times.append(time.perf_counter() - start)

    sha256 = hashlib.sha256(output_bytes).hexdigest()
    return times, sha256


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _format_table(rows: list[tuple[str, float, float, float]], serialize_sha: str) -> str:
    """Format a fixed-width result table."""
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 72)
    lines.append("  Minotaur Benchmark Results")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"  {'Measurement':<36} {'Median':>8} {'Min':>8} {'Max':>8}")
    lines.append(f"  {'-' * 36} {'-' * 8} {'-' * 8} {'-' * 8}")
    for label, median, lo, hi in rows:
        lines.append(f"  {label:<36} {median:>7.3f}s {lo:>7.3f}s {hi:>7.3f}s")
    lines.append("")
    lines.append(f"  serialize SHA-256: {serialize_sha}")
    lines.append("=" * 72)
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run benchmarks and print a result table."""
    parser = argparse.ArgumentParser(description="Benchmark Minotaur graph loading and analysis.")
    parser.add_argument(
        "--graph",
        type=Path,
        required=True,
        help="path to minotaur-graph.json",
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="repository root for analyze and query commands",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="number of repetitions per measurement (default: 3)",
    )
    arguments = parser.parse_args(argv)

    graph_path = arguments.graph.resolve()
    root = arguments.root.resolve()
    repeats = arguments.repeats
    python = sys.executable

    if not graph_path.is_file():
        sys.stderr.write(f"Error: graph file not found: {graph_path}\n")
        return 1
    if not root.is_dir():
        sys.stderr.write(f"Error: root directory not found: {root}\n")
        return 1

    sys.stderr.write(f"Graph:   {graph_path}\n")
    sys.stderr.write(f"Root:    {root}\n")
    sys.stderr.write(f"Repeats: {repeats}\n")
    sys.stderr.write(f"Python:  {python}\n\n")

    rows: list[tuple[str, float, float, float]] = []

    # 1. analyze (AC-09)
    sys.stderr.write("Benchmarking: analyze ...\n")
    analyze_times = _benchmark_analyze(python, root, graph_path, repeats)
    rows.append(
        (
            "analyze --root . --force .",
            _median(analyze_times),
            min(analyze_times),
            max(analyze_times),
        )
    )

    # 2. query definitions (AC-05)
    symbol = _find_query_symbol(graph_path)
    sys.stderr.write(f"Benchmarking: query definitions {symbol} ...\n")
    query_times = _benchmark_query(python, graph_path, root, symbol, repeats)
    rows.append(
        (
            f"query definitions {symbol} --no-refresh",
            _median(query_times),
            min(query_times),
            max(query_times),
        )
    )

    # 3. In-process components
    sys.stderr.write("Benchmarking: in-process components ...\n")
    components = _benchmark_components(graph_path, repeats)
    for name, times in components.items():
        rows.append((name, _median(times), min(times), max(times)))

    # 4. serialize (AC-10)
    sys.stderr.write("Benchmarking: serialize ...\n")
    serialize_times, serialize_sha = _benchmark_serialize(graph_path, repeats)
    rows.append(("serialize", _median(serialize_times), min(serialize_times), max(serialize_times)))

    # Print results
    table = _format_table(rows, serialize_sha)
    print(table)

    return 0


if __name__ == "__main__":
    sys.exit(main())
