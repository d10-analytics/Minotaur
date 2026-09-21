# Portable HTML visualization

Create a local, standalone graph explorer from a validated Minotaur graph:

```bash
minotaur visualize --input graph.json --output graph.html --source-root path/to/source
```

This direct form needs no project configuration. It displays the graph and,
when `--source-root` is supplied, the source evidence attached to its
connections.

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

## Explore configured systems

To include named systems, run the command inside a configured project or point
to its configuration explicitly:

```bash
minotaur visualize --config /path/to/project/.minotaur.toml --output graph.html
```

The configuration tells Minotaur which graph, source root, and system directory
belong together. This avoids showing a graph with definitions from the wrong
project. See [Project configuration](project-configuration.md) for the common
repository layout and alternatives to `docs/systems`.

When the visualization is created inside a configured project with committed
system definitions, the System menu offers **All Systems** and every declared
system. All Systems keeps the complete graph and draws relationships between
different systems as thick red edges. Selecting one system initially shows only
that system's nodes and internal relationships, without containers. Enable
**Show Cross-System Connections** to add directly connected outside nodes, mark
them with thick red borders, and mark their boundary-crossing relationships
with thick red edges. The selected system is then grouped and centered inside a
labeled container. Each outside system receives its own labeled,
deterministically colored container; nodes without declared membership share an
**External / Unassigned** container. These containers group the focused
subgraph without changing graph facts. The option is disabled for All Systems,
defaults to unchecked, and retains its value while the document remains open.
Unrelated systems remain hidden until another system or All Systems is selected.
Node-class and relationship-kind filters continue to apply, so an outside node
disappears when its only enabled connection is filtered out.

[![Orders system with cross-system connections](../assets/system-walkthrough-demo.png)](../../examples/system-walkthrough/minotaur-graph.html)

In this example, `orders` is the selected system. `billing` and the shared,
unassigned files appear because they connect directly to it. The red lines mark
connections that cross the selected boundary. Open the linked HTML file to try
the controls yourself.

The [shop system walkthrough](../../examples/system-walkthrough/README.md#explore-system-boundaries-visually)
includes a small offline explorer with two declared systems and unassigned
boundary nodes. Its checked-in configuration and regeneration script show the
same project layout used for production repositories. `docs/systems` is the
recommended default, while the project configuration's `systems_dir` field can
select a different parent directory without changing the per-system format.

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
the downloaded file must contain no source text, and ensure no project
configuration supplies a source root (run from outside that configured tree
with explicit graph/output paths if necessary). Missing, unreadable,
non-UTF-8, and escaping-symlink paths are not embedded. The command warns,
while still writing successfully, when the artifact exceeds 10 MiB.
