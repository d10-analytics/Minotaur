# Focused Minotaur module workflow

This checked-in example analyzes the `selection` module from the
`language_interpreter` subpackage, then renders the resulting canonical
graph as a portable HTML explorer.

From the repository root, regenerate the artifacts with these two commands:

```bash
minotaur analyze --root . --output examples/python-workflow/minotaur-graph.json --force \
  src/minotaur/language_interpreter/selection.py
minotaur visualize --input examples/python-workflow/minotaur-graph.json --output examples/python-workflow/minotaur-graph.html --source-root . --force
```

Open `minotaur-graph.html` directly in a browser using its local `file://`
path. It is a self-contained offline artifact, not a hosted page, and it does
not need a server or network connection.
