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
2. Copy `__init__.py.tmpl`, `interpreter.py.tmpl`, and `discovery.py.tmpl` to
   `src/minotaur/language_interpreter/example_language/`, removing `.tmpl`
   from each destination filename.
3. Copy `test_interpreter.py.tmpl` to
   `tests/language_interpreter/example_language/test_interpreter.py`.
4. Replace every `example_language` with the new language's lowercase package
   name, `minotaur-example` with its namespace, and `.example` with its
   normalized file extension.
5. Replace the `analyze_example_language_files()` stub with tested,
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

The selected-file function is required. The whole-workspace wrapper is
optional and must stay thin: it delegates to the selected-file function using
language-specific discovery. Do not create language-specific parser, resolver,
symbol, or fixture layers until tested semantics require them, and do not
reimplement the shared `accumulation`, `emission`, `reading`, or `paths`
mechanisms.
