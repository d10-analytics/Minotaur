# Current Python analysis

Minotaur currently provides a tested, library-level analyzer for a bounded
subset of Python source structure. It is intended to establish inspectable
static facts, not to provide a command-line workflow or a stable end-user
interface.

## Structural facts it records

For readable, syntactically valid Python files in the selected workspace, the
analyzer records file and module nodes plus top-level class and function nodes.
It also records method nodes for methods declared directly in those classes.

The resulting graph records containment between these declarations, resolvable
imports within the workspace, and direct calls whose static targets it can
resolve. Import and call relationships include source-location evidence so a
consumer can identify the reference site that established the relationship.

The output uses the canonical Minotaur wire contract described in the
[Minotaur graph format reference](../formats/minotaur-graph-v1.md).

## Diagnostics and unresolved references

If a source file cannot be read or parsed, the analyzer reports a diagnostic
for that file and continues with the other files in the workspace. The failed
file contributes no structural facts.

When an import is missing from the workspace, or a call cannot be resolved
statically, the analyzer records an explicit unresolved-reference node and a
reference relationship with source-location evidence. It does not guess a
relationship to a likely target.

## Boundaries of the current slice

The analyzer does not execute source code or import the workspace it examines.
Its relationships are not runtime-dispatch claims, and it makes no support
claim for relationships beyond this current subset. In particular, dynamic or
otherwise unresolvable calls remain unresolved rather than being inferred from
runtime behavior.

This guide deliberately does not document CLI or API usage: neither is a
supported end-user workflow yet.
