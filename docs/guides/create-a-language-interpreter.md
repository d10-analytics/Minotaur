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

One CLI command may call the selected-file function more than once. When a
call raises, the CLI calls the function again with each selected file on its
own, discarding those results, to find the files that fail alone, and then
calls it once more with the remaining files. Each file that fails alone becomes one
`interpreter-error` diagnostic and contributes no facts; see
[Current Python analysis](analyze-python.md#diagnostics-and-unresolved-references).
Keep every accumulator inside the call and never mutate module-level state, so
a call's result depends only on its arguments and a failed call cannot affect
the next one. Report an expected source problem, such as a read or parse
failure, as an ordinary diagnostic rather than raising.

## Optional whole-workspace convenience API

The language interpreter template omits the whole-workspace helper, so a new
language package exposes only its selected-file function. Library callers that
want a whole-root analysis use the shared `select_sources()` selection
function, which owns folder walking, and a new language package must not walk
folders.

The Python interpreter keeps an older whole-workspace helper. Its
`analyze_python_workspace()` API remains compatible for existing callers,
while `analyze_python_files()` is the API used by the CLI.

`discover_python_files()` is the discovery function behind that older helper.
It decides what a full Python workspace scan means and is not used by the CLI,
which instead selects registered extensions across all requested targets.
This Python arrangement is not a pattern for new languages: a new language
package defines no discovery function, and whole-root callers select its files
through `select_sources()`.

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

Add a representative sample root under `tests/fixtures/analyzer_semantics/`
and include it in `tests/test_analyzer_semantics_version.py` so the registry-coverage
test covers the new interpreter. Update `ANALYZER_SEMANTICS_VERSION` to the fresh
fingerprint printed by that test, then follow the regeneration and later pin
commit procedure in [contributing](contributing.md#changing-analyzer-output).

After registering a new interpreter, maintain the [interpreter edge-case
catalog](../concepts/interpreter-edge-cases.md) in this order: add one status
row for every existing case; for each applicable row add a minimal example,
expected graph facts, one owner marker, and one natural behavioral proof; add a
new append-only case only when a source-proven invariant is genuinely new; then
run the catalog integrity test followed by all six CI lanes. Keep language
semantics owned by the interpreter: this process does not promise mixed-
language graph composition or require separate interpreters to share internal
algorithms.

## Record source freshness

The registration's `namespace` must match the extension key on every emitted
file node. Store the lowercase SHA-256 digest of `ParsedSource.content`, the
original bytes retained by `read_and_parse()`. Hashing decoded text can lose a
byte-order mark or change line endings. Without a matching hash, a query treats
an existing file as changed even when nobody edited it.

For example, this function uses the shared graph constructors (the caller
supplies its registered namespace and language):

```python
import hashlib

from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import IdentityBasis, NodeClass
from minotaur.language_interpreter.reading import ParsedSource


def source_file_node(parsed: ParsedSource, namespace: str, language: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, namespace)
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path=parsed.relative),
        identity=identity,
        node_class=NodeClass.FILE,
        label=parsed.relative,
        path=parsed.relative,
        language=language,
        extensions={namespace: {"content_sha256": hashlib.sha256(parsed.content).hexdigest()}},
    )
```

The digest describes content, not node identity: changing source bytes does not
change a file-path node's identity. The CLI owns saved selection metadata,
serialization, and sidecar writing; an interpreter must not duplicate those
operations. See [freshness](../concepts/freshness.md) for the reader contract.

Test both halves of the integration: a newly generated graph is clean against
unchanged source, and editing source bytes naturally triggers drift. Use the
registered extension and namespace together. Also verify that original bytes,
including a byte-order mark and line endings, are what the producer hashes.

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
`tests/language_interpreter/example_language/test_example_language_interpreter.py`.
The language-qualified basename prevents top-level pytest collection
collisions when multiple interpreter test modules are selected together. Keep
CLI and graph-model tests in their existing top-level test categories.

An interpreter change should include behavioral tests that prove:

- its extension is discovered from a selected directory and an explicit file;
- unsupported files found recursively are ignored, while explicitly selected
  unsupported files fail before analysis;
- graph paths and diagnostics are stable when equivalent file targets are
  supplied in a different order;
- language-specific parse or read failures preserve valid facts from the other
  selected files; and
- for the Python interpreter, its retained `analyze_python_workspace()` helper
  still delegates to the same selected-file analysis behavior; a new language
  package supplies no such helper.

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
