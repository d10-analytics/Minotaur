# Billing system

The billing subsystem owns charging orders once they are complete: its
`charge` entry point records each charge in the shared ledger.

Minotaur reads only `system.toml` in this directory. This narrative file
documents the boundary for human readers and is ignored by every query.
