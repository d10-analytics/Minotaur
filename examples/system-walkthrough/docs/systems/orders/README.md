# Orders system

The orders subsystem owns the lifecycle of an order: creation, completion,
and (in the future) cancellation.

Minotaur reads only `system.toml` in this directory. This narrative file
documents the boundary for human readers and is ignored by every query.
