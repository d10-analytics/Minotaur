#!/usr/bin/env python3
"""Capture the checked-in Python workflow explorer for the root README.

Install the ``visualizer`` extra and Chromium first:

    python3 -m playwright install chromium

The script intentionally opens the checked-in ``file://`` artifact, so the
preview is refreshed from exactly the HTML that users can download or explore
on GitHub Pages.
"""

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
DEFAULT_ARTIFACT = ROOT / "examples" / "python-workflow" / "minotaur-graph.html"
DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "python-workflow-demo.png"
VIEWPORT = {"width": 1440, "height": 900}
DETAIL_WIDTH = VIEWPORT["width"] // 4


def main(argv: Sequence[str] | None = None) -> int:
    """Select a calls edge with source evidence and capture a fixed viewport."""
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
        # Tap the edge through Cytoscape rather than at its rendered midpoint:
        # the layout packs edges closely enough that a screen click can land
        # on a neighbouring edge whenever the analyzed module gains or loses
        # a relationship, which made this capture fragile across regenerations.
        page.evaluate(
            """() => {
                const selected = window.minotaurVisualizer.cy.edges().filter(
                    (edge) => edge.data('kind') === 'calls'
                )[0];
                if (!selected) throw new Error('expected a calls edge');
                selected.emit('tap');
            }"""
        )
        page.wait_for_selector("#call-site-select")
        page.wait_for_selector(".call-site-highlight")
        detail = page.locator("#detail").bounding_box()
        handle = page.locator("#detail-resize").bounding_box()
        assert detail is not None and handle is not None
        page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + 40)
        page.mouse.down()
        page.mouse.move(
            handle["x"] + handle["width"] / 2 + DETAIL_WIDTH - detail["width"],
            handle["y"] + 40,
        )
        page.mouse.up()
        page.wait_for_function(
            "(width) => document.querySelector('#detail').getBoundingClientRect().width === width",
            arg=DETAIL_WIDTH,
        )
        # The complete graph's fit-to-window scale suppresses Cytoscape labels.
        # A selected call can span distant layout ranks, so anchor a closer view
        # on its caller rather than centering both endpoints off-screen.
        page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const selected = cy.edges().filter((edge) => edge.data('kind') === 'calls')[0];
                cy.zoom({
                    level: 2.5,
                    position: selected.source().position(),
                    renderedPosition: { x: cy.width() * 0.42, y: cy.height() * 0.56 },
                });
            }"""
        )
        page.wait_for_timeout(100)
        page.mouse.move(VIEWPORT["width"] - 10, VIEWPORT["height"] - 10)
        page.screenshot(path=str(output))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
