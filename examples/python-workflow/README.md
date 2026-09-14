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

To refresh the README screenshot after regenerating the artifacts, install
the browser dependencies and capture the selected helper call:

```bash
python -m pip install -e ".[visualizer]"
python -m playwright install chromium
python scripts/capture_python_workflow_demo.py
```

The capture waits for the initial layout animation before positioning the
camera and writes `docs/assets/python-workflow-demo.png`. Inspect the resulting
image for readable source evidence before committing it.
