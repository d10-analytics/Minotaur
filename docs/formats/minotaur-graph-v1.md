# Minotaur graph format 0.1.0

Minotaur graph 0.1.0 is a portable description of one current analysis
snapshot. It records structural facts and their evidence; it does not model
Git history, a diff, runtime execution, conditional outcomes, success, or
business rationale.

The normative structural schema is
[`schemas/minotaur-graph/v1.json`](../../schemas/minotaur-graph/v1.json).
This document specifies the semantic checks that JSON Schema alone cannot
express.

## Document envelope

Every document requires the following fields:

```json
{
  "format": "minotaur-graph",
  "format_version": "0.1.0",
  "coordinate_encoding": "utf-8",
  "nodes": [],
  "relationships": []
}
```

`coordinate_encoding` is one of `utf-8`, `utf-16`, or `utf-32`. Positions are
zero-based and ranges are half-open: the start is included and the end is
excluded. A user interface converts lines to one-based values for display.

`generated_by`, `generated_at`, and `source_control` are optional snapshot
context. When present, `generated_at` is UTC RFC 3339; Git commits are full
lowercase 40- or 64-character IDs. These fields are neither node identity nor
history.

## Locations

A location is either absent or complete:

```json
{
  "path": "src/checkout.py",
  "range": {
    "start": {"line": 4, "character": 11},
    "end": {"line": 4, "character": 25}
  }
}
```

Paths are canonical repository-relative paths with slash separators. Absolute
paths and `.` or `..` components are invalid. All positions are non-negative
integers. The range end must not precede its start; when source text is
available, ranges must also fit that text.

An absent optional location means unavailable. Consumers must not infer a
location, coerce malformed coordinates, or substitute a local absolute path.

## Nodes and identity

Every node has an opaque `id`, a required `identity` descriptor, a
`node_class`, and a non-empty display `label`.

```json
{
  "id": "node:sha256:…",
  "identity": {
    "basis": "source-location",
    "namespace": "minotaur-python"
  },
  "node_class": "symbol",
  "symbol_kind": "function",
  "label": "checkout_order",
  "language": "python",
  "location": {"path": "src/checkout.py", "range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 18}}}
}
```

`node_class` is `symbol`, `file`, `resource`, or `unresolved-reference`.
Symbols require `symbol_kind`; files require `path`; unresolved references
require `reference_text`. A symbol kind is one of the schema's core terms or a
namespaced extension. `language`, when present, is a lowercase identifier;
the reserved v1 spellings are `python`, `csharp`, `javascript`, `typescript`,
and `sql`.

The ID is `node:sha256:` followed by the SHA-256 digest of the RFC 8785 JSON
Canonicalization Scheme (JCS) encoding of a canonical identity input. For
`source-location`, that input contains the basis and namespace plus node
class, symbol kind, path, and range. `file-path` uses the file path;
`upstream-identifier` uses an upstream identifier; `unresolved-reference`
uses its originating node, reference text, and location when present; and
`resource-key` uses its producer-defined resource key. A semantic validator
reconstructs and verifies the digest.

Each basis carries only the identity fields it uses: `upstream-identifier`
requires `upstream_identifier`, `unresolved-reference` requires
`originating_node`, `resource-key` requires `resource_key`, and no basis
permits any of the other two. The basis is coupled to the node class:
`file` nodes use `file-path`; `unresolved-reference` nodes use
`unresolved-reference`; `symbol` nodes use `source-location` or
`upstream-identifier`; `resource` nodes use `resource-key`,
`upstream-identifier`, or `source-location`. A `source-location` basis
requires the node `location`. The schema enforces all of these structurally.
Strings that feed the identity input (identity fields, `symbol_kind`,
`path`, `reference_text`, location paths) must not contain unpaired
surrogate code points, which JCS cannot encode; the reference model rejects
them at construction even though JSON Schema cannot express the rule.

## Relationships and evidence

A relationship has no serialized ID. Its identity is the tuple
`(source, target, kind)`.

```json
{
  "source": "node:sha256:…",
  "target": "node:sha256:…",
  "kind": "calls",
  "evidence": [
    {
      "provenance": "static-analysis",
      "producer": {"name": "minotaur-python", "version": "0.1.0"},
      "locations": [
        {
          "path": "src/checkout.py",
          "range": {"start": {"line": 4, "character": 11}, "end": {"line": 4, "character": 25}}
        }
      ]
    }
  ]
}
```

The core relationship kinds are `contains`, `imports`, `references`, `calls`,
`inherits`, and `implements`. A self-relationship is valid. Every endpoint
must identify an existing node, and a document must not contain two identical
relationship tuples.

Every relationship has at least one evidence record. Core provenance values
are `static-analysis`, `imported-graph`, and `curated-rule`. A producer is
optional but, when supplied, requires a name. Curated-rule evidence requires a
separate `rule.id`; it identifies the rule, whereas the producer identifies
the tool that applied it. Confidence is intentionally not a v1 field.

Locations on one evidence record are the inspectable call or reference sites.
They are sorted and duplicate-free. Evidence records with identical canonical
content other than locations are invalid duplicates; their locations belong in
one record. This gives a visualizer one structural edge with an ordered list of
selectable call sites.

## Extensions and ordering

Core objects reject unknown fields. Each supported object may instead carry an
`extensions` object whose non-empty keys conventionally identify a producer
namespace and whose values are objects. The detailed extension-key grammar is
intentionally deferred; consumers must not treat extension data as a core
fact.

The Python analyzer currently emits these producer extensions. On each `file`
node, `extensions["minotaur-python"]["content_sha256"]` is the lowercase
SHA-256 digest of the file's exact bytes. On an analyzed document,
`extensions["minotaur-python"]` carries integer import-resolution counts
(`imports_resolved`, `imports_unresolved`, `imports_root_mismatched`) and an
optional `import_root_hint` string, and the CLI stores
`extensions["minotaur"]["selection"]` as the sorted root-relative targets
supplied to the command (with `.` representing the root). These values are
freshness and diagnostic metadata, not identity inputs or core graph facts.
Extension values use a recursive grammar: an extension object maps non-empty
BMP keys to strings, integers, booleans, null, arrays of extension values, or
nested extension objects. Fractional values are not part of the v1 format; use
a scaled integer when exact fractional semantics are needed, and document the
scale in the extension's contract. Values that do not need arithmetic may be
represented as strings.

As of 2026-08-23, v1 constraints are tightened so that extension values cannot
contain non-integer numbers and extension object keys must remain within the
Basic Multilingual Plane. The model enforces these rules on every load and
construction path, while the schema enforces them for third-party wire input.

Array order does not change graph meaning. A canonical serializer sorts nodes
by ID, relationships by `(source, target, kind)`, locations by path and range,
and evidence by its JCS representation after locations are normalized.

## Validation and fixtures

Validation proceeds as UTF-8 decode with the required `orjson` runtime
dependency, JSON Schema validation, model construction,
semantic validation, and then canonical normalization. Invalid documents are
not normalized or rendered.

The loader uses `orjson` as its only JSON decoder; it does not fall back to the
Python standard-library decoder. This keeps every load path on one acceptance
boundary. In particular, `NaN`, `Infinity`, and `-Infinity`, integer literals
outside the signed 64-bit range, and lone-surrogate escapes are rejected while
decoding. These values are outside the v1 wire contract independently of the
schema and model rules below. `orjson` is therefore a required runtime
dependency for any installation that loads graph JSON.

Semantic validation reports every independent finding with a JSON Pointer
path and one of these codes: `node-id-mismatch`, `node-id-unverifiable`
(the identity input could not be reconstructed or JCS-encoded; no
structurally valid v1 document is known to trigger it), `node-id-duplicate`,
`identity-origin-missing` (an unresolved reference's `originating_node` is
not declared), `range-end-before-start`, `position-line-out-of-bounds` and
`position-character-out-of-bounds` (only when the consumer supplies the
referenced source text; positions count UTF-8 bytes, UTF-16 code units, or
UTF-32 code points per `coordinate_encoding`, lines split on LF, CRLF, or CR,
a line's length excludes its terminator, and a trailing terminator yields a
final empty line), `relationship-endpoint-missing`, `relationship-duplicate`,
`relationship-unresolved-target-kind` (a relationship whose target is an
`unresolved-reference` node must use kind `references`), `evidence-duplicate`
(two records on one relationship with equal provenance, producer, rule, and
extensions), and `evidence-location-duplicate`. Semantic validation never
reorders, deduplicates, coerces, or repairs a document.

The public-safe valid examples are in
[`examples/synthetic-graphs`](../../examples/synthetic-graphs/). Invalid
fixtures are in
[`tests/fixtures/minotaur-graph-v1/invalid`](../../tests/fixtures/minotaur-graph-v1/invalid/):

- `wrong-position-type.json` fails JSON Schema validation;
- `missing-curated-rule.json` fails JSON Schema validation;
- `unsafe-path.json` fails JSON Schema validation; and
- `dangling-relationship.json` passes structural shape checks but fails
  semantic endpoint validation.

### Sidecar digest file

When `analyze` writes a graph, or a user-facing graph-reading command fully
validates one, Minotaur may place an optional sidecar file at
`<GRAPH>.sha256` (for example, `my-graph.json.sha256` beside
`my-graph.json`). The sidecar contains exactly 64 lowercase hexadecimal
characters followed by one newline — the SHA-256 digest of the graph file's
exact bytes.

The sidecar is a Minotaur-internal acceleration hint. Other consumers may
ignore it entirely. Its absence or a digest mismatch does not indicate
corruption; it only means the next user-facing graph-reading command will
perform a full validation pass instead of a fast trusted load. Neither the
graph's JSON bytes, its schema, nor its `format_version` are affected by
sidecar presence or absence.
