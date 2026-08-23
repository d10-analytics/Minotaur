# Portable HTML visualization

Create a local, standalone graph explorer from a validated Minotaur graph:

```bash
minotaur visualize --input graph.json --output graph.html --source-root path/to/source
```

The command loads the graph model, performs semantic validation, and
canonicalizes it before writing output atomically. A matching sidecar digest
skips the JSON Schema pass; use `--validate` to force that pass regardless of
sidecar state. It refuses an existing destination unless `--force` is
supplied, and refuses an output that aliases the input graph.

The generated file has no external scripts, stylesheets, editor links, or
network requests. It includes Cytoscape 3.34.0 and Cytoscape-Dagre 4.0.0;
their source URLs, licenses, and checksums are recorded beside the vendored
assets. Use the controls to filter node classes and relationship kinds,
search labels/paths/references, fit the canvas, and switch between top-down
and left-to-right layout.

The left details panel stays visible while the graph is explored. Click a node
or edge to populate it; click the canvas or press Escape to clear it. Drag its
full-height right-hand divider, or use its arrow/Home/End keys, to adjust its
width. Node connections use separate relationship and target rows so long
qualified names wrap within the chosen width.

## Color modes

The top navigation bar provides System, Light, Catppuccin Mocha, Nord Polar
Night, and Solarized Dark modes. System follows the operating system's current
light/dark preference; the selector changes only the open artifact and stores
no setting. Every mode updates both the surrounding interface and the rendered
graph, so filter swatches remain equal to their edge colors. Selected edges are
always red, and yellow is intentionally excluded from node and edge colors.

The named modes use the published [Catppuccin palette](https://catppuccin.com/palette/),
[Nord palette](https://www.nordtheme.com/docs/colors-and-palettes/), and
[Solarized palette](https://ethanschoonover.com/solarized/) as design sources.

When a source root is available, the artifact embeds only the merged evidence
spans plus up to 50 lines of surrounding context; `calls` edges also retain
their call-site associations and, when known, the caller start for the
viewer’s two context modes. Omit `--source-root` when
the downloaded file must contain no source text. Missing, unreadable,
non-UTF-8, and escaping-symlink paths are not embedded. The command warns,
while still writing successfully, when the artifact exceeds 10 MiB.
