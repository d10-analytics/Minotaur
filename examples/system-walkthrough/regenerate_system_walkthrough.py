#!/usr/bin/env python3
"""Regenerate the checked-in system-walkthrough example artifacts.

Run this script from the repository root. It reproduces the graph, trusted-load
stamp, system-aware HTML explorer, and historical comparison report through the
same public commands the walkthrough documents. The checked-in graph omits
volatile Git snapshot metadata so regeneration stays byte-for-byte reproducible
across commits; normal ``analyze`` output retains that metadata.

The comparison report is generated from a disposable temporary Git repository
whose commit identity and timestamps are fixed, so its two revision IDs and
rendered facts stay stable. The real checkout is only read, and the temporary
repository is removed when the script finishes.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "system-walkthrough"
CONFIG = EXAMPLE / ".minotaur.toml"
COMPARISON_NAME = "minotaur-comparison.html"


def main(argv: Sequence[str] | None = None) -> int:
    """Write the canonical example graph, sidecar, HTML explorer, and report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=EXAMPLE,
        help="directory that receives the graph, sidecar, HTML explorer, and report",
    )
    parser.add_argument(
        "--skip-comparison",
        action="store_true",
        help="only rebuild the graph, sidecar, and explorer",
    )
    parser.add_argument(
        "--comparison-only",
        action="store_true",
        help="only rebuild the historical comparison report",
    )
    arguments = parser.parse_args(argv)
    output_directory = arguments.output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    graph = output_directory / "minotaur-graph.json"
    html = output_directory / "minotaur-graph.html"
    if arguments.comparison_only and arguments.skip_comparison:
        parser.error("--comparison-only and --skip-comparison are mutually exclusive")
    if not arguments.comparison_only:
        _regenerate_explorer(graph, html)
    if not arguments.skip_comparison:
        _regenerate_comparison(output_directory)
    return 0


def _regenerate_explorer(graph: Path, html: Path) -> None:
    """Rebuild the graph, trusted-load sidecar, and system HTML explorer."""
    _run_cli(
        "analyze",
        "--config",
        str(CONFIG),
        "--output",
        str(graph),
        "--force",
    )
    _remove_volatile_snapshot_metadata(graph)
    # A graph-reading command with --validate force-validates the rewritten
    # graph and writes the matching trusted-load stamp, keeping the committed
    # sidecar in sync with the metadata-free bytes.
    _run_cli(
        "query",
        "consumers",
        "orders",
        "--config",
        str(CONFIG),
        "--graph",
        str(graph),
        "--no-refresh",
        "--validate",
    )
    _run_cli(
        "visualize",
        "--config",
        str(CONFIG),
        "--input",
        str(graph),
        "--source-root",
        str(EXAMPLE),
        "--output",
        str(html),
        "--force",
    )


def _regenerate_comparison(output_directory: Path) -> None:
    """Rebuild the offline comparison report from fixed-identity source history."""
    runner = _load_example_runner()
    with tempfile.TemporaryDirectory(prefix="minotaur-system-comparison-") as directory:
        fixture = Path(directory)
        runner.create_systems_baseline(fixture)
        runner.stage_systems_revision(fixture)
        runner.commit_systems_revision(fixture)
        report = output_directory / COMPARISON_NAME
        if report.exists():
            report.unlink()
        # A comparison that finds changes exits 1; that is success here.
        _run_cli(
            "query",
            "diff",
            "--systems",
            runner.SYSTEMS_BASELINE_TAG,
            runner.SYSTEMS_REVISION_TAG,
            "--html",
            str(report),
            cwd=fixture,
            expected=1,
        )


def _load_example_runner() -> ModuleType:
    """Import the example runner that owns the deterministic fixture."""
    location = ROOT / "examples" / "run_walkthrough.py"
    specification = importlib.util.spec_from_file_location("minotaur_run_walkthrough", location)
    if specification is None or specification.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load the example runner from {location}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _remove_volatile_snapshot_metadata(graph: Path) -> None:
    """Keep the distributable example stable while retaining normal Git output."""
    document = json.loads(graph.read_text(encoding="utf-8"))
    document.pop("source_control", None)
    graph.write_bytes(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _run_cli(*arguments: str, cwd: Path = ROOT, expected: int = 0) -> None:
    """Run one documented CLI command, allowing the expected changed status."""
    completed = subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
    )
    if completed.returncode != expected:
        raise subprocess.CalledProcessError(
            completed.returncode, completed.args, completed.stdout, completed.stderr
        )


if __name__ == "__main__":
    raise SystemExit(main())
