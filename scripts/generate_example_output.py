#!/usr/bin/env python3
"""Regenerate the checked-in end-to-end Python workflow artifacts.

Run this script from any directory.  It deliberately invokes the same public
CLI commands documented in the example rather than importing implementation
functions, so an artifact difference catches drift in either command.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIRECTORY = ROOT / "examples" / "python-workflow"
GRAPH_NAME = "minotaur-graph.json"
HTML_NAME = "minotaur-graph.html"


def main(argv: Sequence[str] | None = None) -> int:
    """Write the canonical graph and its portable HTML visualization."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="directory that receives minotaur-graph.json and minotaur-graph.html",
    )
    arguments = parser.parse_args(argv)
    output_directory = arguments.output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    graph = output_directory / GRAPH_NAME
    html = output_directory / HTML_NAME

    _run_cli(
        "analyze",
        "--root",
        ".",
        "--output",
        str(graph),
        "--force",
        "src/minotaur/language_interpreter/selection.py",
    )
    _run_cli(
        "visualize",
        "--input",
        str(graph),
        "--output",
        str(html),
        "--source-root",
        ".",
        "--force",
    )
    return 0


def _run_cli(*arguments: str) -> None:
    """Run one documented CLI command from the repository root.

    Keeping generation at the public-command boundary makes the checked-in
    example an integration contract, rather than an artifact that can remain
    current only because it calls private helpers differently from users.
    """
    subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
