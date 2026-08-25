# Equivalence fixture root

A deterministic, committed Python substrate for `scripts/check_equivalence.py`
(specification `R-07`, root (1) of `D-06`).

The tree is small on purpose: every query class in
`scripts/equivalence_queries.json` has a real, non-empty hit here — several
definitions of `main`, a symbol with more than one caller, a symbol with no
callers at all (`workflow.util.unused_helper`, which is also the
`unreferenced` hit), an import graph for `impact`, and a stable line for
`context`.

Nothing outside the harness imports this package; it is source material to be
analyzed, not code to be executed.
