#!/usr/bin/env python3
"""Benchmark Minotaur graph loading, querying, and analysis.

Produces a fixed-width table of wall-clock measurements for:
  1. ``minotaur analyze --root <root> .`` end-to-end (AC-09), run against a
     temporary output path so the caller-supplied ``--graph`` file is never
     touched.
  2. ``minotaur query definitions <symbol> --no-refresh`` end-to-end (AC-05),
     run against that same temporary graph (analyze already wrote its
     sidecar, so this query takes the trusted, schema-skipping path — see
     ``_benchmark_query``).
  3. In-process component breakdown of one ``load_graph_file`` call:
     UTF-8 decode, sidecar read + ``graph_digest``, ``orjson.loads``,
     ``GraphDocument.from_dict`` (AC-14), ``validate_document`` (AC-13),
     ``drift()`` under the same ``--no-refresh`` sequence the query path
     uses, and the ``GraphIndex.build`` the query path performs.
  4. ``serialize`` on the loaded document with SHA-256 of output (AC-10)

The script never writes to the caller-supplied ``--graph`` path: analyze and
the component/serialize benchmarks all run against a graph in an
invocation-owned temporary directory, which is removed (along with every
artifact created there) before the script exits.

Run from any directory with the .venv's Python:

    .venv/bin/python scripts/benchmark_graph_load.py \\
        --graph /path/to/minotaur-graph.json \\
        --root /path/to/source/root
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

import orjson


class _SubprocessError(Exception):
    """Raised when a benchmarked subprocess exits non-zero."""


def _time_subprocess(
    args: list[str], cwd: Path, repeats: int, *, query_symbol: str | None = None
) -> list[float]:
    """Run *args* as a subprocess *repeats* times, returning wall-clock seconds."""
    times: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = subprocess.run(args, cwd=cwd, capture_output=True)
        elapsed = time.perf_counter() - start
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[:500]
            raise _SubprocessError(
                f"Command failed (exit {result.returncode}):\n"
                f"  {' '.join(args)}\n"
                f"  stderr: {stderr}\n"
            )
        if query_symbol is not None and result.stdout in (b"", b"no definitions\n"):
            raise _SubprocessError(
                f"Query for symbol {query_symbol!r} matched no definitions:\n  {' '.join(args)}\n"
            )
        times.append(elapsed)
    return times


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
    return _time_subprocess(args, cwd=root, repeats=repeats, query_symbol=symbol)


def _benchmark_components(graph_path: Path, root: Path, repeats: int) -> dict[str, list[float]]:
    """Time in-process loading components individually.

    Mirrors the steps ``cli._load_and_refresh_graph`` and ``_run_graph_query``
    perform for a ``--no-refresh`` query, so this table's rows can be summed
    and compared against the end-to-end query row above.
    """
    from minotaur.graph_model.document import GraphDocument
    from minotaur.graph_model.loading import graph_digest, stamp_path
    from minotaur.graph_model.validation import validate_document
    from minotaur.query.freshness import drift
    from minotaur.query.index import GraphIndex

    raw_bytes = graph_path.read_bytes()
    components: dict[str, list[float]] = {
        "utf-8 decode": [],
        "sidecar_read+digest": [],
        "orjson.loads": [],
        "GraphDocument.from_dict": [],
        "validate_document": [],
        "drift (--no-refresh)": [],
        "GraphIndex.build": [],
    }

    for _ in range(repeats):
        # UTF-8 decode
        start = time.perf_counter()
        decoded = raw_bytes.decode("utf-8")
        components["utf-8 decode"].append(time.perf_counter() - start)

        # Sidecar read + digest
        start = time.perf_counter()
        sp = stamp_path(graph_path)
        try:
            with sp.open("rb") as fh:
                _stamp = fh.read(4096).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            _stamp = ""
        _digest = graph_digest(raw_bytes)
        components["sidecar_read+digest"].append(time.perf_counter() - start)

        # orjson.loads
        start = time.perf_counter()
        raw = orjson.loads(decoded)
        components["orjson.loads"].append(time.perf_counter() - start)

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

        # drift(), as performed by _load_and_refresh_graph under --no-refresh
        start = time.perf_counter()
        drift(document, root)
        components["drift (--no-refresh)"].append(time.perf_counter() - start)

        # GraphIndex.build, as performed by _run_graph_query
        start = time.perf_counter()
        GraphIndex.build(document)
        components["GraphIndex.build"].append(time.perf_counter() - start)

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


def _format_table(
    rows: list[tuple[str, float, float, float]], serialize_sha: str, verbose: bool
) -> str:
    """Format a fixed-width result table.

    Median is the only column shown by default; ``--verbose`` adds Min/Max.
    """
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 72)
    lines.append("  Minotaur Benchmark Results")
    lines.append("=" * 72)
    lines.append("")
    if verbose:
        lines.append(f"  {'Measurement':<36} {'Median':>8} {'Min':>8} {'Max':>8}")
        lines.append(f"  {'-' * 36} {'-' * 8} {'-' * 8} {'-' * 8}")
        for label, median, lo, hi in rows:
            lines.append(f"  {label:<36} {median:>7.3f}s {lo:>7.3f}s {hi:>7.3f}s")
    else:
        lines.append(f"  {'Measurement':<36} {'Median':>8}")
        lines.append(f"  {'-' * 36} {'-' * 8}")
        for label, median, _lo, _hi in rows:
            lines.append(f"  {label:<36} {median:>7.3f}s")
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
        help="path to minotaur-graph.json (never modified; a temporary copy is benchmarked)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="repository root for analyze and query commands",
    )
    parser.add_argument(
        "--symbol",
        default="main",
        help="symbol to query in the definitions measurement (default: main)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="number of repetitions per measurement (default: 3, must be >= 1)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show Min/Max columns in addition to Median",
    )
    arguments = parser.parse_args(argv)

    if arguments.repeats < 1:
        parser.error("--repeats must be >= 1")

    graph_path = arguments.graph.resolve()
    root = arguments.root.resolve()
    repeats = arguments.repeats
    verbose = arguments.verbose
    symbol = arguments.symbol
    python = sys.executable

    if not graph_path.is_file():
        sys.stderr.write(f"Error: graph file not found: {graph_path}\n")
        return 1
    if not root.is_dir():
        sys.stderr.write(f"Error: root directory not found: {root}\n")
        return 1

    # M-1: never write the caller's --graph path. TemporaryDirectory creates a
    # unique, invocation-owned location so concurrent runs and pre-existing
    # sibling files cannot collide. Its context removes the graph, sidecar,
    # and any other artifacts produced there on every exit path.
    with tempfile.TemporaryDirectory(prefix="minotaur-benchmark-") as temp_directory:
        temp_graph_path = Path(temp_directory) / graph_path.name

        sys.stderr.write(f"Graph:        {graph_path}\n")
        sys.stderr.write(f"Temp graph:   {temp_graph_path}\n")
        sys.stderr.write(f"Root:         {root}\n")
        sys.stderr.write(f"Repeats:      {repeats}\n")
        sys.stderr.write(f"Python:       {python}\n\n")

        try:
            rows: list[tuple[str, float, float, float]] = []

            # 1. analyze (AC-09), into the temporary path only.
            sys.stderr.write("Benchmarking: analyze ...\n")
            analyze_times = _benchmark_analyze(python, root, temp_graph_path, repeats)
            rows.append(
                (
                    "analyze --root . --force .",
                    _median(analyze_times),
                    min(analyze_times),
                    max(analyze_times),
                )
            )

            # 2. query definitions (AC-05), against the freshly analyzed temp graph.
            sys.stderr.write(f"Benchmarking: query definitions {symbol} ...\n")
            query_times = _benchmark_query(python, temp_graph_path, root, symbol, repeats)
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
            components = _benchmark_components(temp_graph_path, root, repeats)
            for name, times in components.items():
                rows.append((name, _median(times), min(times), max(times)))
            components_sum = sum(_median(times) for times in components.values())
            rows.append(("components sum", components_sum, components_sum, components_sum))

            # 4. serialize (AC-10)
            sys.stderr.write("Benchmarking: serialize ...\n")
            serialize_times, serialize_sha = _benchmark_serialize(temp_graph_path, repeats)
            rows.append(
                ("serialize", _median(serialize_times), min(serialize_times), max(serialize_times))
            )

            # Print results
            table = _format_table(rows, serialize_sha, verbose)
            print(table)
        except _SubprocessError as error:
            sys.stderr.write(str(error))
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
