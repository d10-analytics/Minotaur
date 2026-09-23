# Learn Python binding through small examples

A **binding** associates a name with a value. A **lexical scope** is the region
of source that determines which binding a name refers to. A **qualified name**
includes its module and enclosing class, such as `app.Service.run`.

After [installation](../../README.md#quick-start), run from the repository root:

```bash
python examples/run_walkthrough.py bindings
```

The [runner](../../examples/run_walkthrough.py) creates temporary Python files,
prints `app.py`, analyzes them, and prints these results. `lib.helper` returns
1 and `other.helper` returns 2; their different qualified names matter, not
their return values. The `flag` parameter is deliberately unknown to static
analysis. The runner removes its temporary directory afterward.

A **join** combines information from possible branches. In `same`, both arms
import the same helper, so the call after the `if` resolves. In `different`,
the two imports disagree, so the later call remains unresolved. No branch is
executed to reach these answers.

```text
from lib import helper

def same(flag):
    if flag:
        from lib import helper
    else:
        from lib import helper
    helper()

def different(flag):
    if flag:
        from lib import helper
    else:
        from other import helper
    helper()

def callback():
    register(helper)

class Service:
    def run(self):
        return 1

    def known(self):
        self.run()

def unknown():
    service.run()
$ minotaur analyze --root . --output graph.json --force .
exit: 0
$ minotaur query callers lib.helper --graph graph.json --root . --no-refresh
app.py:8:5  app.same [calls]
app.py:15:5  helper [references] [unresolved]
exit: 0
$ minotaur query impact lib.helper --graph graph.json --root . --no-refresh
depth 0: lib.helper
depth 1: app
depth 1: app.same
exit: 0
$ minotaur query unreferenced lib.py --graph graph.json --root . --no-refresh
no unreferenced symbols
exit: 0
$ minotaur query callers app.Service.run --graph graph.json --root . --no-refresh
app.py:25:9  app.Service.known [calls]
app.py:28:5  service.run [references] [unresolved]
exit: 0
$ minotaur query callers callback_only.handler --graph graph.json --root . --no-refresh
no callers
exit: 0
$ minotaur query impact callback_only.handler --graph graph.json --root . --no-refresh
depth 0: callback_only.handler
exit: 0
$ minotaur query unreferenced callback_only.py --graph graph.json --root . --no-refresh
no unreferenced symbols
exit: 0
```

The unresolved `helper` row is a possible name match, not a proved call to
`lib.helper`. Consequently it does not appear in that function's impact chain.
The module `app` appears in impact because it imports the helper; `app.same`
appears because it calls the helper. `callback` passes the helper to an unknown
registration function: this is a reference, not a direct call to the helper.

For an isolated callback example the runner also creates `callback_only.py`:

```python
def handler():
    return 1


register(handler)
```

`handler` has no callers and only depth zero in its impact result, but is not
unreferenced: passing it as a callback counts as a use. `register` is an unknown
name here; the example describes source facts and is not intended to execute.

Within `Service`, `self.run()` resolves through the owning class. In `unknown`,
`service.run()` has no established receiver and is reported as unresolved.
The tool does not guess that it refers to `Service.run` merely because the
method names match. Read the [Python analysis reference](analyze-python.md)
for the precise boundaries, including branches that return or raise.
