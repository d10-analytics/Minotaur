# Portable HTML visualization

Create a local, standalone graph explorer from a validated Minotaur graph:

```bash
minotaur visualize --input graph.json --output graph.html --source-root path/to/source
```

The command validates the JSON Schema, loads the graph model, performs
semantic validation, and canonicalizes it before writing output atomically.
It refuses an existing destination unless `--force` is supplied, and refuses
an output that aliases the input graph.

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

When a source root is available, the artifact embeds only the merged evidence
spans plus up to 50 lines of surrounding context; omit `--source-root` when
the downloaded file must contain no source text. Missing, unreadable,
non-UTF-8, and escaping-symlink paths are not embedded. The command warns,
while still writing successfully, when the artifact exceeds 10 MiB.
