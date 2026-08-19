# Language interpreter template

Copy this directory to create a new native language interpreter. It is a
source template, not an importable Python package and not a generator input.

## Copy and rename

1. Create `src/minotaur/language_interpreter/example_language/` and
   `tests/language_interpreter/example_language/`.
2. Copy `__init__.py.tmpl`, `interpreter.py.tmpl`, and `discovery.py.tmpl` to
   `src/minotaur/language_interpreter/example_language/`, removing `.tmpl`
   from each destination filename.
3. Copy `test_interpreter.py.tmpl` to
   `tests/language_interpreter/example_language/test_interpreter.py`.
4. Replace every `example_language` with the new language's lowercase package
   name and every `.example` with its normalized file extension.
5. Replace the analysis stub and starter assertion with tested static analysis
   and behavioral assertions, then add exactly one `InterpreterRegistration`
   to `default_registry()`.

Do not register the extension while `analyze_example_language_files()` is a
stub. Registration makes the existing CLI dispatch real user input to that
function, so it belongs only after the implementation and behavioral tests
exist.

The copied starter test intentionally fails while the interpreter is a stub.
Do not skip it: replace its fixture and assertion with behavior that would
fail if the language-specific analysis were removed.

## Boundaries

The new language owns parsing, resolution limits, source locations, evidence,
and graph facts. Source selection, workspace containment, output handling,
and CLI dispatch are shared concerns. A completed language change therefore
adds its package, its tests, and one `default_registry()` registration; it
does not add a language-specific command or reimplement shared path and output
policy.

The selected-file function is required. The whole-workspace wrapper is
optional and must stay thin: it delegates to the selected-file function using
language-specific discovery. Do not create parser, resolver, symbol, or
fixture layers until the language's tested semantics require them.
