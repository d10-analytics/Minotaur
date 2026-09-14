# JavaScript imports and calls

After [installation](../../README.md#quick-start), run from the repository root:

```bash
python examples/run_walkthrough.py javascript
```

The runner copies [app.js](app.js) and [lib.js](lib.js) into a fresh temporary
directory, runs the public commands below there, and removes only that directory
when finished. JavaScript is parsed as source; Node.js is not required.

`lib.js` exports `greet`; `app.js` imports that named declaration through the
exact relative path `./lib.js`. Inside `welcome`, `greet()` has a supported
static target. `console.log(...)` is a member call whose runtime target the
analyzer cannot establish. Its graph contains an unresolved `console.log`
reference; it does not become a resolved calls edge. An unresolved fact means
"not established by this analyzer", not "invalid JavaScript".

```text
$ minotaur analyze --root . --output graph.json --force .
exit: 0
$ minotaur query definitions greet --graph graph.json --root . --no-refresh
lib.js:1  lib.greet  function
exit: 0
$ minotaur query callers lib.greet --graph graph.json --root . --no-refresh
app.js:4:5  app.welcome
exit: 0
$ minotaur query callers app.welcome --graph graph.json --root . --no-refresh
no callers
exit: 0
```

Notice that the module label is `lib`, while the source filename is `lib.js`.
`welcome` has no callers in this selection. That is a successful empty answer,
not proof that nobody calls it outside these files. See the
[JavaScript reference](../../docs/guides/analyze-javascript.md) for supported
exports, scope rules, and excluded syntax. Do not mix Python and JavaScript
files in one analysis invocation.
