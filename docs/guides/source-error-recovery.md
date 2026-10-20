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
broken.py:0:13: parse-error: invalid syntax [severity=error]
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
preflight failure, or an analysis failure that no single selected file
explains, and writes no graph. Do not treat either as a complete successful
analysis.

`--no-refresh` lets us inspect `healthy` in the partial snapshot. The stale
warning for `broken.py` is expected: that selected file is absent from the
partial graph. The runner then fixes its header to `def repaired():`, forces
analysis again, and finds the recovered function at line 1. All temporary
files, including the graph and sidecar, are removed at the end.

A Python file whose bytes cannot be decoded follows the same path under a
different code. Analysis decodes each Python file with the codec that its PEP
263 encoding declaration or UTF-8 byte-order mark selects, or as UTF-8 when it
has neither; see
[Current Python analysis](analyze-python.md#diagnostics-and-unresolved-references).
When the declaration cannot be honoured, or the file contains bytes that the
selected codec cannot decode, analysis reports one `source-read-error` for
that file instead of a `parse-error`, writes the partial graph from its valid
siblings, and exits `1`. To repair it, add or correct the declaration so that
it names the file's actual ASCII-compatible encoding, or re-encode the file as
UTF-8 and remove any declaration that names another codec. Then analyze again.

A file whose name cannot be a graph path gets the same treatment, in every
language. A directory scan that finds a file name containing a backslash, or
name bytes that are not valid UTF-8, reports one error-severity
`source-read-error` for that file under its raw relative path, skips it,
analyzes its siblings, writes the partial graph and exits `1`. To repair it,
rename the file. Naming such a file yourself as a target is a command error:
`analyze` names the target, exits `2`, and writes no graph.

A file that makes the analyzer itself fail unexpectedly is isolated the same
way. Analysis retries each selected file of the failing language on its own,
reports each file that fails alone as one error-severity `interpreter-error`
with no location, such as `deep.py: interpreter-error: RecursionError: maximum
recursion depth exceeded [severity=error]`, writes the partial graph from the
remaining files, and exits `1`. The diagnostic reports a Minotaur limitation
rather than a problem in the source, which may be valid. A file name that cannot be a graph path is never reported this way; it is a `source-read-error`, as described above. When no single
selected file explains the failure, analysis prints one `minotaur: error:
analysis failed in a way no single selected file explains: …` line, exits `2`,
writes no graph, and leaves an existing graph and sidecar unchanged.
