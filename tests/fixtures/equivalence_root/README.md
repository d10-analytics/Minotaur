# Equivalence fixture root

A deterministic, committed Python substrate for `scripts/check_equivalence.py`.
It includes `root_star.py`, a minimal unexecuted source case that keeps the
harness's module-only root-star analysis non-vacuous, and
`workflow/declarations.py`, the analyzer-semantics sample for declaration
roles: a property with explicit getter, setter and deleter accessors, and
`@overload`/`@typing.overload` stubs beside their implementation. It defines no
`main` and calls none of the queried symbols.
`workflow/declared_encoding.py`, the source-encoding sample, declares
`# -*- coding: latin-1 -*-` on a first line that also holds a latin-1 byte, so
the analyzer-semantics fingerprint covers a declaration found beside bytes that
are not UTF-8. It is committed as latin-1 bytes that are not valid UTF-8 and
calls its one function at module scope after a non-ASCII string literal, so it
adds no `unreferenced` hit; `pyproject.toml` excludes it from Ruff, which
rejects any source file that is not valid UTF-8.

The tree is small on purpose: every query class in
`scripts/equivalence_queries.json` has a real, non-empty hit here — several
definitions of `main`, a symbol with more than one caller, a symbol with no
callers at all (`workflow.util.unused_helper`, which is also the
`unreferenced` hit), an import graph for `impact`, and a stable line for
`context`.

Nothing outside the harness imports this package; it is source material to be
analyzed, not code to be executed.

## Per-language default roots

A run with no `--root` compares this root beside the baseline `src` and the
committed JavaScript and SQL samples,
`tests/fixtures/analyzer_semantics/javascript` and
`tests/fixtures/analyzer_semantics/sql`, read-only. Each root is analyzed one
language at a time: the workload `selection` names a file whose extension is
the language boundary, so only the root's files of that language reach a
single `analyze` call. The `src` root also carries JavaScript assets, so it is
analyzed by its Python files alone and a mixed tree is never passed to one
interpreter.

A selection that names a directory or module instead of a file has no
extension and no single language to expand, so it stays a whole-root workload
analyzed with the default `.` target. The committed `Onyx` optional-root
workload selects the `instrument_reader` package this way, and the documented
`--optional-root` comparison is unchanged by the per-language split.

## Expectations and the seed cell

A workload `expect` is `ok` (exit 0 with an answer), `empty` (exit 0 with the
command's exact literal, such as `no changes`), `error` (the argument-
validation path), or `difference` (exit 1 with output, the valid exit status
of `query diff`). The committed `diff-selection` entries for this root, for
`src`, and for the JavaScript root declare `difference`, because a whole-root
selection against a narrower one removes symbols. The SQL root's `diff` is the
unchanged-graph control: its single file makes `diff` print exactly `no
changes`, so it keeps `empty`.

After the distinct-HEAD and provenance guards pass, each compared root also
runs the seed-determinism cell: the branch side's own query arguments are
rerun under a second `PYTHONHASHSEED` and compared byte for byte, so output
whose order leaks out of `hash()` fails the run rather than hiding behind the
pinned seed of ordinary comparisons.
