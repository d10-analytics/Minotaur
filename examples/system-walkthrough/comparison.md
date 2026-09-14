# Compare a committed system boundary with working source

Complete [installation](../../README.md#quick-start) first. Git must be on PATH.
From the repository root, with your Python environment activated, run:

```bash
python examples/run_walkthrough.py systems
```

The [runner's `systems` function](../run_walkthrough.py) performs every setup,
edit, and comparison below inside a new temporary repository. It prints each
Minotaur command and its exit status, then removes only that temporary directory.
It never commits in your checkout or changes your Git configuration. Read
[the inventory walkthrough](README.md) first to learn the shop's boundaries.

## Establish the baseline

The runner copies the shop and its system definitions, creates this configuration,
and initializes a local Git repository:

```toml
[minotaur]
schema_version = 1
root = "."
targets = ["shop"]
graph = "graph.json"
```

It runs `minotaur analyze`, stages the source, definitions, configuration,
`graph.json`, and `graph.json.sha256`, then commits them together. The commit
command supplies a synthetic author with per-command `git -c` settings and
disables signing for that one commit. It neither needs nor sets global identity.
A first `minotaur query diff --systems` reports `no system differences` and exits
0. `HEAD` now names the baseline commit used by subsequent comparisons.

## Change a connection between systems

The runner appends `refund(order)` to billing; that function calls `charge`.
It also imports `refund` in orders and adds `cancel_order(order)` calling it.
The new use crosses from orders into billing. These are structural source
changes; changing only a return value would not necessarily change a system
boundary report. A new private function used only within billing likewise need
not change these reports. Use ordinary graph diff for symbol-level comparisons.

The runner executes:

```bash
minotaur query diff --systems
minotaur query diff --systems --system billing
minotaur query diff --systems --system billing --details
minotaur query diff --systems --system billing --json
```

The first two commands include these exact change rows (their coverage and
selection lines are omitted here for readability):

```text
surface added: billing shop/billing.py.shop.billing.refund
consumer changed: billing <- shop/orders.py
dependency changed: orders -> billing
boundary added: orders.shop.orders -> billing.shop.billing.refund (imports)
boundary added: orders.shop.orders.cancel_order -> billing.shop.billing.refund (calls)
```

Both systems participate in those changes, so selecting billing retains rows
that also mention orders. Selection filters the complete comparison; it does
not restrict analysis to billing's files. `--details` shows old/new values and
evidence; a missing side is `unavailable`. `--json` gives the typed records and
coverage as JSON instead of display text. The runner prints the full output
of both commands so you can inspect the actual evidence and fields.

All four comparisons exit 1 to signal differences. That is expected, not a
failed example. The runner checks each expected status explicitly. Status 2
would mean invalid input or another comparison error and stops the walkthrough.
Comparison reads the baseline and analyzes current source in memory without
rewriting the committed graph or sidecar.

## Change only membership

The runner restores both edited source files to their original bytes, then
adds `shop/ledger.py` to billing's `files` in `system.toml`. It does not regenerate
the graph. The final `minotaur query diff --systems` reports membership changes
and the resulting boundary changes, with status 1. The old and working graph
files still have the baseline bytes: the changed definition, rather than a
source edit, changed which system owns the ledger.

To experiment manually, copy the shop and definitions into a scratch directory
you own, follow the same setup in the runner, and run commands there. Never
perform the baseline commit or source restoration in the original checkout.
