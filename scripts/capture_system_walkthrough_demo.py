#!/usr/bin/env python3
"""Capture a checked-in system walkthrough artifact for documentation."""

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
COMPARISON_ARTIFACT = ROOT / "examples" / "system-walkthrough" / "minotaur-comparison.html"
COMPARISON_OUTPUT = ROOT / "docs" / "assets" / "system-comparison-demo.png"
VIEWPORT = {"width": 1440, "height": 900}


def main(argv: Sequence[str] | None = None) -> int:
    """Screenshot the current explorer, or the historical comparison report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comparison",
        action="store_true",
        help="capture the historical comparison report instead of the explorer",
    )
    parser.add_argument("--artifact", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args(argv)
    if arguments.comparison:
        default_artifact, default_output = COMPARISON_ARTIFACT, COMPARISON_OUTPUT
    else:
        default_artifact, default_output = DEFAULT_ARTIFACT, DEFAULT_OUTPUT
    artifact = (arguments.artifact or default_artifact).resolve()
    output = (arguments.output or default_output).resolve()
    if not artifact.is_file():
        parser.error(f"HTML artifact does not exist: {artifact}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        page.goto(artifact.as_uri(), wait_until="load")
        page.wait_for_function("() => window.minotaurVisualizer?.cy")
        page.locator("#theme-mode").select_option("light")
        if arguments.comparison:
            _focus_comparison(page)
        else:
            _focus_explorer(page)
        page.mouse.move(VIEWPORT["width"] - 10, VIEWPORT["height"] - 10)
        page.screenshot(path=str(output))
        browser.close()
    return 0


def _focus_explorer(page: object) -> None:
    """Focus ``orders``, reveal its boundary, and settle the layout."""
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


def _focus_comparison(page: object) -> None:
    """Show the combined diff and one changed boundary call with its source.

    The details panel's ``Source revision`` switch is a details-only control:
    it changes which captured revision's excerpt is shown without moving the
    graph. Selecting a changed call edge demonstrates it on the saved report.
    """
    page.locator("#system-filter").select_option("orders")
    page.locator("#cross-system-connections").check()
    page.wait_for_function(
        """() => {
            const cy = window.minotaurVisualizer.cy;
            return cy.nodes('.system-container').length >= 2
                && !cy.animated()
                && cy.elements(':animated').empty();
        }"""
    )
    page.evaluate(
        """() => {
            const cy = window.minotaurVisualizer.cy;
            const isCall = function (edge) {
                const record = edge.data('comparison_record');
                return record && record.kind === 'calls';
            };
            const boundary = cy.edges().filter(function (edge) {
                return isCall(edge)
                    && edge.source().data('label') === 'shop.orders.cancel_order'
                    && edge.target().data('label') === 'shop.billing.refund';
            });
            const added = cy.edges().filter(function (edge) {
                return isCall(edge) && edge.data('comparison_record').status === 'added';
            });
            const changed = cy.edges().filter(function (edge) {
                return isCall(edge) && edge.data('comparison_record').status !== 'unchanged';
            });
            const pick = boundary.length ? boundary : (added.length ? added : changed);
            if (pick.length) pick[0].emit('tap');
        }"""
    )
    page.wait_for_timeout(400)


if __name__ == "__main__":
    raise SystemExit(main())
