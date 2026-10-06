"""Chromium proof for interactions in a generated ``file://`` artifact."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_visualizer.html.render import render_html
from minotaur.graph_visualizer.presentation import (
    build_comparison_presentation,
    build_presentation,
)
from minotaur.query.call_diff import CallChange, CallLimitation
from minotaur.query.graph_comparison import (
    GraphNodeChange,
    GraphRelationshipChange,
    endpoint_eligibility,
)
from minotaur.query.system_diff import SystemDiffResult

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


def test_generated_file_artifact_filters_search_and_shows_edge_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_path = tmp_path / "graph.json"
    graph = json.loads((ROOT / "examples/synthetic-graphs/small-workflow.json").read_text())
    unrelated = json.loads(
        (ROOT / "examples/synthetic-graphs/unresolved-reference-demo.json").read_text()
    )["nodes"][0]
    graph["nodes"].append(unrelated)
    # Duplicate range evidence must become one user-selectable call site while
    # exposing both supporting provenance values in the rendered artifact.
    relationship = graph["relationships"][0]
    relationship["evidence"].append(
        {
            "provenance": "curated-rule",
            "rule": {"id": "test-rule"},
            "locations": [relationship["evidence"][0]["locations"][0]],
        }
    )
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    source_root = tmp_path / "source"
    source = source_root / "src"
    source.mkdir(parents=True)
    source.joinpath("checkout.py").write_text(
        "\n".join(f"line {i}" for i in range(80)), encoding="utf-8"
    )
    source.joinpath("tax.py").write_text("def calculate_tax(): pass\n", encoding="utf-8")
    systems_dir = source_root / "docs/systems"
    for name, file in {
        "checkout": "src/checkout.py",
        "notifications": "src/notifications.py",
        "tax": "src/tax.py",
    }.items():
        system_dir = systems_dir / name
        system_dir.mkdir(parents=True)
        system_dir.joinpath("system.toml").write_text(
            f'schema_version = 1\nname = "{name}"\nfiles = ["{file}"]\n',
            encoding="utf-8",
        )
    tmp_path.joinpath(".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "source"\n'
        'graph = "../graph.json"\ntargets = ["src/checkout.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
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
        assert page.locator("#system-filter option").all_text_contents() == [
            "All Systems",
            "checkout",
            "notifications",
            "tax",
        ]
        assert page.locator("#cross-system-connections").is_disabled()
        assert not page.locator("#cross-system-connections").is_checked()
        page.locator("#system-filter").select_option("checkout")
        page.wait_for_timeout(450)
        assert page.locator("#cross-system-connections").is_enabled()
        assert not page.locator("#cross-system-connections").is_checked()
        assert page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const visible = cy.nodes(':visible');
                const box = visible.renderedBoundingBox({includeLabels:true});
                return visible.length === 2
                    && visible.every(node => node.data('system') === 'checkout')
                    && cy.edges(':visible').length === 0
                    && cy.nodes('.system-container').length === 0
                    && Math.abs((box.x1 + box.x2) / 2 - cy.width() / 2) < 1
                    && Math.abs((box.y1 + box.y2) / 2 - cy.height() / 2) < 1;
            }"""
        )
        page.locator("#cross-system-connections").check()
        page.wait_for_timeout(450)
        assert page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const nodes = cy.nodes().not('.system-container');
                const inside = nodes.filter(node => node.data('system') === 'checkout');
                const outside = nodes.filter(node => node.data('system') !== 'checkout');
                const containers = cy.nodes('.system-container');
                return inside.length === 2
                    && outside.length === 2
                    && inside.every(node => !node.hasClass('outside-system'))
                    && outside.every(node => node.hasClass('outside-system'))
                    && outside.filter(':visible').length === 1
                    && outside.filter(':visible').every(node =>
                        node.pstyle('border-width').pfValue === 6
                        && node.pstyle('border-color').strValue === 'rgb(198,40,40)')
                    && cy.edges(':visible').length === 1
                    && cy.edges(':visible').every(edge => edge.hasClass('cross-system')
                        && edge.pstyle('width').pfValue === 4
                        && edge.pstyle('line-color').strValue === 'rgb(198,40,40)')
                    && containers.length === 2
                    && containers.map(node => node.data('label'))
                        .sort().join(',') === 'checkout,tax'
                    && new Set(containers.map(node => node.data('container_color'))).size === 2
                    && cy.getElementById('system-container:checkout').children().length === 2
                    && cy.getElementById('system-container:tax').children().length === 1;
            }"""
        )
        assert page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const selected = cy.getElementById('system-container:checkout');
                const boundary = cy.getElementById('system-container:tax');
                const selectedBox = selected.renderedBoundingBox({includeLabels:true});
                const boundaryBox = boundary.renderedBoundingBox({includeLabels:true});
                const selectedCenter = {
                    x:(selectedBox.x1 + selectedBox.x2) / 2,
                    y:(selectedBox.y1 + selectedBox.y2) / 2
                };
                const centered = Math.abs(selectedCenter.x - cy.width() / 2) < 1
                    && Math.abs(selectedCenter.y - cy.height() / 2) < 1;
                const separated = boundaryBox.x2 < selectedBox.x1
                    || boundaryBox.x1 > selectedBox.x2
                    || boundaryBox.y2 < selectedBox.y1
                    || boundaryBox.y1 > selectedBox.y2;
                return centered && separated;
            }"""
        )
        page.locator("#system-filter").select_option("")
        page.wait_for_timeout(450)
        assert page.locator("#cross-system-connections").is_disabled()
        assert page.locator("#cross-system-connections").is_checked()
        assert page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                return cy.nodes(':visible').length === 4
                    && cy.nodes().every(node => !node.hasClass('outside-system'))
                    && cy.edges().every(edge => edge.hasClass('cross-system'));
            }"""
        )
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
                        const line = edge.pstyle('line-color').value;
                        const arrow = edge.pstyle('target-arrow-color').value;
                        return !isYellow(line)
                            && edge.hasClass('cross-system')
                            && line.join(',') === '198,40,40'
                            && arrow.join(',') === '198,40,40';
                    });
                }"""
            )
        assert page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const nodeFontSize = cy.nodes()[0].pstyle('font-size').pfValue;
                return nodeFontSize === 14
                    && cy.nodes().every((node) => node.pstyle('font-size').pfValue === nodeFontSize)
                    && cy.edges().every((edge) => (
                        edge.pstyle('font-size').pfValue === nodeFontSize
                        && edge.pstyle('font-weight').strValue === 'bold'
                    ));
            }"""
        )
        assert page.evaluate(
            """() => window.minotaurVisualizer.cy.edges().every((edge) => {
                const line = edge.pstyle('line-color').value;
                const arrow = edge.pstyle('target-arrow-color').value;
                return edge.hasClass('cross-system')
                    && line.join(',') === '198,40,40'
                    && arrow.join(',') === '198,40,40';
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
        assert page.locator("#call-site-select").count() == 1
        assert (
            page.locator("#call-site-select")
            .locator("option")
            .inner_text()
            .startswith("1. src/checkout.py:5:12")
        )
        assert page.locator("#context-mode").locator("option").all_text_contents() == [
            "Call-site window",
            "Caller start → call",
        ]
        assert page.locator(".call-site-highlight").inner_text().endswith("line 4")
        # A real overflow check proves the excerpt itself scrolls rather than
        # expanding the entire detail pane beyond the viewport.
        assert page.locator(".code-excerpt").evaluate(
            "element => element.scrollHeight > element.clientHeight"
        )
        site_detail = page.locator("#call-site-detail").inner_text()
        assert "static-analysis" in site_detail and "curated-rule" in site_detail
        page.locator("#context-mode").select_option("caller")
        assert page.locator(".code-line").first.inner_text().endswith("line 2")
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
        # Recalculate screen coordinates after the flex layout changes. This
        # fails if Cytoscape still translates pointer input using its old canvas
        # bounds, which is the offset-selection regression this test guards.
        resized_edge = _click_visible_edge_and_show_details(page)
        assert resized_edge["kind"] in page.locator("#detail-content").inner_text()
        resized_node = _click_connected_node_and_show_details(page)
        assert resized_node["label"] in page.locator("#detail-content").inner_text()
        page.reload()
        assert page.locator("#theme-mode").input_value() == "system"
        browser.close()
    assert all(url.startswith("file:") for url in requested)


def test_shortcuts_respect_focused_controls_and_keep_body_shortcuts(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_bytes((ROOT / "examples/synthetic-graphs/provenance-demo.json").read_bytes())
    output = tmp_path / "view.html"
    assert cli.main(["visualize", "--input", str(graph_path), "--output", str(output)]) == 0

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(output.as_uri())
        page.wait_for_function("() => window.minotaurVisualizer?.cy")
        camera = (
            "() => ({zoom: window.minotaurVisualizer.cy.zoom(), "
            "pan: window.minotaurVisualizer.cy.pan()})"
        )
        fitted = page.evaluate(camera)
        page.evaluate("window.minotaurVisualizer.cy.panBy({x: 123, y: 57})")
        panned = page.evaluate(camera)
        assert panned != fitted
        system_filter = page.locator("#system-filter")
        assert all(
            not label.lower().startswith("f")
            for label in system_filter.locator("option").all_text_contents()
        )
        system_filter.focus()
        page.keyboard.press("f")
        assert page.evaluate(camera) == panned
        system_filter.evaluate("element => element.blur()")
        assert page.evaluate("document.activeElement.tagName") == "BODY"
        page.keyboard.press("f")
        assert page.evaluate(camera) == fitted

        _click_visible_edge_and_show_details(page)
        details = page.locator("#detail-content").inner_text()
        search = page.locator("#search")
        search.fill("foo")
        checkbox = page.locator('#edge-filters input[type="checkbox"]').first
        checkbox.focus()
        page.keyboard.press("Escape")
        assert search.input_value() == "foo"
        assert page.locator("#detail-content").inner_text() == details

        site_select = page.locator("#call-site-select")
        site_select.focus()
        page.keyboard.press("Escape")
        assert page.locator("#detail-content").inner_text() == details
        assert search.input_value() == "foo"
        search.focus()
        page.keyboard.press("Escape")
        assert search.input_value() == ""
        assert not search.evaluate("element => element === document.activeElement")
        assert page.locator("#detail-content").inner_text() == details
        assert page.evaluate("document.activeElement.tagName") == "BODY"
        page.keyboard.press("Escape")
        assert "Select a node or edge" in page.locator("#detail-content").inner_text()
        browser.close()


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


@pytest.mark.parametrize("kind", ["calls", "references"])
def test_edge_inspector_keeps_provenance_without_locations(tmp_path: Path, kind: str) -> None:
    graph = json.loads((ROOT / "examples/synthetic-graphs/provenance-demo.json").read_text())
    graph["relationships"][0]["kind"] = kind
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    output = tmp_path / "view.html"
    assert cli.main(["visualize", "--input", str(graph_path), "--output", str(output)]) == 0

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(output.as_uri())
        _click_visible_edge_and_show_details(page)
        assert (
            page.get_by_text("Provenance", exact=True)
            .locator("..")
            .locator(".field-value")
            .inner_text()
            == "imported-graph, static-analysis"
        )
        assert (
            page.get_by_text("Supporting provenance", exact=True)
            .locator("..")
            .locator(".field-value")
            .inner_text()
            == "static-analysis"
        )
        assert (
            page.get_by_text("Location", exact=True)
            .locator("..")
            .locator(".field-value")
            .inner_text()
            == "src/checkout.py:5:12"
        )
        if kind == "calls":
            assert page.locator("#call-site-select option").all_text_contents() == [
                "1. src/checkout.py:5:12"
            ]
        browser.close()


def test_non_call_locations_merge_provenance_and_hover_locations(tmp_path: Path) -> None:
    graph = json.loads((ROOT / "examples/synthetic-graphs/small-workflow.json").read_text())
    relationship = graph["relationships"][0]
    relationship["kind"] = "references"
    first_location = relationship["evidence"][0]["locations"][0]
    second_location = {
        "path": "src/checkout.py",
        "range": {
            "start": {"line": 6, "character": 11},
            "end": {"line": 6, "character": 25},
        },
    }
    relationship["evidence"] = [
        {"provenance": "curated-rule", "rule": {"id": "test-rule"}, "locations": [first_location]},
        {"provenance": "static-analysis", "locations": [first_location]},
        {"provenance": "imported-graph", "locations": [second_location]},
    ]
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    output = tmp_path / "view.html"
    assert cli.main(["visualize", "--input", str(graph_path), "--output", str(output)]) == 0

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(output.as_uri())
        edge = _click_visible_edge_and_show_details(page)
        assert edge["kind"] == "references"
        assert (
            page.get_by_text("Provenance", exact=True)
            .locator("..")
            .locator(".field-value")
            .inner_text()
            == "curated-rule, static-analysis, imported-graph"
        )
        assert page.get_by_text("Locations (2)", exact=True).is_visible()
        assert page.locator("#site-tabs button").count() == 2
        assert page.locator("#site-location").inner_text() == "src/checkout.py:5:12"
        assert page.locator("#site-provenance").inner_text() == "curated-rule, static-analysis"
        page.locator("#site-tabs button").nth(1).click()
        assert page.locator("#site-location").inner_text() == "src/checkout.py:7:12"
        assert page.locator("#site-provenance").inner_text() == "imported-graph"
        point = page.evaluate(
            """id => {
                const cy = window.minotaurVisualizer.cy;
                const point = cy.getElementById(id).renderedMidpoint();
                const bounds = cy.container().getBoundingClientRect();
                return {x: bounds.left + point.x, y: bounds.top + point.y};
            }""",
            edge["id"],
        )
        page.mouse.move(point["x"], point["y"])
        playwright.expect(page.locator("#tooltip")).to_be_visible()
        assert page.locator("#tooltip").text_content().splitlines() == [
            "references",
            "src/checkout.py:5:12",
            "src/checkout.py:7:12",
        ]
        browser.close()


def test_sql_fk_pointer_details_keep_mapping_and_payload_free_records_separate(
    tmp_path: Path,
) -> None:
    """A real FK edge click preserves each record's mappings and locations."""
    graph = json.loads((ROOT / "examples/synthetic-graphs/small-workflow.json").read_text())
    relationship = graph["relationships"][0]
    relationship["kind"] = "sql:foreign-key-to"
    duplicate_location = relationship["evidence"][0]["locations"][0]
    relationship["evidence"] = [
        {
            "provenance": "static-analysis",
            "extensions": {
                "minotaur-sql": {
                    "foreign_key_columns": [
                        {"local": "order_id", "referenced": "id"},
                        {"local": "store_id", "referenced": "id"},
                    ]
                }
            },
            "locations": [duplicate_location],
        },
        {
            "provenance": "static-analysis",
            "locations": [
                duplicate_location,
                {
                    "path": "tables.sql",
                    "range": {
                        "start": {"line": 12, "character": 0},
                        "end": {"line": 12, "character": 10},
                    },
                },
            ],
        },
    ]
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    output = tmp_path / "view.html"
    assert cli.main(["visualize", "--input", str(graph_path), "--output", str(output)]) == 0

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(output.as_uri())
        edge = _click_visible_edge_and_show_details(page)
        assert edge["kind"] == "sql:foreign-key-to"
        records = page.locator(".sql-fk-record")
        assert records.count() == 2
        mapped = records.nth(0).inner_text()
        payload_free = records.nth(1).inner_text()
        assert records.nth(0).locator(".sql-fk-pairs li").all_text_contents() == [
            "order_id → id",
            "store_id → id",
        ]
        assert "src/checkout.py:5:12" in mapped
        assert "tables.sql:13:1" not in mapped
        assert "Column mappings" not in payload_free
        assert "order_id" not in payload_free
        assert "store_id" not in payload_free
        assert "src/checkout.py:5:12" in payload_free
        assert "tables.sql:13:1" in payload_free
        browser.close()


def test_extension_free_sql_fk_keeps_generic_location_tabs(
    tmp_path: Path,
) -> None:
    """An extension-free SQL edge remains on the generic detail path."""
    graph = json.loads((ROOT / "examples/synthetic-graphs/small-workflow.json").read_text())
    relationship = graph["relationships"][0]
    relationship["kind"] = "sql:foreign-key-to"
    relationship["evidence"][0]["locations"].append(
        {
            "path": "tables.sql",
            "range": {
                "start": {"line": 12, "character": 0},
                "end": {"line": 12, "character": 10},
            },
        }
    )
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    output = tmp_path / "view.html"
    assert cli.main(["visualize", "--input", str(graph_path), "--output", str(output)]) == 0

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(output.as_uri())
        edge = _click_visible_edge_and_show_details(page)
        assert edge["kind"] == "sql:foreign-key-to"
        assert page.locator(".sql-fk-record").count() == 0
        assert page.locator("#site-tabs").count() == 1
        assert page.locator("#site-location").inner_text() == "src/checkout.py:5:12"
        browser.close()


def test_graph_format_documents_sql_fk_evidence_contract() -> None:
    documentation = (ROOT / "docs/formats/minotaur-graph-v1.md").read_text()
    for phrase in (
        '`extensions["minotaur-sql"] = {"foreign_key_columns": [...]}`',
        "`foreign_key_columns` array is nonempty",
        "fields `local` and `referenced`",
        "Its order is semantic",
        "table-edge tuple `(source, target, kind)`",
        "Constraint names",
        "`ON DELETE`",
        "`ON UPDATE`",
        "`NOT FOR REPLICATION`",
    ):
        assert phrase in documentation


@pytest.mark.parametrize("comparison", [False, True], ids=["ordinary", "comparison"])
@pytest.mark.parametrize("declared_name", ["External / Unassigned", "declared"])
def test_unassigned_containers_do_not_collide_with_declared_systems(
    tmp_path: Path, comparison: bool, declared_name: str
) -> None:
    membership = {"checkout": "checkout", "declared": declared_name, "unassigned": None}
    if comparison:
        presentation = _comparison_presentation(
            [
                _comparison_node(node_id, before_system=system, after_system=system)
                for node_id, system in membership.items()
            ],
            [
                _comparison_edge(
                    "edge:" + target,
                    "checkout",
                    target,
                    before_systems=("checkout", membership[target]),
                    after_systems=("checkout", membership[target]),
                )
                for target in ("declared", "unassigned")
            ],
            changed=False,
        )
    else:
        presentation = build_presentation(
            {
                "nodes": [
                    {"id": node_id, "label": node_id, "node_class": "symbol"}
                    for node_id in membership
                ],
                "relationships": [
                    {"source": "checkout", "target": target, "kind": "calls", "evidence": []}
                    for target in ("declared", "unassigned")
                ],
            }
        )
        presentation["systems"] = ["checkout", declared_name]
        presentation["node_systems"] = {
            node_id: system for node_id, system in membership.items() if system is not None
        }
    artifact = tmp_path / "containers.html"
    artifact.write_bytes(render_html(presentation))

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(artifact.as_uri())
        page.locator("#system-filter").select_option("checkout")
        page.locator("#cross-system-connections").check()
        page.wait_for_function(
            "() => window.minotaurVisualizer.cy.nodes('.system-container').length > 0"
        )
        containers = page.evaluate(
            """() => Object.fromEntries(window.minotaurVisualizer.cy.nodes('.system-container').map(
                node => [node.id(), node.data('label')]
            ))"""
        )
        assert containers == {
            "system-container:checkout": "checkout",
            "system-container:" + declared_name: declared_name,
            "unassigned-system-container": (
                "External / Unassigned (undeclared)"
                if declared_name == "External / Unassigned"
                else "External / Unassigned"
            ),
        }
        for node_id, container_id in (
            ("declared", "system-container:" + declared_name),
            ("unassigned", "unassigned-system-container"),
        ):
            if comparison:
                assert page.evaluate(
                    """([nodeId, containerId]) => {
                        const cy = window.minotaurVisualizer.cy;
                        const box = cy.getElementById(nodeId).boundingBox({includeLabels:true});
                        const center = cy.getElementById(containerId).position();
                        return Math.abs(center.x - (box.x1 + box.x2) / 2) < 0.01
                            && Math.abs(center.y - (box.y1 + box.y2) / 2) < 0.01;
                    }""",
                    [node_id, container_id],
                )
            else:
                assert page.evaluate(
                    "id => window.minotaurVisualizer.cy.getElementById(id)"
                    ".children().map(n => n.id())",
                    container_id,
                ) == [node_id]
        page.locator("#system-filter").select_option(declared_name)
        page.wait_for_function(
            "name => window.minotaurVisualizer.cy.nodes('.selected-system-container')"
            ".map(n => n.id()).join() === 'system-container:' + name",
            arg=declared_name,
        )
        assert page.evaluate(
            "window.minotaurVisualizer.cy.nodes('.selected-system-container').map(n => n.id())"
        ) == ["system-container:" + declared_name]
        browser.close()


def test_checked_in_system_walkthrough_exposes_configured_boundary_view() -> None:
    """The public system example exercises focus, boundaries, and offline use."""
    artifact = ROOT / "examples/system-walkthrough/minotaur-graph.html"
    requested: list[str] = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.on("request", lambda request: requested.append(request.url))
        page.goto(artifact.as_uri())
        assert page.locator("#system-filter option").all_text_contents() == [
            "All Systems",
            "billing",
            "orders",
        ]
        page.locator("#system-filter").select_option("orders")
        page.locator("#btn-direction").click()
        page.locator("#cross-system-connections").check()
        page.wait_for_timeout(450)
        assert page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const labels = cy.nodes('.system-container')
                    .map(node => node.data('label')).sort();
                return labels.includes('orders')
                    && labels.includes('billing')
                    && labels.includes('External / Unassigned')
                    && cy.edges(':visible').filter('.cross-system').length > 0
                    && cy.nodes('.boundary-system-container').every(node => {
                        const selected = cy.getElementById('system-container:orders').position();
                        const boundary = node.position();
                        return Math.abs(boundary.x - selected.x)
                            > Math.abs(boundary.y - selected.y);
                    });
            }"""
        )
        browser.close()
    assert all(url.startswith("file:") for url in requested)


def test_python_workflow_preview_generator_captures_selected_call_site(tmp_path: Path) -> None:
    """The README preview is reproducible from the public offline artifact."""
    preview = tmp_path / "python-workflow-demo.png"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "capture_python_workflow_demo.py"),
            "--output",
            str(preview),
        ],
        cwd=ROOT,
        check=True,
    )
    png = preview.read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (1440, 900)
    assert len(png) > 10_000


def test_system_walkthrough_preview_generator_captures_boundary_view(tmp_path: Path) -> None:
    """The system documentation preview is reproducible from the public artifact."""
    preview = tmp_path / "system-walkthrough-demo.png"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "capture_system_walkthrough_demo.py"),
            "--output",
            str(preview),
        ],
        cwd=ROOT,
        check=True,
    )
    png = preview.read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (1440, 900)
    assert len(png) > 10_000


def test_ordinary_context_mode_persists_until_unavailable_or_edge_reselected(
    tmp_path: Path,
) -> None:
    graph = json.loads((ROOT / "examples/synthetic-graphs/small-workflow.json").read_text())
    graph["relationships"][0]["evidence"][0]["locations"].extend(
        [
            {
                "path": "src/checkout.py",
                "range": {
                    "start": {"line": 70, "character": 11},
                    "end": {"line": 70, "character": 25},
                },
            },
            {
                "path": "src/other.py",
                "range": {
                    "start": {"line": 20, "character": 0},
                    "end": {"line": 20, "character": 5},
                },
            },
        ]
    )
    source_root = tmp_path / "source"
    (source_root / "src").mkdir(parents=True)
    for filename in ("checkout.py", "other.py"):
        (source_root / "src" / filename).write_text(
            "\n".join(f"line {line}" for line in range(100)), encoding="utf-8"
        )
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
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

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(output.as_uri())
        _click_visible_edge_and_show_details(page)
        sites = page.locator("#call-site-select")
        mode = page.locator("#context-mode")
        assert sites.locator("option").all_text_contents() == [
            "1. src/checkout.py:5:12",
            "2. src/checkout.py:71:12",
            "3. src/other.py:21:1",
        ]
        assert mode.input_value() == "window"
        mode.select_option("caller")
        sites.select_option("1")
        assert mode.input_value() == "caller"
        assert page.locator("#call-site-detail .code-number").first.inner_text() == "3"
        assert page.locator("#call-site-detail .code-text").first.inner_text() == "line 2"
        assert page.locator(".call-site-highlight .code-number").all_text_contents() == ["71"]
        sites.select_option("2")
        assert mode.locator("option").all_text_contents() == ["Call-site window"]
        assert mode.input_value() == "window"
        assert page.locator("#call-site-detail .code-number").first.inner_text() == "1"
        sites.select_option("0")
        assert mode.input_value() == "window"
        mode.select_option("caller")
        mode.evaluate("element => element.blur()")
        page.keyboard.press("Escape")
        assert "Select a node or edge" in page.locator("#detail-content").inner_text()
        _click_visible_edge_and_show_details(page)
        assert mode.input_value() == "window"
        assert page.locator("#call-site-detail .code-number").first.inner_text() == "1"
        browser.close()


def test_call_site_context_is_unavailable_without_a_root_and_has_no_caller_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = json.loads((ROOT / "examples/synthetic-graphs/small-workflow.json").read_text())
    # A file caller has no known function/method boundary, so the alternate
    # prefix mode must not be offered even though its call location is valid.
    graph["relationships"][0]["source"] = graph["nodes"][1]["id"]
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    output = tmp_path / "view.html"
    monkeypatch.chdir(tmp_path)
    assert cli.main(["visualize", "--input", str(graph_path), "--output", str(output)]) == 0

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(output.as_uri())
        _click_visible_edge_and_show_details(page)
        assert page.locator("#context-mode").locator("option").all_text_contents() == [
            "Call-site window"
        ]
        assert "no source root was provided" in page.locator("#call-site-detail").inner_text()
        browser.close()


def _assert_visible_layout(page: object, direction: str) -> None:
    page.wait_for_timeout(450)
    result = page.evaluate(
        """async direction => {
            const cy = window.minotaurVisualizer.cy;
            const enabled = new Set(Array.from(document.querySelectorAll(
                '#kind-filters input:checked'), cb => cb.dataset.kind));
            const edges = new Set(Array.from(document.querySelectorAll(
                '#edge-filters input:checked'), cb => cb.dataset.edgekind));
            const original = window.originalElements;
            const ids = new Set(original.filter(e => e.group === 'nodes' &&
                enabled.has(e.data.node_class)).map(e => e.data.id));
            const eligible = original.filter(e => e.group === 'nodes' ? ids.has(e.data.id) :
                edges.has(e.data.kind) && ids.has(e.data.source) && ids.has(e.data.target));
            if (!window.layoutReference) {
                const host = document.createElement('div');
                host.style.cssText = `position:absolute;left:-10000px;width:${cy.width()}px;` +
                    `height:${cy.height()}px`;
                document.body.appendChild(host);
                window.layoutReference = cytoscape({container:host, elements:[],
                    style:cy.style().json(), layout:{name:'preset'}, minZoom:0.1, maxZoom:4});
            }
            const reference = window.layoutReference;
            reference.elements().remove();
            reference.add(eligible.map(e => ({
                group: e.group, data: e.data,
                selected: cy.getElementById(e.data.id).selected(),
                classes: cy.getElementById(e.data.id).classes().join(' ')
            })));
            reference.layout({name:'grid', fit:false}).run();
            await new Promise(requestAnimationFrame);
            if (ids.size) reference.layout({name:'dagre', rankDir:direction,
                nodeSep:40, rankSep:60, edgeSep:15, animate:false, padding:30}).run();
            const errors = reference.nodes().map(n => {
                const actual = cy.getElementById(n.id()).position();
                return Math.hypot(actual.x - n.position('x'), actual.y - n.position('y'));
            });
            const camera = [cy.zoom(), cy.pan('x'), cy.pan('y')];
            const expectedCamera = [reference.zoom(), reference.pan('x'), reference.pan('y')];
            const result = {
                error: Math.max(0, ...errors),
                finite: cy.nodes().every(n => Number.isFinite(n.position('x')) &&
                    Number.isFinite(n.position('y'))) && camera.every(Number.isFinite),
                visible: cy.elements(':visible').map(e => e.id()).sort(),
                expected: eligible.map(e => e.data.id).sort(),
                cameraError: ids.size ? Math.max(...camera.map((v,i) =>
                    Math.abs(v - expectedCamera[i]))) : 0,
                preserved: cy.elements().every((e,i) => e === window.originalIdentities[i] &&
                    JSON.stringify(e.data()) === JSON.stringify(original[i].data))
            };
            return result;
        }""",
        direction,
    )
    assert result["visible"] == result["expected"]
    assert result["finite"]
    assert result["preserved"]
    assert result["error"] < 0.01, result
    assert result["cameraError"] < 0.1, result


@pytest.mark.parametrize("bundled", [False, True], ids=["generated", "bundled"])
def test_layout_uses_only_filter_eligible_elements(tmp_path: Path, bundled: bool) -> None:
    artifact = ROOT / "examples/python-workflow/minotaur-graph.html"
    if not bundled:
        artifact = tmp_path / "viewer.html"
        assert (
            cli.main(
                [
                    "visualize",
                    "--input",
                    str(ROOT / "examples/python-workflow/minotaur-graph.json"),
                    "--output",
                    str(artifact),
                    "--source-root",
                    str(ROOT / "src"),
                ]
            )
            == 0
        )
    errors: list[str] = []
    requests: list[str] = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(artifact.as_uri())
        page.evaluate("""() => {
            const cy = window.minotaurVisualizer.cy;
            window.originalElements = cy.elements().map(e => ({group:e.group(), data:e.data()}));
            window.originalElements = JSON.parse(JSON.stringify(window.originalElements));
            window.originalIdentities = cy.elements().toArray();
        }""")
        _assert_visible_layout(page, "TB")
        classes = ["file", "symbol", "unresolved-reference"]
        for mask in [6, 5, 4, 3, 2, 1, 0, 7]:
            for index, kind in enumerate(classes):
                page.locator(f'input[data-kind="{kind}"]').set_checked(bool(mask & (1 << index)))
            empty_camera = (
                page.evaluate(
                    "({zoom:window.minotaurVisualizer.cy.zoom(), "
                    "pan:window.minotaurVisualizer.cy.pan()})"
                )
                if mask == 0
                else None
            )
            _assert_visible_layout(page, "TB")
            if mask == 0:
                camera = page.evaluate(
                    "({zoom:window.minotaurVisualizer.cy.zoom(), "
                    "pan:window.minotaurVisualizer.cy.pan()})"
                )
                assert camera == empty_camera
                page.locator("#btn-fit").click()
                page.keyboard.press("f")
                page.wait_for_timeout(350)
                assert (
                    page.evaluate(
                        "({zoom:window.minotaurVisualizer.cy.zoom(), "
                        "pan:window.minotaurVisualizer.cy.pan()})"
                    )
                    == camera
                )
        for direction in ["LR", "BT", "RL"]:
            page.locator("#btn-direction").click()
            _assert_visible_layout(page, direction)
        for checkbox in page.locator("#edge-filters input").all():
            checkbox.uncheck()
            _assert_visible_layout(page, "RL")
            page.locator('input[data-kind="symbol"]').uncheck()
            page.locator('input[data-kind="symbol"]').check()
            _assert_visible_layout(page, "RL")
            checkbox.check()
        page.locator('input[data-kind="unresolved-reference"]').uncheck()
        _assert_visible_layout(page, "RL")
        for reset in ["button", "shortcut"]:
            page.evaluate("window.minotaurVisualizer.cy.pan({x:0,y:0})")
            if reset == "button":
                page.locator("#btn-fit").click()
            else:
                page.keyboard.press("f")
            _assert_visible_layout(page, "RL")
        page.locator("#search").fill("no matching label anywhere")
        page.wait_for_timeout(200)
        assert page.evaluate("window.minotaurVisualizer.cy.nodes('.dimmed').length") > 0
        page.locator("#btn-direction").click()
        _assert_visible_layout(page, "TB")
        page.locator("#search").fill("")
        page.wait_for_timeout(200)
        _click_connected_node_and_show_details(page)
        page.locator('input[data-kind="file"]').uncheck()
        page.locator('input[data-kind="symbol"]').uncheck()
        assert (
            "Select a node or edge to inspect it." in page.locator("#detail-content").inner_text()
        )
        page.locator('input[data-kind="file"]').check()
        page.locator('input[data-kind="symbol"]').check()
        page.locator("#btn-direction").click()
        assert page.evaluate("window.minotaurVisualizer.cy.nodes().some(n => n.animated())")
        page.locator('input[data-kind="unresolved-reference"]').check()
        page.locator('input[data-kind="unresolved-reference"]').uncheck()
        page.locator("#btn-direction").click()
        _assert_visible_layout(page, "BT")
        browser.close()
    assert not errors
    assert all(url.startswith("file:") for url in requests)


def test_comparison_revision_switches_retain_union_layout_and_side_edges(tmp_path: Path) -> None:
    """Revision controls hide side records without rerunning the union layout."""

    nodes = [
        _comparison_node("shared", label="shared", before_system="A", after_system="A"),
        _comparison_node("moved", label="moved", before_system="A", after_system="B"),
        _comparison_node(
            "departed",
            label="departed",
            status="removed",
            after=False,
            before_system="A",
            after_system=None,
        ),
        _comparison_node(
            "added",
            label="added",
            status="added",
            before=False,
            before_system=None,
            after_system="A",
        ),
    ]
    relationships = [
        _comparison_edge(
            "edge:stable",
            "shared",
            "moved",
            status="changed",
            before_systems=("A", "A"),
            after_systems=("A", "B"),
        ),
        _comparison_edge(
            "edge:departed",
            "shared",
            "departed",
            kind="references",
            status="removed",
            before_systems=("A", "A"),
            after=False,
        ),
        _comparison_edge(
            "edge:added",
            "shared",
            "added",
            kind="references",
            status="added",
            before=False,
            after_systems=("A", "A"),
        ),
    ]
    presentation = _comparison_presentation(nodes, relationships, changed=True)
    artifact = tmp_path / "comparison.html"
    artifact.write_bytes(render_html(presentation))

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(artifact.as_uri())
        page.wait_for_timeout(500)
        assert page.locator("#revision-control").is_visible()
        assert page.locator("#revision-view").locator("option").all_text_contents() == [
            "Combined",
            "Before",
            "After",
        ]
        page.locator("#system-filter").select_option("A")
        page.wait_for_timeout(500)
        page.locator("#cross-system-connections").check()
        page.wait_for_timeout(500)
        combined_containers = page.evaluate(
            """() => Object.fromEntries(window.minotaurVisualizer.cy.nodes('.system-container').map(
                node => [node.id(), {
                    visible: node.visible(),
                    box: node.boundingBox({includeLabels:true}),
                }]
            ))"""
        )
        assert combined_containers
        assert all(container["visible"] for container in combined_containers.values())
        drag = page.evaluate(
            """() => {
                const cy = window.minotaurVisualizer.cy;
                const node = cy.getElementById('shared');
                const point = node.renderedPosition();
                const bounds = cy.container().getBoundingClientRect();
                return {x: bounds.left + point.x, y: bounds.top + point.y};
            }"""
        )
        page.mouse.move(drag["x"], drag["y"])
        page.mouse.down()
        page.mouse.move(drag["x"] + 70, drag["y"] + 35)
        page.mouse.up()
        dragged_position = page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('shared').position()"
        )
        before_switch = page.evaluate(
            """() => ({
                runs: window.minotaurVisualizer.layoutRuns(),
                zoom: window.minotaurVisualizer.cy.zoom(),
                pan: window.minotaurVisualizer.cy.pan(),
                positions: Object.fromEntries(window.minotaurVisualizer.cy.nodes().map(
                    node => [node.id(), node.position()]
                )),
            })"""
        )
        assert abs(before_switch["positions"]["shared"]["x"] - dragged_position["x"]) < 0.01
        assert abs(before_switch["positions"]["shared"]["y"] - dragged_position["y"]) < 0.01
        page.locator("#revision-view").select_option("before")
        assert page.evaluate("window.minotaurVisualizer.layoutRuns()") == before_switch["runs"]
        before_containers = page.evaluate(
            """() => Object.fromEntries(window.minotaurVisualizer.cy.nodes('.system-container').map(
                node => [node.id(), {
                    visible: node.visible(),
                    box: node.boundingBox({includeLabels:true}),
                }]
            ))"""
        )
        assert before_containers.keys() == combined_containers.keys()
        assert all(container["visible"] for container in before_containers.values())
        assert before_containers == combined_containers
        assert page.evaluate(
            "window.minotaurVisualizer.cy.nodes(':visible').map(n => n.id()).sort()"
        ) == ["departed", "moved", "shared"]
        assert page.evaluate(
            "window.minotaurVisualizer.cy.edges(':visible').map(e => e.id()).sort()"
        ) == ["edge:departed", "edge:stable"]
        page.locator("#revision-view").select_option("after")
        assert page.evaluate("window.minotaurVisualizer.layoutRuns()") == before_switch["runs"]
        after_containers = page.evaluate(
            """() => Object.fromEntries(window.minotaurVisualizer.cy.nodes('.system-container').map(
                node => [node.id(), {
                    visible: node.visible(),
                    box: node.boundingBox({includeLabels:true}),
                }]
            ))"""
        )
        assert after_containers.keys() == combined_containers.keys()
        assert all(container["visible"] for container in after_containers.values())
        assert after_containers == combined_containers
        assert page.evaluate(
            "window.minotaurVisualizer.cy.nodes(':visible').map(n => n.id()).sort()"
        ) == ["added", "shared"]
        # The moved endpoint is no longer in A on the After side, so the
        # relationship cannot be presented as an invented A-internal edge.
        assert page.evaluate(
            "window.minotaurVisualizer.cy.edges(':visible').map(e => e.id()).sort()"
        ) == ["edge:added"]
        after_switch = page.evaluate(
            """() => ({
                zoom: window.minotaurVisualizer.cy.zoom(),
                pan: window.minotaurVisualizer.cy.pan(),
                positions: Object.fromEntries(window.minotaurVisualizer.cy.nodes().map(
                    node => [node.id(), node.position()]
                )),
            })"""
        )
        assert after_switch["zoom"] == before_switch["zoom"]
        assert after_switch["pan"] == before_switch["pan"]
        for node_id in ("shared", "moved"):
            assert after_switch["positions"][node_id] == before_switch["positions"][node_id]
        # Direction is an explicit layout action and uses the two-side union,
        # including the hidden departed/added records as layout inputs.
        page.locator("#btn-direction").click()
        page.wait_for_timeout(450)
        assert page.evaluate("window.minotaurVisualizer.layoutRuns()") > before_switch["runs"]
        browser.close()


def test_comparison_cross_system_classes_follow_revision_endpoint_membership(
    tmp_path: Path,
) -> None:
    nodes = [
        _comparison_node("m1", before_system="A", after_system="A"),
        _comparison_node("m2", status="changed", before_system="A", after_system="B"),
        _comparison_node("m3", before_system="B", after_system="B"),
        _comparison_node("m4", before_system=None, after_system=None),
        _comparison_node("m5", status="changed", before_system="B", after_system="A"),
    ]
    relationships = [
        _comparison_edge("e1", "m1", "m2", before_systems=("A", "A"), after_systems=("A", "B")),
        _comparison_edge("e2", "m3", "m2", status="added", before=False, after_systems=("B", "B")),
        _comparison_edge("e3", "m1", "m4", before_systems=("A", None), after_systems=("A", None)),
        _comparison_edge("e5", "m2", "m5", before_systems=("A", "B"), after_systems=("B", "A")),
    ]
    presentation = _comparison_presentation(nodes, relationships, changed=True)
    expected = {
        "": {
            "before": {"e1": False, "e2": False, "e3": False, "e5": True},
            "after": {"e1": True, "e2": False, "e3": False, "e5": True},
            "combined": {"e1": False, "e2": False, "e3": False, "e5": True},
        },
        "A": {
            "before": {"e1": False, "e2": False, "e3": True, "e5": True},
            "after": {"e1": True, "e2": False, "e3": True, "e5": True},
            "combined": {"e1": False, "e2": False, "e3": True, "e5": True},
        },
        "B": {
            "before": {"e1": False, "e2": False, "e3": False, "e5": True},
            "after": {"e1": True, "e2": False, "e3": False, "e5": True},
            "combined": {"e1": True, "e2": False, "e3": False, "e5": True},
        },
    }
    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        _open_comparison(page, tmp_path, presentation, "cross-system.html")
        for system, views in expected.items():
            page.locator("#system-filter").select_option(system)
            if system:
                page.locator("#cross-system-connections").check()
            for view, classes in views.items():
                page.locator("#revision-view").select_option(view)
                actual = page.evaluate(
                    """() => Object.fromEntries(window.minotaurVisualizer.cy.edges().map(
                        edge => [edge.id(), edge.hasClass('cross-system')]
                    ))"""
                )
                assert actual == classes, (system, view)
                assert page.evaluate(
                    """() => window.minotaurVisualizer.cy.nodes().not('.system-container').every(
                        node => !Object.hasOwn(node.data(), 'system')
                            && !Object.hasOwn(node.data(), 'systems')
                    )"""
                )
        browser.close()


def test_comparison_empty_filter_view_keeps_controls_safe(tmp_path: Path) -> None:
    """An empty comparison view remains safe for reset, direction, and switches."""

    nodes = [
        _comparison_node(
            "departed", status="removed", after=False, before_system="A", after_system=None
        ),
        _comparison_node(
            "added", status="added", before=False, before_system=None, after_system="A"
        ),
    ]
    presentation = _comparison_presentation(nodes, [], changed=True)
    artifact = tmp_path / "empty-comparison.html"
    artifact.write_bytes(render_html(presentation))
    errors: list[str] = []

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(artifact.as_uri())
        page.wait_for_timeout(500)
        page.locator('input[data-kind="symbol"]').uncheck()
        page.wait_for_timeout(250)
        camera = page.evaluate(
            """() => ({
                zoom: window.minotaurVisualizer.cy.zoom(),
                pan: window.minotaurVisualizer.cy.pan(),
            })"""
        )
        assert page.evaluate("window.minotaurVisualizer.cy.nodes(':visible').length") == 0
        page.locator("#btn-fit").click()
        page.locator("#btn-direction").click()
        page.locator("#revision-view").select_option("before")
        page.locator("#revision-view").select_option("after")
        assert page.evaluate("window.minotaurVisualizer.cy.nodes(':visible').length") == 0
        assert (
            page.evaluate(
                """() => ({
                    zoom: window.minotaurVisualizer.cy.zoom(),
                    pan: window.minotaurVisualizer.cy.pan(),
                })"""
            )
            == camera
        )
        browser.close()

    assert not errors


# --- Comparison emphasis, filters, selection/search priority, and details ---


def _comparison_location(path: str, line: int, length: int = 4) -> dict[str, object]:
    return {
        "path": path,
        "range": {
            "start": {"line": line, "character": 0},
            "end": {"line": line, "character": length},
        },
    }


def _comparison_node(
    node_id: str,
    *,
    status: str = "unchanged",
    label: str | None = None,
    before: bool = True,
    after: bool = True,
    path: str | None = None,
    line: int = 0,
    before_system: str | None = "A",
    after_system: str | None = "A",
) -> GraphNodeChange:
    """Build one comparison node through the production side-record shape.

    Each side is ``{node, system}`` exactly as the comparator emits it, so a
    fixture cannot inject a per-side ``systems`` field that production never
    writes.
    """

    def side(system: str | None) -> dict[str, object]:
        return {
            "node": {
                "node_class": "symbol",
                "label": label or node_id,
                "symbol_kind": "function",
                "location": _comparison_location(path or f"{node_id}.py", line),
            },
            "system": system,
        }

    reasons = [status] if status != "unchanged" else []
    involved = sorted({name for name in (before_system, after_system) if name})
    return GraphNodeChange(
        node_id,
        status,
        tuple(reasons),
        tuple(involved),
        side(before_system) if before else None,
        side(after_system) if after else None,
    )


def _comparison_edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    status: str = "unchanged",
    kind: str = "calls",
    before: bool = True,
    after: bool = True,
    before_systems: tuple[str | None, str | None] = ("A", "A"),
    after_systems: tuple[str | None, str | None] = ("A", "A"),
) -> GraphRelationshipChange:
    payload = {"source": source, "target": target, "kind": kind, "evidence": []}
    involved = sorted({name for pair in (before_systems, after_systems) for name in pair if name})
    return GraphRelationshipChange(
        edge_id,
        source,
        target,
        kind,
        status,
        tuple([status] if status != "unchanged" else []),
        tuple(involved),
        dict(payload) if before else None,
        dict(payload) if after else None,
        endpoint_eligibility(
            (before_systems,) if before else (),
            (after_systems,) if after else (),
        ),
    )


def _comparison_presentation(
    nodes: list[GraphNodeChange],
    relationships: list[GraphRelationshipChange],
    *,
    changed: bool,
    calls: list[dict[str, object]] | None = None,
    excerpts: dict[str, object] | None = None,
    added_systems: list[str] | None = None,
    limitations: list[dict[str, object]] | None = None,
    revisions: dict[str, str] | None = None,
) -> dict[str, object]:
    """Serialize a comparison fixture through the production presentation builder.

    ``changed`` is retained at the call sites for documentation value; the
    complete-result boolean is derived from the stored records exactly as it is
    in production.
    """
    del changed
    revision_names = revisions or {"old": "before-sha", "new": "after-sha"}
    call_changes = tuple(
        CallChange(
            str(call.get("relationship_id") or call.get("id")),
            str(call.get("status", "unchanged")),
            tuple(str(reason) for reason in call.get("reasons", ())),
            tuple(str(name) for name in call.get("involved_systems", ())),
            call.get("before"),
            call.get("after"),
        )
        for call in (calls or [])
    )
    system_names = {name for item in (*nodes, *relationships) for name in item.involved_systems}
    result = SystemDiffResult(
        old_system_names=tuple(sorted(system_names)),
        new_system_names=tuple(sorted(system_names)),
        added_systems=tuple(added_systems or ()),
        nodes=tuple(nodes),
        relationships=tuple(relationships),
        call_changes=call_changes,
        limitations=tuple(
            CallLimitation(
                str(item.get("side", "")),
                item.get("relationship_id"),  # type: ignore[arg-type]
                str(item.get("code", "")),
                str(item.get("message", "")),
            )
            for item in (limitations or [])
        ),
        old_revision=revision_names.get("old"),
        new_revision=revision_names.get("new"),
    )
    return build_comparison_presentation(result, excerpts=excerpts)


@pytest.mark.parametrize("comparison", [False, True], ids=["ordinary", "comparison"])
def test_script_tokenizer_sequences_in_payload_preserve_viewer(
    tmp_path: Path, comparison: bool
) -> None:
    text = "<!--<script></script>"
    excerpts = {
        "paths": {text: {"status": "available", "spans": [{"start": 0, "lines": [text]}]}},
        "call_sites": {},
    }
    if comparison:
        presentation = _comparison_presentation(
            [
                _comparison_node(
                    "inert-node",
                    label=text,
                    path=text,
                    before_system=text,
                    after_system=text,
                )
            ],
            [],
            changed=False,
            excerpts={"before": excerpts, "after": excerpts},
        )
    else:
        graph = json.loads((ROOT / "examples/synthetic-graphs/small-workflow.json").read_text())
        graph["nodes"][0]["label"] = text
        graph["nodes"][0]["location"]["path"] = text
        presentation = build_presentation(graph, excerpts)
        presentation["systems"] = [text]
    artifact = tmp_path / "inert.html"
    artifact.write_bytes(render_html(presentation))

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(artifact.as_uri())
        assert page.evaluate("typeof window.minotaurVisualizer !== 'undefined'")
        assert page.evaluate("window.minotaurVisualizer.cy.nodes().length") == len(
            presentation["graph"]["nodes"]
        )
        assert page.evaluate("document.scripts.length") == 4
        browser.close()


def _open_comparison(
    page: object, tmp_path: Path, presentation: dict[str, object], name: str
) -> None:
    artifact = tmp_path / name
    artifact.write_bytes(render_html(presentation))
    page.goto(artifact.as_uri())
    page.wait_for_timeout(450)


def _click_node_by_id(page: object, node_id: str) -> None:
    point = page.evaluate(
        """(id) => {
            const node = window.minotaurVisualizer.cy.getElementById(id);
            const point = node.renderedPosition();
            const bounds = window.minotaurVisualizer.cy.container().getBoundingClientRect();
            return {x: bounds.left + point.x, y: bounds.top + point.y};
        }""",
        node_id,
    )
    page.mouse.click(point["x"], point["y"])


def _click_edge_by_id(page: object, edge_id: str) -> None:
    point = page.evaluate(
        """(id) => {
            const edge = window.minotaurVisualizer.cy.getElementById(id);
            const point = edge.renderedMidpoint();
            const bounds = window.minotaurVisualizer.cy.container().getBoundingClientRect();
            return {x: bounds.left + point.x, y: bounds.top + point.y};
        }""",
        edge_id,
    )
    page.mouse.click(point["x"], point["y"])


def _node_opacity(page: object, node_id: str) -> float:
    return page.evaluate(
        "(id) => window.minotaurVisualizer.cy.getElementById(id).pstyle('opacity').pfValue",
        node_id,
    )


def _comparison_camera(page: object) -> dict[str, object]:
    return page.evaluate(
        """() => ({
            zoom: window.minotaurVisualizer.cy.zoom(),
            pan: window.minotaurVisualizer.cy.pan(),
            positions: Object.fromEntries(window.minotaurVisualizer.cy.nodes().map(
                node => [node.id(), node.position()]
            )),
            highlighted: window.minotaurVisualizer.cy.elements('.highlighted').map(
                element => element.id()
            ).sort(),
        })"""
    )


def test_comparison_emphasis_search_and_selection_priority(tmp_path: Path) -> None:
    """Selection outranks search, search outranks emphasis, and clearing restores."""
    nodes = [
        _comparison_node("changed-node", status="changed", label="changed-node"),
        _comparison_node("steady-node", label="steady-node"),
        _comparison_node("lonely-node", label="lonely-node"),
    ]
    relationships = [
        _comparison_edge("edge:changed", "changed-node", "steady-node", status="changed"),
        _comparison_edge(
            "edge:steady", "steady-node", "lonely-node", status="unchanged", kind="references"
        ),
    ]
    presentation = _comparison_presentation(nodes, relationships, changed=True)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "priority.html")

        assert page.locator("#emphasis-control").is_visible()
        assert page.locator("#emphasis-changes").is_checked()
        assert page.locator("#emphasis-changes").is_enabled()

        # Changed structure and both endpoints keep full opacity; isolated
        # unchanged structure uses the existing de-emphasis treatment.
        assert _node_opacity(page, "changed-node") == 1
        assert _node_opacity(page, "steady-node") == 1
        assert _node_opacity(page, "lonely-node") < 1
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('edge:changed').hasClass('dimmed')"
        )
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('edge:steady').hasClass('dimmed')"
        )
        # Emphasis changes opacity only: node colors keep their class meaning.
        colors = page.evaluate(
            """() => ({
                changed: window.minotaurVisualizer.cy
                    .getElementById('changed-node').pstyle('background-color').strValue,
                unchanged: window.minotaurVisualizer.cy
                    .getElementById('lonely-node').pstyle('background-color').strValue,
            })"""
        )
        assert colors["changed"] == colors["unchanged"]

        # Unchecking the control restores normal opacity.
        page.locator("#emphasis-changes").uncheck()
        assert _node_opacity(page, "lonely-node") == 1
        page.locator("#emphasis-changes").check()
        assert _node_opacity(page, "lonely-node") < 1

        # A nonempty search temporarily takes priority over change emphasis.
        page.locator("#search").fill("lonely")
        page.wait_for_function(
            "() => window.minotaurVisualizer.cy.getElementById('lonely-node')"
            ".hasClass('highlighted')"
        )
        assert _node_opacity(page, "steady-node") < 1

        # Selection takes priority over an active search.
        _click_node_by_id(page, "steady-node")
        page.wait_for_function(
            "() => window.minotaurVisualizer.cy.getElementById('steady-node')"
            ".hasClass('highlighted')"
        )
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('steady-node').hasClass('dimmed')"
        )
        assert _node_opacity(page, "steady-node") == 1

        # Clearing selection restores the still-active search.
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => window.minotaurVisualizer.cy.getElementById('lonely-node')"
            ".hasClass('highlighted')"
        )
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('steady-node').hasClass('dimmed')"
        )

        # Clearing search while a selection is active does not override it.
        _click_node_by_id(page, "steady-node")
        page.wait_for_function(
            "() => window.minotaurVisualizer.cy.getElementById('steady-node')"
            ".hasClass('highlighted')"
        )
        page.locator("#search").fill("")
        page.wait_for_timeout(300)
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('steady-node')"
            ".hasClass('highlighted')"
        )
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('steady-node').hasClass('dimmed')"
        )

        # Clearing selection restores change emphasis. The first Escape returns
        # focus from the search field; the second clears the selection.
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        assert _node_opacity(page, "lonely-node") < 1
        assert _node_opacity(page, "steady-node") == 1

        # A blank search is inactive rather than a match-everything search.
        page.locator("#search").fill("   ")
        page.wait_for_timeout(250)
        assert _node_opacity(page, "lonely-node") < 1
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.nodes().some(n => n.hasClass('highlighted'))"
        )
        browser.close()


def test_comparison_no_change_state_disables_emphasis(tmp_path: Path) -> None:
    """A complete identical comparison shows normal opacity and a disabled control."""
    nodes = [_comparison_node("again"), _comparison_node("also")]
    relationships = [_comparison_edge("edge:same", "again", "also")]
    presentation = _comparison_presentation(nodes, relationships, changed=False)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "no-change.html")

        assert page.locator("#emphasis-changes").is_disabled()
        assert not page.locator("#emphasis-changes").is_checked()
        assert page.locator("#comparison-no-change").is_visible()
        assert page.locator("#comparison-no-change").inner_text() == "No structural changes found"
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.nodes().every(n => !n.hasClass('dimmed'))"
        )
        assert _node_opacity(page, "again") == 1
        browser.close()


def test_comparison_call_only_change_prevents_no_change_state(tmp_path: Path) -> None:
    """A stored call-expression change is visible even when no structure changed."""
    nodes = [_comparison_node("again"), _comparison_node("also")]
    relationships = [_comparison_edge("edge:same", "again", "also")]
    calls = [
        {
            "id": "edge:same",
            "relationship_id": "edge:same",
            "status": "changed",
            "reasons": ["expression_changed"],
            "involved_systems": ["A"],
        }
    ]
    presentation = _comparison_presentation(nodes, relationships, changed=True, calls=calls)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "call-only.html")

        assert page.locator("#emphasis-changes").is_enabled()
        assert page.locator("#emphasis-changes").is_checked()
        assert not page.locator("#comparison-no-change").is_visible()
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('edge:same').hasClass('dimmed')"
        )
        assert _node_opacity(page, "again") == 1

        # Hiding the change with a filter does not imply comparison-wide equality.
        page.locator('input[data-edgekind="calls"]').uncheck()
        page.wait_for_timeout(250)
        assert page.locator("#emphasis-changes").is_enabled()
        assert not page.locator("#comparison-no-change").is_visible()
        assert "call Changed: edge:same" in page.locator("#comparison-summary-body").evaluate(
            "element => element.textContent"
        )
        browser.close()


def test_comparison_summary_lists_change_without_drawable_node(tmp_path: Path) -> None:
    """An added empty system stays discoverable through the category summary."""
    presentation = _comparison_presentation([], [], changed=True, added_systems=["empty-system"])

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "empty-system.html")

        assert page.evaluate("() => window.minotaurVisualizer.cy.nodes().length") == 0
        assert page.locator("#comparison-header").is_visible()
        assert page.locator("#comparison-summary").evaluate("e => e.tagName") == "DETAILS"
        page.locator("#comparison-summary summary").click()
        assert page.locator("#comparison-summary-body").is_visible()
        body = page.locator("#comparison-summary-body").inner_text()
        assert "system added: empty-system" in body
        assert page.locator("#emphasis-changes").is_enabled()
        browser.close()


def test_comparison_header_shows_captured_historical_revision_identities() -> None:
    """The saved historical report visibly names both resolved revisions."""
    artifact = ROOT / "examples" / "system-walkthrough" / "minotaur-comparison.html"

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(artifact.as_uri())
        page.wait_for_function("() => window.minotaurVisualizer?.cy")

        header = page.locator("#comparison-header")
        assert header.is_visible()
        revisions = page.locator("#comparison-revisions")
        assert revisions.is_visible()
        assert page.locator(".comparison-revision").all_inner_texts() == [
            "Before: v1.0 · 134b138",
            "After: v2.0 · a2b78dd",
        ]
        browser.close()


@pytest.mark.parametrize("artifact_kind", ["committed", "fresh"])
def test_ordinary_explorer_hides_comparison_controls(tmp_path: Path, artifact_kind: str) -> None:
    artifact = ROOT / "examples/python-workflow/minotaur-graph.html"
    if artifact_kind == "fresh":
        html = artifact.read_text(encoding="utf-8")
        prefix = '<script id="minotaur-presentation" type="application/json">'
        presentation = json.loads(html.split(prefix, 1)[1].split("</script>", 1)[0])
        artifact = tmp_path / "explorer.html"
        artifact.write_bytes(render_html(presentation))

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        page.goto(artifact.as_uri())
        page.wait_for_function("() => window.minotaurVisualizer?.cy")
        for selector in ("#revision-control", "#emphasis-control"):
            control = page.locator(selector)
            assert control.evaluate("element => getComputedStyle(element).display") == "none"
            assert not control.is_visible()
        browser.close()


@pytest.mark.parametrize("empty_revisions", [False, True], ids=["named", "empty"])
def test_comparison_reveals_controls_and_hides_empty_revisions(
    tmp_path: Path, empty_revisions: bool
) -> None:
    presentation = _comparison_presentation(
        [_comparison_node("node")],
        [],
        changed=False,
        revisions={"old": "", "new": ""} if empty_revisions else {"old": "old", "new": "new"},
    )
    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page()
        _open_comparison(page, tmp_path, presentation, "comparison-controls.html")
        for selector in ("#revision-control", "#emphasis-control"):
            control = page.locator(selector)
            assert control.is_visible()
            assert control.evaluate("element => getComputedStyle(element).display") == "flex"
        revisions = page.locator("#comparison-revisions")
        assert revisions.is_visible() is not empty_revisions
        assert revisions.evaluate("element => getComputedStyle(element).display") == (
            "none" if empty_revisions else "flex"
        )
        browser.close()


def test_graph_only_artifact_hides_comparison_revision_header() -> None:
    """An ordinary graph view never shows comparison-only revision identity."""
    artifact = ROOT / "examples" / "system-walkthrough" / "minotaur-graph.html"

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(artifact.as_uri())
        page.wait_for_function("() => window.minotaurVisualizer?.cy")

        assert not page.locator("#comparison-header").is_visible()
        assert not page.locator("#comparison-revisions").is_visible()
        browser.close()


def test_comparison_header_labels_working_tree_without_a_commit_id(tmp_path: Path) -> None:
    """The working-tree side is labeled, never given a fabricated commit ID."""
    presentation = _comparison_presentation(
        [],
        [],
        changed=True,
        revisions={"old": "HEAD · 134b138", "new": "Working tree at report generation"},
    )

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "working-tree-header.html")

        assert page.locator("#comparison-revisions").is_visible()
        assert page.locator(".comparison-revision").all_inner_texts() == [
            "Before: HEAD · 134b138",
            "After: Working tree at report generation",
        ]
        after = page.locator(".comparison-revision").nth(1).inner_text()
        assert after == "After: Working tree at report generation"
        assert "·" not in after
        browser.close()


def test_comparison_source_revision_keeps_graph_and_per_side_state(tmp_path: Path) -> None:
    """One excerpt region switches sides without moving the graph or losing state."""
    nodes = [
        _comparison_node("caller", path="app.py", line=0),
        _comparison_node("callee", path="app.py", line=5),
    ]
    relationships = [_comparison_edge("edge:call", "caller", "callee", status="changed")]
    calls = [
        {
            "id": "edge:call",
            "relationship_id": "edge:call",
            "status": "changed",
            "reasons": ["expression_changed"],
            "involved_systems": ["A"],
        }
    ]
    excerpts = {
        "before": {
            "paths": {
                "app.py": {
                    "status": "available",
                    "spans": [{"start": 0, "lines": [f"before {i}" for i in range(8)]}],
                }
            },
            "call_sites": {
                "edge:call": [
                    {
                        "location": _comparison_location("app.py", 1),
                        "provenance": ["old-proof"],
                    }
                ]
            },
        },
        "after": {
            "paths": {
                "app.py": {
                    "status": "available",
                    "spans": [{"start": 0, "lines": [f"after {i}" for i in range(8)]}],
                }
            },
            "call_sites": {
                "edge:call": [
                    {
                        "location": _comparison_location("app.py", 2),
                        "provenance": ["new-proof"],
                    },
                    {
                        "location": _comparison_location("app.py", 4),
                        "provenance": ["new-proof"],
                    },
                ]
            },
        },
    }
    presentation = _comparison_presentation(
        nodes, relationships, changed=True, calls=calls, excerpts=excerpts
    )

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "source-revision.html")

        _click_edge_by_id(page, "edge:call")
        page.wait_for_selector("#source-revision")
        assert page.locator("#source-revision").locator("option").all_text_contents() == [
            "Before",
            "After",
        ]
        assert page.locator("#source-revision").input_value() == "after"
        assert page.locator(".code-excerpt").count() == 1
        assert "after 2" in page.locator("#call-site-detail").inner_text()
        assert "Captured After revision: after-sha" in (
            page.locator("#call-site-detail").inner_text()
        )
        assert (
            page.locator(".code-excerpt").evaluate("e => getComputedStyle(e).maxHeight") == "420px"
        )

        # Each side remembers its own selected call site.
        page.locator("#call-site-select").select_option("1")
        assert "after 4" in page.locator("#call-site-detail").inner_text()

        before_state = _comparison_camera(page)
        page.locator("#source-revision").focus()
        page.locator("#source-revision").select_option("before")
        assert page.locator(".code-excerpt").count() == 1
        assert "before 1" in page.locator("#call-site-detail").inner_text()
        assert "Captured Before revision: before-sha" in (
            page.locator("#call-site-detail").inner_text()
        )
        # Switching source revision must not steal focus from the control.
        assert page.evaluate("() => document.activeElement.id") == "source-revision"
        assert _comparison_camera(page) == before_state

        page.locator("#source-revision").select_option("after")
        assert page.locator("#call-site-select").input_value() == "1"
        assert "after 4" in page.locator("#call-site-detail").inner_text()
        browser.close()


def test_comparison_missing_side_and_hidden_selection(tmp_path: Path) -> None:
    """Removed edges default Before, a missing side reads Not present, and a
    revision switch that hides the selection restores the empty panel."""
    nodes = [
        _comparison_node("caller", path="app.py", line=0),
        _comparison_node("target", path="app.py", line=5),
        _comparison_node("after-added", status="added", before=False),
    ]
    relationships = [
        _comparison_edge("edge:call", "caller", "target", status="changed"),
        _comparison_edge(
            "edge:removed", "caller", "target", status="removed", before=True, after=False
        ),
    ]
    calls = [
        {
            "id": "edge:call",
            "relationship_id": "edge:call",
            "status": "changed",
            "reasons": ["expression_changed"],
            "involved_systems": ["A"],
        },
        {
            "id": "edge:removed",
            "relationship_id": "edge:removed",
            "status": "removed",
            "reasons": ["removed"],
            "involved_systems": ["A"],
        },
    ]
    excerpts = {
        "before": {
            "paths": {
                "app.py": {
                    "status": "available",
                    "spans": [{"start": 0, "lines": [f"line {i}" for i in range(8)]}],
                }
            },
            "call_sites": {
                "edge:call": [
                    {
                        "location": _comparison_location("app.py", 1),
                        "provenance": ["old-proof"],
                    }
                ],
                "edge:removed": [
                    {
                        "location": _comparison_location("app.py", 1),
                        "provenance": ["old-proof"],
                    }
                ],
            },
        },
        "after": {
            "paths": {
                "app.py": {
                    "status": "unavailable",
                    "reason": "captured source bytes are unavailable",
                }
            },
            "call_sites": {},
        },
    }
    presentation = _comparison_presentation(
        nodes, relationships, changed=True, calls=calls, excerpts=excerpts
    )

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "missing-side.html")

        # An unchanged endpoint of a changed relationship keeps Unchanged status.
        _click_node_by_id(page, "target")
        page.wait_for_function(
            "() => document.querySelector('#detail-content').innerText.includes('Unchanged')"
        )
        assert (
            "Unchanged · Connected to a changed relationship"
            in page.locator("#detail-content").inner_text()
        )

        # A present side whose captured bytes are missing says Source unavailable.
        _click_edge_by_id(page, "edge:call")
        page.wait_for_selector("#source-revision")
        assert page.locator("#source-revision").input_value() == "after"
        assert "Source unavailable" in page.locator("#comparison-side-detail").inner_text()
        page.locator("#source-revision").select_option("before")
        assert "line 1" in page.locator("#call-site-detail").inner_text()

        # A removed edge defaults to Before and reports a missing side as absent.
        _click_edge_by_id(page, "edge:removed")
        page.wait_for_selector("#source-revision")
        assert page.locator("#source-revision").input_value() == "before"
        page.locator("#source-revision").select_option("after")
        missing = page.locator("#comparison-side-detail").inner_text()
        assert "Not present" in missing
        assert "Source unavailable" not in missing

        # A revision switch that hides the selection deselects it and restores
        # the ordinary empty panel without a special absence message.
        _click_node_by_id(page, "after-added")
        page.wait_for_function(
            "() => document.querySelector('#detail-content').innerText.includes('after-added')"
        )
        page.locator("#revision-view").select_option("before")
        page.wait_for_function(
            "() => document.querySelector('#detail-content')"
            ".innerText.includes('Select a node or edge')"
        )
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('after-added')"
            ".hasClass('revision-hidden')"
        )
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.nodes(':visible')"
            ".some(n => n.id() === 'after-added')"
        )
        browser.close()


def test_comparison_search_does_not_reveal_revision_hidden_items(tmp_path: Path) -> None:
    """Search respects the active revision view and cannot reveal absent items."""
    nodes = [
        _comparison_node("present"),
        _comparison_node("after-added", status="added", before=False),
    ]
    presentation = _comparison_presentation(nodes, [], changed=True)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "search-hidden.html")

        page.locator("#revision-view").select_option("before")
        page.wait_for_timeout(250)
        page.locator("#search").fill("after-added")
        page.wait_for_timeout(250)
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('after-added')"
            ".hasClass('revision-hidden')"
        )
        assert _node_opacity(page, "after-added") == 0
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.nodes(':visible')"
            ".some(n => n.id() === 'after-added')"
        )
        browser.close()


def test_comparison_details_state_stored_status_text(tmp_path: Path) -> None:
    """Details label each stored classification instead of only dimming it."""
    nodes = [
        _comparison_node("changed-node", status="changed"),
        _comparison_node("added-node", status="added", before=False),
        _comparison_node("removed-node", status="removed", after=False),
    ]
    relationships = [
        _comparison_edge(
            "edge:removed",
            "changed-node",
            "removed-node",
            status="removed",
            before=True,
            after=False,
        ),
    ]
    presentation = _comparison_presentation(nodes, relationships, changed=True)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "status-text.html")

        _click_node_by_id(page, "changed-node")
        page.wait_for_function(
            "() => document.querySelector('#detail-content').innerText.includes('Changed')"
        )
        changed = page.locator("#detail-content").inner_text()
        assert "Changed" in changed
        assert "Not present" not in changed

        _click_node_by_id(page, "added-node")
        page.wait_for_function(
            "() => document.querySelector('#detail-content').innerText.includes('Added')"
        )
        added = page.locator("#detail-content").inner_text()
        assert "Added" in added
        assert "Not present" in added

        _click_node_by_id(page, "removed-node")
        page.wait_for_function(
            "() => document.querySelector('#detail-content').innerText.includes('Removed')"
        )
        removed = page.locator("#detail-content").inner_text()
        assert "Removed" in removed
        assert "Not present" in removed

        _click_edge_by_id(page, "edge:removed")
        page.wait_for_selector("#source-revision")
        edge_detail = page.locator("#detail-content").inner_text()
        assert "Removed" in edge_detail
        assert "Not present" in edge_detail
        assert page.locator("#source-revision").input_value() == "before"
        browser.close()


def test_comparison_unavailable_evidence_is_limitation_not_change(tmp_path: Path) -> None:
    """Unavailable call evidence is neither emphasized nor claimed as equality."""
    nodes = [
        _comparison_node("changed-node", status="changed"),
        _comparison_node("kept"),
        _comparison_node("also-kept"),
    ]
    relationships = [_comparison_edge("edge:u", "kept", "also-kept")]
    calls = [
        {
            "id": "edge:u",
            "relationship_id": "edge:u",
            "status": "unavailable",
            "reasons": ["unavailable"],
            "involved_systems": ["A"],
        }
    ]
    limitations = [
        {
            "side": "old",
            "relationship_id": "edge:u",
            "code": "call-expression-unavailable",
            "message": "structural call expression was unavailable for one or more observations",
        }
    ]
    presentation = _comparison_presentation(
        nodes,
        relationships,
        changed=True,
        calls=calls,
        limitations=limitations,
    )

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "unavailable-evidence.html")

        assert page.locator("#emphasis-changes").is_enabled()
        assert page.locator("#comparison-no-change").is_hidden()
        assert page.locator("#comparison-notices").is_visible()
        assert "call-expression-unavailable" in page.locator("#comparison-notices").inner_text()

        # The unavailable call residual is not a change: its unchanged endpoints
        # stay de-emphasized while the stored changed node keeps full opacity.
        assert _node_opacity(page, "changed-node") == 1
        assert _node_opacity(page, "kept") < 1
        assert _node_opacity(page, "also-kept") < 1

        _click_edge_by_id(page, "edge:u")
        page.wait_for_function(
            "() => document.querySelector('#detail-content')"
            ".innerText.includes('Call-expression comparison unavailable')"
        )
        assert (
            "Call-expression comparison unavailable" in page.locator("#detail-content").inner_text()
        )
        browser.close()


def test_comparison_no_change_message_qualifies_unavailable_evidence(tmp_path: Path) -> None:
    """The no-change message and notice stay qualified when evidence is missing."""
    nodes = [_comparison_node("a"), _comparison_node("b")]
    relationships = [_comparison_edge("edge:u", "a", "b")]
    calls = [
        {
            "id": "edge:u",
            "relationship_id": "edge:u",
            "status": "unavailable",
            "reasons": ["unavailable"],
            "involved_systems": ["A"],
        }
    ]
    limitations = [
        {
            "side": "old",
            "relationship_id": "edge:u",
            "code": "call-expression-unavailable",
            "message": "structural call expression was unavailable for one or more observations",
        }
    ]
    presentation = _comparison_presentation(
        nodes,
        relationships,
        changed=False,
        calls=calls,
        limitations=limitations,
    )

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "qualified-no-change.html")

        assert page.locator("#emphasis-changes").is_disabled()
        message = page.locator("#comparison-no-change").inner_text()
        assert message.startswith("No structural changes found")
        assert "Call-expression comparison unavailable" in message
        assert page.locator("#comparison-notices").is_visible()
        assert "structural call expression was unavailable" in (
            page.locator("#comparison-notices").inner_text()
        )
        browser.close()


def test_comparison_hidden_selection_restores_change_emphasis(tmp_path: Path) -> None:
    """Deselecting a hidden item restores emphasis, not the selection treatment."""
    nodes = [
        _comparison_node("caller"),
        _comparison_node("target"),
        _comparison_node("after-added", status="added", before=False),
    ]
    presentation = _comparison_presentation(nodes, [], changed=True)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _open_comparison(page, tmp_path, presentation, "restore-emphasis.html")

        _click_node_by_id(page, "after-added")
        page.wait_for_function(
            "() => document.querySelector('#detail-content').innerText.includes('after-added')"
        )
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('caller').hasClass('faded')"
        )

        page.locator("#revision-view").select_option("before")
        page.wait_for_function(
            "() => document.querySelector('#detail-content')"
            ".innerText.includes('Select a node or edge')"
        )
        assert page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('caller').hasClass('dimmed')"
        )
        assert not page.evaluate(
            "() => window.minotaurVisualizer.cy.getElementById('caller').hasClass('faded')"
        )
        assert _node_opacity(page, "caller") < 0.5
        browser.close()


def _system_comparison_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, system: str | None
) -> Path:
    """Generate a real comparison artifact through the public systems command."""
    root = tmp_path / "focused-repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n    return receive()\n", encoding="utf-8"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "app"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "App"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Browser tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)

    artifact = tmp_path / "focused-comparison.html"
    monkeypatch.chdir(root)
    arguments = ["query", "diff", "--systems", "--html", str(artifact)]
    if system is not None:
        arguments[3:3] = ["--system", system]
    assert cli.main(arguments) == 0
    return artifact


def test_cli_selected_system_focuses_the_saved_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-08: a `--system NAME --html` artifact opens focused on NAME."""
    artifact = _system_comparison_artifact(tmp_path, monkeypatch, system="App")

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(artifact.as_uri())
        page.wait_for_timeout(400)

        assert page.locator("#system-filter option").all_text_contents() == [
            "All Systems",
            "App",
        ]
        assert page.locator("#system-filter").input_value() == "App"

        # All Systems remains selectable after the focused initial state.
        page.locator("#system-filter").select_option("")
        page.wait_for_timeout(200)
        assert page.locator("#system-filter").input_value() == ""
        browser.close()


def test_unselected_system_leaves_the_comparison_filter_on_all_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `--system` the saved artifact still opens on All Systems."""
    artifact = _system_comparison_artifact(tmp_path, monkeypatch, system=None)

    with sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(artifact.as_uri())
        page.wait_for_timeout(400)

        assert page.locator("#system-filter").input_value() == ""
        browser.close()
