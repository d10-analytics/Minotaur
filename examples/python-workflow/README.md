# Focused Minotaur module workflow

This checked-in example analyzes the `selection` module from the
`language_interpreter` subpackage, then renders the resulting canonical
graph as a portable HTML explorer.

From the repository root, regenerate the checked-in artifacts with:

```bash
python3 scripts/generate_example_output.py
```

The generator invokes the public commands and omits only volatile Git snapshot
metadata from this distributable example. A direct `minotaur analyze` command
retains that metadata in its normal output.

Open `minotaur-graph.html` directly in a browser using its local `file://`
path. It is a self-contained offline artifact, not a hosted page, and it does
not need a server or network connection.
