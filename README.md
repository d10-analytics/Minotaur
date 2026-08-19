# Minotaur

> A local-first toolkit for turning source code into
> evidence-backed software architecture maps.

Minotaur helps engineers explore how software is structured and which
relationships can be established from source code. It will interpret supported
languages, normalize those facts into one canonical graph, and create portable
interactive visualizations.

The project is intentionally designed around traceability: a graph should make
clear what was established by static analysis, what was supplied as a rule,
and what remains unresolved.

## Status

Minotaur is in early development. The public repository structure and product
boundaries are in place, and the versioned canonical graph contract exists: a
public JSON Schema, a tested Python graph model, and a semantic validator that
verifies node identities, relationship integrity, source ranges, and evidence
grouping. A tested Python analyzer and language-neutral selected-path CLI are
also available; current structural facts and limits are described in the
[Python analysis guide](docs/guides/analyze-python.md).

There is not yet a visualizer. The first end-user release will pair native
Python analysis with a self-contained interactive HTML visualizer.

## What Minotaur will do

Minotaur has two major extension boundaries:

```text
Source code
     |
     v
language_interpreter/
     |
     v
              canonical Minotaur graph
                         |
                         v
                graph_visualizer/
                         |
                         v
        Interactive HTML, DOT, SVG, and other views
```

### `language_interpreter/`

Language interpreters examine source workspaces directly and produce graph
facts from the language they understand. Each interpreter owns its language
semantics, resolution limits, source locations, and evidence.

Python is the first implemented interpreter. C#, JavaScript, and other
languages are future extensions, not current compatibility claims. See
[Create a language interpreter](docs/guides/create-a-language-interpreter.md)
for the selected-file API and registration convention for new languages.

### `graph_visualizer/`

The visualizer turns a canonical graph into an understandable exploration
experience. The first target is a single self-contained HTML file that opens
locally without a server and supports:

- zooming and panning;
- node-kind and relationship-provenance filters;
- symbol and label search;
- node details, source locations, and connected relationships;
- visual distinction between evidence types; and
- switchable graph layout direction.

Static formats such as DOT and SVG are planned alongside the interactive view.

## Evidence is part of the graph

Minotaur represents more than nodes and lines. Nodes and relationships retain
their source location, identity, relationship type, provenance, supporting
evidence, and unresolved state. There is deliberately no canonical confidence
score: how a relationship was established is stated explicitly rather than
summarized as a cross-tool probability.

This distinction matters. A static source reference and a relationship supplied
by a declared rule are useful—but they do not mean the same thing. Minotaur
keeps those differences visible instead of blending them into an apparently
certain diagram. Runtime observation and human assertion are planned as future,
separately labeled evidence types, not current ones.

## What a graph does not prove

Minotaur is a structural analysis tool, not a complete behavioral model. A
relationship in a graph is evidence that a connection exists or was observed;
it is not, by itself, proof that the connection executes in every run or in a
particular scenario.

In particular, a graph does not automatically explain:

- whether a call is reached through a particular conditional branch;
- whether dynamic dispatch, reflection, generated code, or configuration
  changes its runtime target;
- whether an observed relationship succeeds, fails, or produces a given
  outcome; or
- the product or business rationale behind a relationship.

Where available, Minotaur may retain directly observable context. Interpretation
beyond that evidence belongs in documentation or, in the future, explicitly
labeled human-authored annotations.

## Local-first and privacy-conscious

Minotaur is being built for local analysis and portable artifacts. Its intended
HTML output is self-contained and usable from `file://`; a hosted service is
not required to explore a graph.

## Initial scope

The current implementation includes:

- a versioned canonical Minotaur graph schema;
- graph validation, identity, provenance, and serialization primitives;
- a bounded native Python analyzer and selected-path CLI (currently `.py` only).

It does not yet include a visualizer, C#,
JavaScript, a hosted graph service, automatic runtime tracing, or broad
compatibility with third-party graph formats.

## Repository layout

```text
src/minotaur/
  graph_model/             # Canonical graph contract and graph operations
  language_interpreter/    # Native source-language analysis; Python first
  graph_visualizer/        # Interactive HTML and future static views

schemas/minotaur-graph/    # Versioned public graph schema
examples/                  # Synthetic, public-safe inputs and workspaces
docs/                      # Architecture, concepts, guides, and formats
tests/                     # Behavioral tests and public fixtures
```

## Contributing

Minotaur is at the foundation stage. Before adding a language interpreter or
visualization feature, its behavior and evidence model
should be specified and tested against synthetic public fixtures. Contribution
guidance will be added in `CONTRIBUTING.md` as the first implementation
milestone takes shape.

## License

Minotaur is released under the [MIT License](LICENSE).
