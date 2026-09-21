#!/usr/bin/env python3
"""Capture the checked-in system walkthrough explorer for documentation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError as error:  # pragma: no cover - depends on optional extra
    raise SystemExit(
        "The screenshot generator requires the visualizer extra. "
        "Install it with: pip install -e '.[visualizer]'"
    ) from error


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = ROOT / "examples" / "system-walkthrough" / "minotaur-graph.html"
DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "system-walkthrough-demo.png"
VIEWPORT = {"width": 1440, "height": 900}


def main(argv: Sequence[str] | None = None) -> int:
    """Focus the orders system, reveal its boundary, and capture the explorer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args(argv)
    artifact = arguments.artifact.resolve()
    output = arguments.output.resolve()
    if not artifact.is_file():
        parser.error(f"HTML artifact does not exist: {artifact}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        page.goto(artifact.as_uri(), wait_until="load")
        page.wait_for_function("() => window.minotaurVisualizer?.cy")
        page.locator("#theme-mode").select_option("light")
        page.locator("#system-filter").select_option("orders")
        page.locator("#btn-direction").click()
        page.locator("#cross-system-connections").check()
        page.wait_for_function(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                return cy.nodes('.system-container').length === 3
                    && !cy.animated()
                    && cy.elements(':animated').empty();
            }"""
        )
        page.evaluate(
            """() => {
                const edge = window.minotaurVisualizer.cy
                    .edges(':visible').filter('.cross-system')[0];
                if (!edge) throw new Error('expected a visible cross-system edge');
                edge.emit('tap');
            }"""
        )
        page.wait_for_function("() => !document.querySelector('#detail-content .empty-state')")
        page.mouse.move(VIEWPORT["width"] - 10, VIEWPORT["height"] - 10)
        page.screenshot(path=str(output))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
