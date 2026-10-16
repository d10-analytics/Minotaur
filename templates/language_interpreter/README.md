# Language interpreter template

Copy this directory to create a new native language interpreter. It is a
source template, not an importable Python package and not a generator input.

Start from the shared interpreter mechanisms in `accumulation`, `emission`,
`reading`, and `paths`. The interpreter template shows their intended wiring:
`read_and_parse()` owns selected-file reading and parse diagnostics,
`RelationshipAccumulator` owns relationship evidence, `NodeEmitter` and
`symbol_node()` own shared node construction, and `resolve_relative()` owns
the common root-escape guard. Keep language parsing and semantic traversal in
the copied interpreter.

## Copy and rename

1. Create `src/minotaur/language_interpreter/example_language/` and
   `tests/language_interpreter/example_language/`.
2. Copy `__init__.py.tmpl` and `interpreter.py.tmpl` to
   `src/minotaur/language_interpreter/example_language/`, removing `.tmpl`
   from each destination filename.
3. Copy `test_example_language_interpreter.py.tmpl` to
   `tests/language_interpreter/example_language/test_example_language_interpreter.py`.
4. Rename the placeholders in the copied file contents and destination paths,
   in this order:
   - Replace every `example_language` with the new language's lowercase
     Python package name.
   - Replace every `example-language` with the new language's graph language
     identifier, which must match `^[a-z][a-z0-9-]*$` and need not equal the
     package name.
   - Replace every `minotaur-example` with the new language's registered
     namespace.
   - Replace every `.example-lang` with the new language's normalized file
     extension.
5. Sort the imports and format the copied files, where `<name>` is the new
   package name:

   ```bash
   ruff check --select I --fix src/minotaur/language_interpreter/<name> tests/language_interpreter/<name>
   ruff format src/minotaur/language_interpreter/<name> tests/language_interpreter/<name>
   ```

6. Replace the `analyze_example_language_files()` stub with tested,
   language-specific static analysis. Replace the starter assertion with
   behavioral assertions, then add exactly one `InterpreterRegistration` to
   `default_registry()`.

Do not register the extension while `analyze_example_language_files()` is a
stub. Registration makes the existing CLI dispatch real user input to that
function, so it belongs only after the implementation and behavioral tests
exist.

The copied starter test intentionally fails while the interpreter is a stub.
Do not skip it: replace the stub with tested language-specific analysis and
replace its fixture and assertion with behavior that would fail if that
analysis were removed.

## Boundaries

The new language owns parsing semantics, resolution limits, source locations,
and graph facts. The shared `reading` module handles selected-file reading and
parse-failure diagnostics; `accumulation`, `emission`, and `paths` provide
common relationship, node, and relative-path mechanisms. Source selection,
workspace containment, output handling, and CLI dispatch are shared concerns.
A completed language change therefore adds its package, its tests, and one
`default_registry()` registration; it does not add a language-specific
command or reimplement shared path and output policy.

The selected-file function is required. The template omits the optional
whole-workspace helper, so the copied package exposes only the selected-file
function. The shared `select_sources()` selection function owns folder
walking, workspace containment, symlinks, extension matching, and excluded
folders; library callers that want a whole-root analysis use it. A language
package must not walk folders, list excluded directory names, or import
`selection` at module level.

Do not create language-specific parser, resolver, symbol, or fixture layers
until tested semantics require them, and do not reimplement the shared
`accumulation`, `emission`, `reading`, or `paths` mechanisms.

## Freshness checklist

Emit a file node with the SHA-256 of each `ParsedSource.content` under the
registered namespace's `content_sha256` key. Follow the complete construction
example in [the interpreter guide](../../docs/guides/create-a-language-interpreter.md#record-source-freshness).
Before registration, test unchanged-source cleanliness and drift after a real
byte edit, including original-byte hashing for BOM and CRLF input. Leave
selection recording, serialization, and sidecar writes to the shared CLI.
