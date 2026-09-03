#!/usr/bin/env python3
"""Regenerate the checked-in system-walkthrough example artifacts.

Run this script from the repository root. It reproduces
``examples/system-walkthrough/minotaur-graph.json`` and its trusted-load
stamp ``minotaur-graph.json.sha256`` through the same public ``analyze`` and
``query`` commands the walkthrough documents, so an artifact difference
catches drift in either command. The checked-in graph omits volatile Git
snapshot metadata so regeneration stays byte-for-byte reproducible across
commits; normal ``analyze`` output retains that metadata.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "system-walkthrough"
GRAPH = EXAMPLE / "minotaur-graph.json"


def main() -> int:
    """Write the canonical example graph and re-stamp its sidecar."""
    _run_cli(
        "analyze",
        "--root",
        "examples/system-walkthrough",
        "--output",
        str(GRAPH),
        "--force",
        "examples/system-walkthrough/shop",
    )
    _remove_volatile_snapshot_metadata()
    # A graph-reading command with --validate force-validates the rewritten
    # graph and writes the matching trusted-load stamp, keeping the committed
    # sidecar in sync with the metadata-free bytes.
    _run_cli(
        "query",
        "consumers",
        "orders",
        "--graph",
        str(GRAPH),
        "--root",
        "examples/system-walkthrough",
        "--no-refresh",
        "--validate",
    )
    return 0


def _remove_volatile_snapshot_metadata() -> None:
    """Keep the distributable example stable while retaining normal Git output."""
    document = json.loads(GRAPH.read_text(encoding="utf-8"))
    document.pop("source_control", None)
    GRAPH.write_bytes(
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
