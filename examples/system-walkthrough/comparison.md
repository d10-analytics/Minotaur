# System comparison command reference

For the visual explanation and a plain-language summary of the result, begin
with [See what changed](README.md#3-see-what-changed) in the main walkthrough.
This page preserves the exact setup, commands, output, exit behavior, evidence
limits, and regeneration steps for readers who want to reproduce or automate
the comparison.

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

## Two ways to compare source revisions

Minotaur compares two analyzed revisions, not two saved graph files. Both
commands below analyze the source that belongs to each side with the same
installed Minotaur version, using each revision's own configuration, source
selection, and system definitions. A committed graph or sidecar is not
required, so a stale saved graph cannot change the answer.

- `minotaur query diff --systems` compares the committed source at `HEAD` with
  the source currently in your working tree. It is the convenient way to review
  uncommitted work before committing it.
- `minotaur query diff --systems BEFORE AFTER` compares two explicit Git
  revisions — tags, branches, or commit IDs. Each side is read from its own
  revision, so the comparison is isolated from your working tree.

Both forms accept `--system NAME` to focus the report, `--details` for old and
new evidence, `--json` for automation, and `--html PATH` to write a
self-contained offline report. The report is a captured comparison: it records
the revision identities that were resolved when it was generated and does not
change when your working files change later.

## The reproducible scenario

The runner copies the checked-in shop and its system definitions into a
temporary repository, creates this configuration, and initializes a local Git
repository on `main`:

```toml
[minotaur]
schema_version = 1
root = "."
targets = ["shop"]
graph = "graph.json"
```

It commits only the source, definitions, and configuration, under a fixed
author, committer, and timestamp. No graph or sidecar is committed, because the
comparison analyzes each revision's captured source directly; the `graph` entry
in the configuration is simply unused by this route. The commit never needs or
sets a global identity. Since every commit input is fixed, the two revision IDs
below are the same on every machine. The baseline is tagged `v1.0`.

The runner then applies one change set and tags the result `v2.0`. The change
set deliberately contains four representative kinds of change:

- **A boundary change.** Orders gains an import of Billing's new `refund` and a
  `cancel_order` function that calls it. A new cross-system call and import
  cross the Orders/Billing boundary, and Billing gains `refund` on its exposed
  surface.
- **A call-expression change.** Billing's `charge` changes the value it passes
  to the shared ledger's `record`. The call still resolves, so it is reported
  as a changed call expression rather than as an added or removed call.
- **A move.** `complete_order` moves from `shop/orders.py` to the new
  `shop/order_ops.py`, which the Orders definition now lists. The old symbol is
  removed and an equivalent symbol is added inside the same system, so a move
  is visible as a removal plus an addition plus a membership change.
- **A removed item.** The unassigned `shop/checkout.py` file is deleted. Its
  module, its `checkout` symbol, and the Orders surface it reached all
  disappear from the report.

## Compare HEAD with the working tree

With the change set applied but uncommitted, `HEAD` still names `v1.0` and the
working tree holds the edits:

```console
$ minotaur query diff --systems
```

The report names the two sides as `HEAD · <short commit>` and
`Working tree at report generation`. The working-tree side has no commit
identity, and the report never invents one. The complete change set is:

```text
membership changed: shop/order_ops.py — unassigned -> orders
surface added: billing shop/billing.py.shop.billing.refund
surface removed: orders shop/orders.py.shop.orders.create_order
consumer added: billing <- shop/order_ops.py
consumer changed: billing <- shop/orders.py
consumer removed: billing <- shop/checkout.py
consumer removed: orders <- shop/checkout.py
dependency changed: orders -> billing
boundary added: billing.shop.billing.refund -> no_system.shop.ledger.record (calls)
boundary added: orders.shop.order_ops -> billing.shop.billing.charge (imports)
boundary added: orders.shop.order_ops.complete_order -> billing.shop.billing.charge (calls)
boundary added: orders.shop.orders -> billing.shop.billing.refund (imports)
boundary added: orders.shop.orders.cancel_order -> billing.shop.billing.refund (calls)
boundary removed: no_system.shop.checkout -> billing.shop.billing.charge (imports)
boundary removed: no_system.shop.checkout -> orders.shop.orders.create_order (imports)
boundary removed: no_system.shop.checkout.checkout -> billing.shop.billing.charge (calls)
boundary removed: no_system.shop.checkout.checkout -> orders.shop.orders.create_order (calls)
boundary removed: orders.shop.orders -> billing.shop.billing.charge (imports)
boundary removed: orders.shop.orders.complete_order -> billing.shop.billing.charge (calls)
```

Write the same comparison as an offline report:

```console
$ minotaur query diff --systems --html working-tree-comparison.html
```

`--html` writes the report and still prints the summary; the command exits `1`
because changes were found. That is a successful comparison, not a failure.
`--json` returns the same typed change records and the resolved side identities
instead of the display text.

## Compare two historical revisions

Once the change set is committed and tagged, the same comparison runs between
two named revisions:

```console
$ minotaur query diff --systems v1.0 v2.0
```

The report retains the requested names and their resolved short commit IDs, so
moving branch names cannot obscure which revisions the saved report compares:

```text
Before: v1.0 · 134b138
After: v2.0 · a2b78dd
```

The complete change set is the same as the working-tree report above, because
the same source difference is analyzed on both sides. A saved historical report
embeds `revisions.old` and `revisions.new` with those labels plus a `before` and
`after` record carrying each requested revision, its full resolved commit ID,
and a source digest. Generating a report from a branch that later moves does
not change the saved identities.

Focus one system, inspect evidence, or write the report:

```console
$ minotaur query diff --systems v1.0 v2.0 --system billing
$ minotaur query diff --systems v1.0 v2.0 --system billing --details
$ minotaur query diff --systems v1.0 v2.0 --html minotaur-comparison.html
```

Selection filters the complete comparison; it does not restrict analysis to the
selected system's files. A change involving both Orders and Billing stays
visible when selecting either one. `--details` shows old and new values and
source evidence, with an absent side printed as `unavailable`.

## Exit status

- `0` means the complete comparison found no changes.
- `1` means the comparison succeeded and found changes. Writing a requested
  `--html` report does not change this: a successful report with changes exits
  `1`.
- `2` means the comparison or the requested output could not be completed, for
  example an unknown revision, an invalid configuration, an unreadable output
  destination, or an ambiguous identity. Errors name the revision or side that
  could not be acquired or interpreted; a partial comparison is never printed
  as if it were complete.

## Evidence limits

A comparison reports accepted structural facts only. It does not infer renames,
causality, edit timing, or intent. A move therefore appears as a removal and an
addition rather than a first-class rename.

When the stored call-expression evidence is unavailable on a side, the report
prints a limitation notice for that side instead of claiming the call is
unchanged or changed. Missing call evidence alone never counts as a detected
change. Snapshot acquisition failures are different: a revision that cannot be
acquired or interpreted is an error at exit `2`, not a limitation.

## Compare visually

The report written by `--html` is a self-contained, offline page. It makes no
network requests and can be opened directly from disk. It offers the same
comparison experience for both workflows:

- **Graph view** switches the whole map between **Combined**, **Before**, and
  **After**. This is a graph-level view: it changes which revision's structure
  is drawn and keeps the layout stationary.
- **Emphasize changes** dims unchanged structure while keeping added, removed,
  and changed structure at full opacity. Existing node-class and
  relationship-kind colors are retained.
- **System** and node/relationship filters focus the map without changing the
  stored comparison facts.
- The **details panel** exposes a separate **Source revision** switch for a
  selected call. That switch changes only which side's captured source excerpt
  is shown; it never changes the graph view, layout, zoom, or selection. The
  excerpt caption names the captured revision, for example
  `Captured After revision: v2.0 · a2b78dd`.

[![Combined comparison of the v1.0 and v2.0 shop revisions](../../docs/assets/system-comparison-demo.png)](minotaur-comparison.html)

The saved report is static. It does not observe or re-read your working files,
so editing source after generation cannot change what the report shows.
Regenerate it to compare the new state.

## Regenerate the checked-in report

`examples/system-walkthrough/minotaur-comparison.html` is generated from the
same fixed-identity temporary history by the walkthrough's regeneration script:

```bash
python3 examples/system-walkthrough/regenerate_system_walkthrough.py --comparison-only
```

The full regeneration command rebuilds the graph, sidecar, explorer, and
comparison report together:

```bash
python3 examples/system-walkthrough/regenerate_system_walkthrough.py
```
