# Recover from a malformed source file

After [installation](../../README.md#quick-start), run from the repository root:

```bash
python examples/run_walkthrough.py malformed
```

The runner creates `good.py` containing `def healthy(): return 1` and
`broken.py` with the invalid header `def repaired(:`. It uses a fresh temporary
workspace, so it never breaks a bundled source file. Analysis writes a partial
graph from the valid file and reports the malformed sibling on standard error:

```text
$ minotaur analyze --root . --output graph.json .
stderr:
broken.py:0:13: parse-error: invalid syntax
exit: 1
$ minotaur query definitions healthy --graph graph.json --root . --no-refresh
good.py:1  good.healthy  function
stderr:
minotaur: stale: broken.py
exit: 0
$ minotaur analyze --root . --output graph.json --force .
exit: 0
$ minotaur query definitions repaired --graph graph.json --root . --no-refresh
broken.py:1  broken.repaired  function
exit: 0
```

The parser's diagnostic wording can vary by Python version. Its zero-based
location differs from the one-based locations in query results. Exit `1` from
analysis means a partial graph with diagnostics; exit `2` means an argument or
preflight failure. Do not treat either as a complete successful analysis.

`--no-refresh` lets us inspect `healthy` in the partial snapshot. The stale
warning for `broken.py` is expected: that selected file is absent from the
partial graph. The runner then fixes its header to `def repaired():`, forces
analysis again, and finds the recovered function at line 1. All temporary
files, including the graph and sidecar, are removed at the end.
