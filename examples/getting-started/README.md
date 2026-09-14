# Your first Python graph

Complete the [installation steps](../../README.md#quick-start) first. Run all
commands from the repository root with that Python environment activated.

Read [app.py](app.py): `greeting` returns text; `welcome` calls it with `"Ada"`.
`name: str` and `-> str` are type hints describing string input and output.
They help readers and tools; they do not themselves enforce types at runtime.

Run the portable walkthrough (Linux, macOS, or Windows):

```bash
python examples/run_walkthrough.py python
```

The [runner](../run_walkthrough.py) creates a new temporary directory, copies
`app.py` into it, and runs the following commands there. It prints every command
and exit status. `.` means that temporary source directory; the output graph
and its `.sha256` sidecar live beside the copied source. The HTML embeds source
excerpts because the visualization command supplies `--source-root .`.
No example source is executed by Minotaur.

```text
$ minotaur analyze --root . --output graph.json --force app.py
exit: 0
$ minotaur query definitions greeting --graph graph.json --root . --no-refresh
app.py:4  app.greeting  function
exit: 0
$ minotaur query callers app.greeting --graph graph.json --root . --no-refresh
app.py:11:12  app.welcome
exit: 0
$ minotaur visualize --input graph.json --output graph.html --source-root .
exit: 0
```

`app.greeting` is a qualified name: module `app`, function `greeting`.
`app.py:11:12` identifies the caller's one-based line and column. The analysis
and visualization commands are silent on success; `exit: 0` is printed by the
runner, not Minotaur. `--force` permits regeneration and `--no-refresh` asks a
query to use the saved snapshot.

The runner then prints a `file://` URL. Paste it into your browser to inspect
the graph. Select a calls edge and inspect its source evidence. Press Enter in
the terminal when finished; only this invocation's temporary directory is
removed. Use `--no-pause` to run the same commands without waiting or retaining
the HTML for viewing. To experiment interactively, copy `app.py` into a scratch
directory of your own and run the displayed commands from that directory.

Next: [binding examples](../../docs/guides/python-binding-examples.md),
[error recovery](../../docs/guides/source-error-recovery.md), or the
[implementation reading guide](../../docs/guides/contributing.md).
