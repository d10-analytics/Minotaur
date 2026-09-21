#!/usr/bin/env python3
"""Regenerate the checked-in system-walkthrough example artifacts.

Run this script from the repository root. It reproduces the graph, trusted-load
stamp, and system-aware HTML explorer through the same public commands the
walkthrough documents. The checked-in graph omits volatile Git snapshot
metadata so regeneration stays byte-for-byte reproducible across commits;
normal ``analyze`` output retains that metadata.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "system-walkthrough"
CONFIG = EXAMPLE / ".minotaur.toml"


def main(argv: Sequence[str] | None = None) -> int:
    """Write the canonical example graph, sidecar, and HTML explorer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=EXAMPLE,
        help="directory that receives the graph, sidecar, and HTML explorer",
    )
    output_directory = parser.parse_args(argv).output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    graph = output_directory / "minotaur-graph.json"
    html = output_directory / "minotaur-graph.html"
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
    return 0


def _remove_volatile_snapshot_metadata(graph: Path) -> None:
    """Keep the distributable example stable while retaining normal Git output."""
    document = json.loads(graph.read_text(encoding="utf-8"))
    document.pop("source_control", None)
    graph.write_bytes(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _run_cli(*arguments: str) -> None:
    """Run one documented CLI command from the repository root."""
    subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
