# Create a language interpreter

Minotaur's language-interpreter boundary separates shared source selection
from language-specific static analysis. An interpreter owns its language
semantics, resolution limits, source locations, and evidence. It does not own
command-line target walking, workspace containment, exclusion rules, or output
writing.

Start by copying [`templates/language_interpreter/`](../../templates/language_interpreter/).
Its README gives the exact destination paths and rename steps. The template
creates only the language package and its behavioral-test starter; it does not
create a command, change CI, or register an extension.

This guide describes the convention every new native interpreter should
follow. It keeps the single `minotaur analyze` command language-neutral while
allowing library users to choose between whole-workspace and selected-file
analysis.

## Required integration API

Every interpreter must expose a selected-file function with this shape:

```python
def analyze_example_language_files(
    workspace: Workspace,
    files: tuple[Path, ...],
) -> AnalysisResult: ...
```

`workspace` is an already validated, resolved source root. `files` are
existing regular files beneath that root, selected by the shared CLI policy.
The interpreter should use their root-relative POSIX paths for graph paths,
identities, diagnostics, and deterministic ordering.

This is the required integration point because it gives every language the
same security and user-experience rules. The CLI resolves symlinks before
checking containment, deduplicates overlapping targets, applies ordinary
recursive exclusions, decides whether an extension is supported, and handles
canonical atomic output. Reimplementing any of those concerns in an
interpreter would let languages disagree about what the same command means.

An interpreter may assume the supplied paths meet this contract when called
from the CLI. Its public docstring should state the assumptions so direct
library callers understand that they are responsible for equivalent
validation.

## Optional whole-workspace convenience API

An interpreter may also expose a convenience wrapper:

```python
def analyze_example_language_workspace(root: Path) -> AnalysisResult:
    workspace = Workspace(root)
    return analyze_example_language_files(workspace, discover_example_language_files(workspace))
```

Use this only for the simple library use case: analyze every normally
discoverable file for one language beneath a root. Keep it a thin wrapper over
the selected-file function. It must not have a second parsing or graph-building
implementation.

The Python interpreter follows this pattern. Its
`analyze_python_workspace()` API remains compatible for existing callers,
while `analyze_python_files()` is the API used by the CLI. Future languages
should follow the same arrangement when a whole-workspace helper is useful.

Language-specific discovery belongs only in this optional wrapper. For
example, `discover_python_files()` decides what a full Python workspace scan
means; it is not used by the CLI, which instead selects registered extensions
across all requested targets.

## Register the extension

Add an `InterpreterRegistration` in `default_registry()` in
`src/minotaur/language_interpreter/registry.py`. A registration pairs one
normalized file extension with the selected-file function:

```python
InterpreterRegistration(
    ".example",
    analyze_example_language_files,
    namespace="minotaur-example",
)
```

Extensions are case-insensitive and must be unique. A duplicate is rejected
at registry construction rather than allowing registration order to silently
choose an interpreter. Registering the extension is all that is needed for the
existing CLI to discover and dispatch explicitly selected `.example` files;
do not add a language-specific subcommand or language flag.

## Mixed-language analysis is a separate design decision

The registry and CLI selection boundary are intentionally ready for multiple
extensions, but they do not yet define how results from two interpreters become
one graph. Do not implement that by concatenating documents in an interpreter
or by adding an implicit merge in the CLI.

Before enabling mixed-language command invocations, specify graph-composition
rules for producer metadata, node identities, relationship/evidence merging,
diagnostic ordering, and error behavior. Until then, a valid selection using
only one registered interpreter remains the supported composition boundary.

## Tests to add

Place an interpreter's tests at
`tests/language_interpreter/example_language/test_interpreter.py`. Keep CLI
and graph-model tests in their existing top-level test categories. The Python
interpreter establishes this convention at
`tests/language_interpreter/python/test_interpreter.py`.

An interpreter change should include behavioral tests that prove:

- its extension is discovered from a selected directory and an explicit file;
- unsupported files found recursively are ignored, while explicitly selected
  unsupported files fail before analysis;
- graph paths and diagnostics are stable when equivalent file targets are
  supplied in a different order;
- language-specific parse or read failures preserve valid facts from the other
  selected files; and
- the whole-workspace convenience wrapper, if supplied, delegates to the same
  selected-file analysis behavior.

Use synthetic, public-safe source fixtures. The graph must describe only facts
the interpreter can establish statically; unresolved language constructs should
remain explicit unresolved references rather than guessed edges.

Across languages, a name bound in the lexical scope that owns a reference is
not an unresolved reference. This lexical-scope suppression applies to
function and method bodies.

For an unresolved member expression, emit exactly one unresolved fact labelled
with the expression's full text, beside the ordinary fact for its base
expression when that base is eligible. A member expression in a call position
is still one member fact; it does not imply a call relationship.
