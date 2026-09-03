# System definitions walkthrough

This example walks through Minotaur's declared-system queries — `surface`,
`consumers`, and `system-deps` — on a fabricated mini repository: an online
storefront whose source lives in `shop/`. Two subsystems are declared as
committed system definitions under `docs/systems/`; every other file is
outside every declared system. Nothing here is a real product: the sources,
definitions, and analyzed graph are all public example artifacts.

The concept is documented in [Purpose and boundary](../../docs/concepts/purpose.md),
the query model in [System definitions](../../docs/guides/system-definitions.md),
the committed file contract in
[system definition format v1](../../docs/formats/system-definition-v1.md), and
the per-command options in the [query reference](../../docs/guides/query-reference.md).

## The example tree

```text
examples/system-walkthrough/
├── README.md                                this walkthrough
├── minotaur-graph.json                      committed analysis of shop/,
│   └── minotaur-graph.json.sha256           plus its trusted-load stamp
├── shop/                                    the fabricated storefront package
│   ├── __init__.py
│   ├── billing.py                           declared system "billing"
│   ├── checkout.py                          no declared system
│   ├── ledger.py                            no declared system
│   └── orders.py                            declared system "orders"
├── docs/systems/                            committed system definitions
│   ├── billing/
│   │   ├── system.toml
│   │   └── README.md                        human narrative, ignored
│   └── orders/
│       ├── system.toml
│       └── README.md                        human narrative, ignored
└── regenerate_system_walkthrough.py         reproduces the committed graph
```

Every query command below runs from the repository root and reads the
committed graph with `--no-refresh`, so a walkthrough never rewrites a
checked-in file. `--root examples/system-walkthrough` is the source root the
graph was analyzed against; the definitions are found at that root's default
`docs/systems` location.

## The declared systems

Each system is one directory under `docs/systems/` holding one machine-readable
`system.toml`:

```toml
# docs/systems/orders/system.toml
schema_version = 1
name = "orders"
files = ["shop/orders.py"]
```

```toml
# docs/systems/billing/system.toml
schema_version = 1
name = "billing"
files = ["shop/billing.py"]
```

A definition names a unique system and lists the individual files that belong
to it — nothing else. The `README.md` files inside the system directories are
human narrative; Minotaur reads and validates only each `system.toml`.
Membership is the exact test "is this file listed": `checkout.py` and
`ledger.py` are listed by no system and are therefore `no_system` files.

## A fresh analysis matches the committed graph

`minotaur-graph.json` was produced by the public `analyze` command shown here,
with only volatile Git snapshot metadata removed so the committed bytes stay
reproducible across commits. Re-run the same command against a scratch path and
`diff` the result against the checked-in graph to confirm they agree:

```console
$ minotaur analyze --root examples/system-walkthrough \
    --output /tmp/system-walkthrough-graph.json \
    --force examples/system-walkthrough/shop
$ minotaur query diff examples/system-walkthrough/minotaur-graph.json \
    /tmp/system-walkthrough-graph.json
no changes
```

A successful `analyze` is silent on standard output. The
[regenerate script](regenerate_system_walkthrough.py) automates this sequence
(including re-stamping the sidecar) when the fabricated sources change.

## surface: what outside files reach into the system

`surface` answers: which in-scope symbols do files outside the system
reach, through the symbol layer — `calls` or `references`? Importing the
system's module is a consumer fact, never an exposed boundary, so an outside
file that only imports `shop.orders` would expose nothing.

```console
$ minotaur query surface orders \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh
shop/orders.py  shop.orders.create_order  calls
```

The storefront checkout creates orders, so `create_order` is the one exposed
symbol; `complete_order` is only ever called from inside the system and is
internal. The same records as JSON:

```console
$ minotaur query surface orders \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh --json
{"query":"surface","refreshed":false,"results":[{"category":"system: orders","kinds":["calls"],"path":"shop/orders.py","symbol":"shop.orders.create_order"}],"stale":[]}
```

## consumers: which outside files use the system

`consumers` answers: one record per outside file participating in a
boundary-crossing relationship, carrying the distinct relationship kinds that
file contributes (`calls`, `references`, and `imports`) and the concrete
in-scope targets it reaches:

```console
$ minotaur query consumers orders \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh
shop/checkout.py (no_system)  calls: shop.orders.create_order (shop/orders.py); imports: shop.orders.create_order (shop/orders.py)
```

`checkout.py` is a `no_system` consumer: it imports `create_order` (module
layer) and calls it (symbol layer). If it had only imported the module without
a call that resolved, it would still be a consumer — through `imports` alone —
because linking against the system's module is itself a consumer fact.

```console
$ minotaur query consumers orders \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh --json
{"query":"consumers","refreshed":false,"results":[{"category":"no_system","file":"shop/checkout.py","kinds":["calls","imports"],"targets":[{"kind":"calls","label":"shop.orders.create_order","path":"shop/orders.py"},{"kind":"imports","label":"shop.orders.create_order","path":"shop/orders.py"}]}],"stale":[]}
```

Consumers of the billing system show both categories of consumer file: the
`no_system` checkout and `orders.py`, which belongs to the *other declared
system*:

```console
$ minotaur query consumers billing \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh
shop/checkout.py (no_system)  calls: shop.billing.charge (shop/billing.py); imports: shop.billing.charge (shop/billing.py)
shop/orders.py (system: orders)  calls: shop.billing.charge (shop/billing.py); imports: shop.billing.charge (shop/billing.py)
```

## system-deps: what the system itself reaches

`system-deps` answers: which target categories the system's own files reach
through outgoing `calls`, `references`, and `imports` — other named systems,
plus the explicit `no_system` category for path-carrying targets in no
declared system and `external` for path-less upstream targets. The orders
subsystem charges orders through billing and appends to the shared ledger:

```console
$ minotaur query system-deps orders \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh
no_system  calls: shop.ledger.record (shop/ledger.py); imports: shop.ledger.record (shop/ledger.py)
system: billing  calls: shop.billing.charge (shop/billing.py); imports: shop.billing.charge (shop/billing.py)
```

```console
$ minotaur query system-deps orders \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh --json
{"query":"system-deps","refreshed":false,"results":[{"category":"no_system","targets":[{"kind":"calls","label":"shop.ledger.record","path":"shop/ledger.py"},{"kind":"imports","label":"shop.ledger.record","path":"shop/ledger.py"}]},{"category":"system: billing","targets":[{"kind":"calls","label":"shop.billing.charge","path":"shop/billing.py"},{"kind":"imports","label":"shop.billing.charge","path":"shop/billing.py"}]}],"stale":[]}
```

No target is silently attributed to a system: an unlisted path-carrying target
is reported under `no_system`, never guessed into one of the named systems.
Billing's own dependencies go only to the shared ledger, so its `system-deps`
has a single `no_system` row:

```console
$ minotaur query system-deps billing \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh
no_system  calls: shop.ledger.record (shop/ledger.py); imports: shop.ledger.record (shop/ledger.py)
```

```console
$ minotaur query surface billing \
    --graph examples/system-walkthrough/minotaur-graph.json \
    --root examples/system-walkthrough --no-refresh
shop/billing.py  shop.billing.charge  calls
```

## Empty results are answers, not errors

A system whose boundary has no matches prints its own empty text form — `no
exposed symbols`, `no consumers`, or `no dependencies` — and still exits `0`.
Declared files that the analyzed graph does not contain are reported as
`minotaur: warning:` lines on standard error, never silently dropped; an
unknown system name exits `2` with the nearest declared systems.
