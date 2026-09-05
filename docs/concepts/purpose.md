# Purpose and boundary

This page states the problem Minotaur exists to solve and the boundary that
follows from it. Every feature is designed and reviewed against it.

## 1. The problem

Structural claims about a codebase are made constantly and verified rarely.

> "This symbol is only constructed in one file."
> "This change affects these four callers."
> "That subsystem is consumed only by these two modules."
> "The core package has no Qt dependency."

Engineers write these into reviews, plans, and documentation. Coding agents
write them into specifications and audits. The claims are cheap to make,
expensive to verify, and wrong often enough that the tooling around them has
learned to say "verify facts from implementations, not from names or
summaries." For the *structural* class of fact there has been no cheap
mechanism to do so. Text search is the fallback, and text search cannot tell
a call from a comment, a definition from a reference, or a resolved target
from a guess.

**Minotaur makes a structural claim checkable against evidence** — cheaply,
reproducibly at a named commit, and honestly, so that what could not be
established is reported as unresolved rather than guessed.

That is the whole product. Everything in the repository serves it.

## 2. The primitives

| Primitive | What it is | Where it lives today |
| --- | --- | --- |
| **Facts** | A canonical, validated graph of what the source establishes: definitions, containment, imports, calls, references, each with provenance and location evidence. Unresolved references are explicit nodes, never inferred edges. | `graph_model/`, `language_interpreter/` |
| **Questions** | Fixed queries that answer one structural question each from a graph: `callers`, `definitions`, `impact`, `unreferenced`, `surface`, `consumers`, `system-deps`, `context`. | `query/` |
| **Scopes** | A named boundary that makes a question answerable about *part* of a codebase — "when I say `orders`, I mean these files." A scope is a lens, not a claim: it says what you are asking about, not what is true. Shipped as committed `system.toml` definitions (one directory per system under `docs/systems`, format v1). | shipped — committed system definitions |

Two operations compare answers:

| Operation | Compares | Status |
| --- | --- | --- |
| **`diff`** | the committed structure at `HEAD` against the current working tree, or the same question across two explicit snapshots | shipped |
| **Expectations** | the current answer against a declared answer | *archived/deferred* — no implementation is planned |

The declared-answer concept is archived/deferred. No authored expected sets,
expectation files, or `expect`-style evaluation command ship in this package.

Freshness (content hashes, drift detection, the trusted-load sidecar)
guarantees that every answer is about the tree you think it is about.

## 3. Principles

1. **Source is the only author of truth.** Every Minotaur artifact is derived
   from source and can be regenerated. Declared inputs are questions and
   beliefs, never facts. A shipped scope is a read-only lens that nothing
   checks. The declared-answer concept is archived/deferred and has no
   implementation in this package.
2. **Unresolved is an answer.** A reference the interpreter cannot resolve is
   recorded as an unresolved node with its location. Minotaur never guesses a
   likely target and never attaches a confidence score.
3. **Reproducible at a commit.** A graph records the `source_control`
   commit/branch of its last real generation plus per-file content digests.
   Regeneration is gated on the analyzed content: identical content never
   rewrites, so committed bytes stay stable across commit advances and branch
   switches; a content change regenerates and re-records the snapshot. The
   content digests, not the stamp, are the freshness authority, so the stamp
   may legitimately lag `HEAD`.
4. **Differences are reported, not enforced.** `diff` reports what differs
   between two snapshots. What a difference *means* — a defect or accepted
   debt — is the caller's judgment. There is no suppression, baseline, ratchet,
   or severity vocabulary.
5. **Exit status is a fact, not a policy.** A question that could not be
   answered exits `2`. A `diff` whose compared structures differ exits `1`,
   while an identical comparison exits `0`; scripts can branch on those facts.
   Nothing else is encoded in exit codes, and no shipped query compares a
   declared answer.
6. **Nothing repository-specific.** Minotaur knows languages, not products.
   Framework conventions and product knowledge enter only through declared,
   inspectable inputs that live in the repository they describe: exclusion
   patterns and scopes. System relationships remain facts computed from the
   analyzed graph; this page does not promise a separate declared-answer or
   curated-edge package.

## 4. System definitions under this framing

A **system definition** is a scope, shipped in this version: a committed
file naming a boundary by listing individual repository files, one directory
per system under `docs/systems`. Its only job is to let questions be asked
about a named part of the codebase: `surface` (what the system exposes),
`consumers` (who outside uses it), `system-deps` (what it actually depends
on). Relationships are computed from the analyzed graph only — a definition
declares no dependencies and no expectations, and no hand-recorded
relationship data. It lists root-relative file paths only: it names a unique
system and its files, references no qualified names, and never node ids. A
scope is a lens, not a claim.

No separate declared-answer or curated-edge package is part of this contract.
A system definition is exactly the committed scope above: relationships come
from the analyzed graph only, and nothing in the shipped package is checked
against an authored expectation or augmented with hand-recorded edges.

## 5. Does a proposed feature belong in Minotaur?

Ask, in order:

1. Is it a fact the source establishes, a question about such facts, a scope
   for a question, or a comparison of answers? If none — it is out.
2. Can source alone regenerate it? If a human is the author of its truth —
   it is out.
3. Does it work on any repository in a supported language with no knowledge
   of a particular product? If not — the product knowledge must arrive as a
   declared input, or it is out.
4. Does it decide what a result *means*? If so — it is out; report the
   result and let the caller decide.
5. Does it need to be right about runtime behavior? If so — it is out until
   a dynamic-evidence layer with its own contract exists.
