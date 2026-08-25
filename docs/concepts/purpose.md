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
| **Questions** | Fixed queries that answer one structural question each from a graph: `callers`, `definitions`, `impact`, `unreferenced`, `context`. | `query/` |
| **Scopes** | A named boundary that makes a question answerable about *part* of a codebase — "when I say `parsers`, I mean these paths." A scope is a lens, not a claim: it says what you are asking about, not what is true. | *planned* — system definitions |

Two operations compare answers:

| Operation | Compares | Status |
| --- | --- | --- |
| **`diff`** | the same question across two snapshots | shipped |
| **Expectations** | the current answer against a declared answer | *planned* |

An expectation is a persisted question with the answer its author believes
is true — "callers of `open_channel` ⊆ {`client.py`}", "system `core`
imports nothing from `qtpy`". Minotaur runs the question and reports the
difference. It is `diff` with one side declared instead of analyzed.

Freshness (content hashes, drift detection, the trusted-load sidecar)
guarantees that every answer is about the tree you think it is about.

## 3. Principles

1. **Source is the only author of truth.** Every Minotaur artifact is derived
   from source and can be regenerated. Declared inputs (scopes, expectations)
   are questions and beliefs, never facts; they are checked, not trusted.
2. **Unresolved is an answer.** A reference the interpreter cannot resolve is
   recorded as an unresolved node with its location. Minotaur never guesses a
   likely target and never attaches a confidence score.
3. **Reproducible at a commit.** A graph records its source-control snapshot
   and per-file content digests. Two people running the same question at the
   same commit get the same answer.
4. **Differences are reported, not enforced.** Minotaur says what differs
   from what was declared. What a difference *means* — a defect, accepted
   debt, a stale declaration — is the caller's judgment. There is no
   suppression, baseline, ratchet, or severity vocabulary.
5. **Exit status is a fact, not a policy.** A question that could not be
   answered exits `2`. An expectation that differs exits `1`, exactly as a
   `diff` tool does, so scripts can branch on it. Nothing else is encoded in
   exit codes.
6. **Nothing repository-specific.** Minotaur knows languages, not products.
   Framework conventions and product knowledge enter only through declared,
   inspectable inputs (exclusion patterns, curated-rule edges with a rule
   id, scopes, expectations) that live in the repository they describe.

## 4. Scopes and expectations under this framing

A **system definition** is a scope: a committed file naming a boundary
(paths), the systems it declares dependencies on, and any curated-rule edges
static analysis cannot see (registry dispatch, framework callbacks) with a
rule id. It references qualified names and paths, never node ids. Its only
job is to let questions be asked about a named part of the codebase:
`surface` (what the system exposes), `consumers` (who outside uses it),
`system-deps` (what it actually depends on).

An **expectation** is a question plus a declared answer, usually scoped to a
system. The declared dependencies in a system definition *are* expectations
("this system depends only on these"); free-standing expectations cover
ownership and containment claims that are not dependency-shaped ("this name
is defined in exactly one place"). Both are evaluated by the same comparison
and reported the same way.

Scopes without expectations are documentation with nothing to check;
expectations without scopes are glob soup. They are one design.

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
