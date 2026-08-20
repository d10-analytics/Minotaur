"""Chromium proof for interactions in a generated ``file://`` artifact."""

from __future__ import annotations

from pathlib import Path

import pytest

from minotaur import cli

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

ROOT = Path(__file__).parents[1]


def _click_visible_edge_and_show_details(page: object) -> dict[str, object]:
    """Exercise the user-facing edge-selection path with real pointer input.

    Cytoscape's event dispatcher can be invoked synthetically, but that bypasses
    hit testing and would not reproduce the original click-to-details failure.
    """
    edge = page.evaluate(
        """() => {
            const cy = window.minotaurVisualizer.cy;
            const selected = cy.edges(':visible')[0];
            if (!selected) throw new Error('expected a visible edge');
            const point = selected.renderedMidpoint();
            const bounds = cy.container().getBoundingClientRect();
            return {
                id: selected.id(),
                x: bounds.left + point.x,
                y: bounds.top + point.y,
                kind: selected.data('kind'),
            };
        }"""
    )
    page.mouse.click(edge["x"], edge["y"])
    page.wait_for_function(
        "(kind) => document.querySelector('#detail-content').innerText.includes(kind)",
        arg=edge["kind"],
    )
    return edge


def _click_connected_node_and_show_details(page: object) -> dict[str, object]:
    """Select a node with a visible connection using the same pointer path as a user.

    Selecting only visible graph elements keeps this helper stable after filter
    interactions and proves the displayed connection list reflects the view a
    user can actually inspect.
    """
    node = page.evaluate(
        """() => {
            const cy = window.minotaurVisualizer.cy;
            const selected = cy.nodes(':visible').filter(
                (node) => node.connectedEdges().filter(':visible').length > 0
            )[0];
            if (!selected) throw new Error('expected a visible connected node');
            const point = selected.renderedPosition();
            const bounds = cy.container().getBoundingClientRect();
            return {
                x: bounds.left + point.x,
                y: bounds.top + point.y,
                label: selected.data('label'),
            };
        }"""
    )
    page.mouse.click(node["x"], node["y"])
    page.wait_for_function("() => document.querySelectorAll('#detail .edge-target').length > 0")
    return node


def test_generated_file_artifact_filters_search_and_shows_edge_details(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        (ROOT / "examples/synthetic-graphs/small-workflow.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    source_root = tmp_path / "source"
    source = source_root / "src"
    source.mkdir(parents=True)
    source.joinpath("checkout.py").write_text(
        "\n".join(f"line {i}" for i in range(80)), encoding="utf-8"
    )
    source.joinpath("tax.py").write_text("def calculate_tax(): pass\n", encoding="utf-8")
    output = tmp_path / "view.html"
    assert (
        cli.main(
            [
                "visualize",
                "--input",
                str(graph_path),
                "--output",
                str(output),
                "--source-root",
                str(source_root),
            ]
        )
        == 0
    )

    requested: list[str] = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.on("request", lambda request: requested.append(request.url))
        page.goto(output.as_uri())
        assert page.locator("#theme-mode").input_value() == "system"
        for mode, expected_background in {
            "light": "rgb(245, 245, 240)",
            "catppuccin-mocha": "rgb(30, 30, 46)",
            "nord-polar-night": "rgb(46, 52, 64)",
            "solarized-dark": "rgb(0, 43, 54)",
        }.items():
            page.locator("#theme-mode").select_option(mode)
            background = page.locator("#cy").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            assert background == expected_background
            assert page.evaluate(
                """() => {
                    const isYellow = (color) => color[0] > 120 && color[1] > 120
                        && color[2] * 1.4 < color[0] && color[2] * 1.4 < color[1]
                        && Math.abs(color[0] - color[1]) < 110;
                    return window.minotaurVisualizer.cy.edges().every((edge) => {
                        const checkbox = document.querySelector(
                            `input[data-edgekind="${edge.data('kind')}"]`
                        );
                        const hex = getComputedStyle(checkbox)
                            .getPropertyValue('--kind-border').trim();
                        const value = Number.parseInt(hex.slice(1), 16);
                        const expected = [value >> 16, (value >> 8) & 255, value & 255];
                        const line = edge.pstyle('line-color').value;
                        const arrow = edge.pstyle('target-arrow-color').value;
                        return !isYellow(line)
                            && line.every((channel, index) => channel === expected[index])
                            && arrow.every((channel, index) => channel === expected[index]);
                    });
                }"""
            )
        assert page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const nodeFontSize = cy.nodes()[0].pstyle('font-size').pfValue;
                return cy.nodes().every((node) => node.pstyle('font-size').pfValue === nodeFontSize)
                    && cy.edges().every((edge) => (
                        edge.pstyle('font-size').pfValue === nodeFontSize
                        && edge.pstyle('font-weight').strValue === 'bold'
                    ));
            }"""
        )
        assert page.evaluate(
            """() => window.minotaurVisualizer.cy.edges().every((edge) => {
                const checkbox = document.querySelector(
                    `input[data-edgekind="${edge.data('kind')}"]`
                );
                const hex = getComputedStyle(checkbox).getPropertyValue('--kind-border').trim();
                const value = Number.parseInt(hex.slice(1), 16);
                const expected = [value >> 16, (value >> 8) & 255, value & 255];
                const line = edge.pstyle('line-color').value;
                const arrow = edge.pstyle('target-arrow-color').value;
                return line.every((channel, index) => channel === expected[index])
                    && arrow.every((channel, index) => channel === expected[index]);
            })"""
        )
        total_nodes = page.evaluate("window.minotaurVisualizer.cy.nodes().length")
        page.locator('input[data-kind="symbol"]').uncheck()
        assert page.evaluate("window.minotaurVisualizer.cy.nodes(':visible').length") < total_nodes
        page.locator('input[data-kind="symbol"]').check()
        page.locator("#search").fill("no such graph node")
        page.wait_for_function(
            "window.minotaurVisualizer.cy.edges().every((edge) => edge.hasClass('dimmed'))"
        )
        assert page.evaluate(
            "window.minotaurVisualizer.cy.edges().every((edge) => edge.hasClass('dimmed'))"
        )
        page.locator("#search").fill("")
        page.wait_for_function(
            "window.minotaurVisualizer.cy.edges().every((edge) => !edge.hasClass('dimmed'))"
        )
        edge = _click_visible_edge_and_show_details(page)
        assert edge["kind"] in page.locator("#detail-content").inner_text()
        assert page.evaluate(
            """(edgeId) => {
                const edge = window.minotaurVisualizer.cy.getElementById(edgeId);
                const selectedHex = window.minotaurVisualizer.activeTheme().selected;
                const selectedValue = Number.parseInt(selectedHex.slice(1), 16);
                const selectedRed = [
                    selectedValue >> 16,
                    (selectedValue >> 8) & 255,
                    selectedValue & 255,
                ];
                return edge.hasClass('highlighted')
                    && edge.pstyle('line-color').value.every(
                        (channel, index) => channel === selectedRed[index]
                    )
                    && edge.pstyle('target-arrow-color').value.every(
                        (channel, index) => channel === selectedRed[index]
                    );
            }""",
            arg=edge["id"],
        )
        assert page.evaluate(
            "document.querySelector('#detail').getBoundingClientRect().right <= "
            "document.querySelector('#cy').getBoundingClientRect().left"
        )
        node = _click_connected_node_and_show_details(page)
        assert node["label"] in page.locator("#detail-content").inner_text()
        assert page.evaluate(
            """() => {
                const detail = document.querySelector('#detail');
                const item = detail.querySelector('.edge-item');
                const styles = getComputedStyle(detail);
                const resize = document.querySelector('#detail-resize');
                const handleHeight = resize.getBoundingClientRect().height;
                const detailHeight = detail.getBoundingClientRect().height;
                return Number.parseFloat(styles.minWidth) === 240
                    && handleHeight === detailHeight
                    && item.children.length === 2
                    && getComputedStyle(item).display === 'grid';
            }"""
        )
        starting_width = page.locator("#detail").bounding_box()["width"]
        resize_box = page.locator("#detail-resize").bounding_box()
        page.mouse.move(resize_box["x"] + resize_box["width"] / 2, resize_box["y"] + 30)
        page.mouse.down()
        page.mouse.move(resize_box["x"] + resize_box["width"] / 2 + 80, resize_box["y"] + 30)
        page.mouse.up()
        assert page.locator("#detail").bounding_box()["width"] >= starting_width + 75
        page.keyboard.press("Escape")
        assert "Select a node or edge" in page.locator("#detail-content").inner_text()
        page.reload()
        assert page.locator("#theme-mode").input_value() == "system"
        browser.close()
    assert all(url.startswith("file:") for url in requested)


def test_checked_in_python_workflow_artifact_opens_without_external_requests() -> None:
    """The public example remains usable as an offline download/open artifact."""
    artifact = ROOT / "examples/python-workflow/minotaur-graph.html"
    requested: list[str] = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.on("request", lambda request: requested.append(request.url))
        page.goto(artifact.as_uri())
        assert page.evaluate("window.minotaurVisualizer.cy.nodes().length") > 0
        edge = _click_visible_edge_and_show_details(page)
        assert edge["kind"] in page.locator("#detail-content").inner_text()
        browser.close()
    assert all(url.startswith("file:") for url in requested)
