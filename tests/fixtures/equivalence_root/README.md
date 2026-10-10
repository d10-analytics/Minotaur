# Equivalence fixture root

A deterministic, committed Python substrate for `scripts/check_equivalence.py`.
It includes `root_star.py`, a minimal unexecuted source case that keeps the
harness's module-only root-star analysis non-vacuous, and
`workflow/declarations.py`, the analyzer-semantics sample for declaration
roles: a property with explicit getter, setter and deleter accessors, and
`@overload`/`@typing.overload` stubs beside their implementation. It defines no
`main` and calls none of the queried symbols.

The tree is small on purpose: every query class in
`scripts/equivalence_queries.json` has a real, non-empty hit here — several
definitions of `main`, a symbol with more than one caller, a symbol with no
callers at all (`workflow.util.unused_helper`, which is also the
`unreferenced` hit), an import graph for `impact`, and a stable line for
`context`.

Nothing outside the harness imports this package; it is source material to be
analyzed, not code to be executed.
