"""Behavioral coverage for the bounded native Python interpreter.

Language-specific interpreter tests live beneath ``tests/language_interpreter``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_file
from minotaur.graph_model.provenance import NodeClass, Provenance, RelationshipKind
from minotaur.graph_model.validation import IssueCode, validate_document
from minotaur.language_interpreter.contract import AnalysisResult, DiagnosticCode
from minotaur.language_interpreter.python import analyze_python_files, analyze_python_workspace
from minotaur.language_interpreter.python.interpreter import _ScopeCallVisitor
from minotaur.language_interpreter.source_text import LineIndex
from minotaur.language_interpreter.workspace import Workspace


def _write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _node_id(result: AnalysisResult, label: str) -> str:
    document = result.document
    return next(node.id for node in document.nodes if node.label == label)


def _nodes(result: AnalysisResult, label: str) -> list:
    return [node for node in result.document.nodes if node.label == label]


def _unresolved_by_source(result: AnalysisResult) -> dict[str, set[str]]:
    """Map each origin symbol's label to the unresolved texts recorded for it."""
    labels = {node.id: node.label for node in result.document.nodes}
    texts = {
        node.id: node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    grouped: dict[str, set[str]] = {}
    for relationship in result.document.relationships:
        if relationship.target in texts:
            grouped.setdefault(labels[relationship.source], set()).add(texts[relationship.target])
    return grouped


def _edge_labels(result: AnalysisResult) -> set[tuple[str, str, str]]:
    """Return label-keyed relationship triples for readable expectations."""
    labels = {node.id: node.label for node in result.document.nodes}
    return {
        (labels[relationship.source], labels[relationship.target], relationship.kind)
        for relationship in result.document.relationships
    }


def test_python_interpreter_establishes_containment_imports_and_direct_calls(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "library.py",
        "def helper():\n    return 1\n",
    )
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Runner:\n"
        "    def run(self):\n"
        "        self.local()\n\n"
        "    def local(self):\n"
        "        helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    document = result.document
    app_module = _node_id(result, "app")
    helper = _node_id(result, "library.helper")
    runner = _node_id(result, "app.Runner")
    run = _node_id(result, "app.Runner.run")
    local = _node_id(result, "app.Runner.local")

    assert result.diagnostics == ()
    assert validate_document(document).is_valid
    assert {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in document.relationships
    } >= {
        (app_module, helper, RelationshipKind.IMPORTS.value),
        (runner, run, RelationshipKind.CONTAINS.value),
        (runner, local, RelationshipKind.CONTAINS.value),
        (run, local, RelationshipKind.CALLS.value),
        (local, helper, RelationshipKind.CALLS.value),
    }
    call = next(
        relationship
        for relationship in document.relationships
        if relationship.source == local
        and relationship.target == helper
        and relationship.kind == RelationshipKind.CALLS.value
    )
    assert call.evidence[0].locations[0].path == "app.py"
    assert call.evidence[0].locations[0].range.start.line == 7


def test_cli_records_file_content_hashes_and_root_relative_selection(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root, "z.py", "z = 1\n")
    _write(root, "a.py", "a = 1\n")
    output = tmp_path / "graph.json"

    assert (
        cli.main(
            [
                "analyze",
                "--root",
                str(root),
                "--output",
                str(output),
                str(root / "z.py"),
                str(root / "a.py"),
            ]
        )
        == 0
    )

    graph = json.loads(output.read_text(encoding="utf-8"))
    assert graph["extensions"] == {
        "minotaur": {"selection": ["a.py", "z.py"]},
        "minotaur-python": {
            "imports_resolved": 0,
            "imports_root_mismatched": 0,
            "imports_unresolved": 0,
        },
    }
    for node in graph["nodes"]:
        if node["node_class"] == "file":
            digest = hashlib.sha256((root / node["path"]).read_bytes()).hexdigest()
            assert node["extensions"]["minotaur-python"]["content_sha256"] == digest

    loaded = load_graph_file(output)
    assert loaded.canonical == graph


def test_dynamic_and_missing_imports_are_explicit_unresolved_references(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "import unavailable\n\ndef run(value):\n    value.callback()\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text: node
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert set(unresolved) == {"unavailable"}
    assert all(node.location is not None for node in unresolved.values())
    assert {relationship.kind for relationship in result.document.relationships} >= {
        RelationshipKind.REFERENCES.value,
    }
    assert validate_document(result.document).is_valid


def test_syntax_error_is_reported_without_erasing_other_workspace_facts(tmp_path: Path) -> None:
    _write(tmp_path, "valid.py", "def working():\n    return 1\n")
    _write(tmp_path, "broken.py", "def incomplete(:\n")

    result = analyze_python_workspace(tmp_path)

    assert [(diagnostic.code, diagnostic.path) for diagnostic in result.diagnostics] == [
        (DiagnosticCode.PARSE_ERROR, "broken.py")
    ]
    assert {node.label for node in result.document.nodes} >= {"valid", "valid.working"}
    assert "broken" not in {node.label for node in result.document.nodes}
    assert validate_document(result.document).is_valid


def test_type_comment_is_parsed_without_a_parse_error(tmp_path: Path) -> None:
    _write(tmp_path, "typed.py", "x: int # type: int\n")

    result = analyze_python_workspace(tmp_path)

    assert result.diagnostics == ()
    assert {node.label for node in result.document.nodes} >= {"typed.py", "typed"}
    assert any(
        node.node_class == NodeClass.FILE and node.path == "typed.py"
        for node in result.document.nodes
    )
    assert any(
        node.node_class == NodeClass.SYMBOL and node.label == "typed"
        for node in result.document.nodes
    )
    assert validate_document(result.document).is_valid


def test_package_relative_imports_analyze_without_crashing(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pkg/__init__.py",
        "from . import util\n\ndef run():\n    util.helper()\n",
    )
    _write(tmp_path, "pkg/util.py", "def helper():\n    return 1\n")

    result = analyze_python_workspace(tmp_path)
    package = _node_id(result, "pkg")
    helper = _node_id(result, "pkg.util.helper")
    run = _node_id(result, "pkg.run")

    assert result.diagnostics == ()
    assert validate_document(result.document).is_valid
    assert {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    } >= {
        (package, _node_id(result, "pkg.util"), RelationshipKind.IMPORTS.value),
        (run, helper, RelationshipKind.CALLS.value),
    }


def test_package_context_resolves_relative_imports_in_function_nested_class(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "pkg/__init__.py",
        "from .helper import helper\n"
        "\n"
        "def outer():\n"
        "    class Nested:\n"
        "        from .helper import helper\n"
        "        helper()\n"
        "    return Nested\n",
    )
    _write(tmp_path, "pkg/helper.py", "def helper():\n    return 1\n")

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "pkg.outer")
    helper = _node_id(result, "pkg.helper.helper")

    assert (
        outer,
        helper,
        RelationshipKind.CALLS.value,
    ) in {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }


def test_calls_in_nested_functions_are_attributed_to_the_outer_function(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def helper():\n    return 1\n\ndef outer():\n    def inner():\n        helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "app.helper")

    assert (outer, helper, RelationshipKind.CALLS.value) in {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }


def test_module_alias_and_module_level_calls_resolve_to_known_workspace_function(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "import library as lib\n\ndef caller():\n    lib.helper()\n\nlib.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    app = _node_id(result, "app")
    caller = _node_id(result, "app.caller")
    helper = _node_id(result, "library.helper")

    assert {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    } >= {
        (caller, helper, RelationshipKind.CALLS.value),
        (app, helper, RelationshipKind.CALLS.value),
    }
    assert "lib.helper" not in {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }


def test_module_callback_reference_is_resolved_without_misclassifying_calls(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def handler():\n    return 1\n\n"
        "def register(callback):\n    return callback\n\n"
        "def helper():\n    return 1\n\n"
        "register(handler)\n"
        "helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    handler = _node_id(result, "app.handler")
    register = _node_id(result, "app.register")
    helper = _node_id(result, "app.helper")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (module, handler, RelationshipKind.REFERENCES.value) in relationships
    assert (module, handler, RelationshipKind.CALLS.value) not in relationships
    assert (module, register, RelationshipKind.CALLS.value) in relationships
    assert (module, helper, RelationshipKind.CALLS.value) in relationships
    assert (module, helper, RelationshipKind.REFERENCES.value) not in relationships

    reference = relationships[(module, handler, RelationshipKind.REFERENCES.value)]
    location = reference.evidence[0].locations[0]
    assert location.path == "app.py"
    assert location.range.start.line == 9
    assert location.range.start.character == 9


def test_attribute_and_nested_load_references_preserve_call_and_unresolved_boundaries(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def handler():\n    return 1\n\n"
        "class Runner:\n"
        "    def on_click(self):\n        return 1\n\n"
        "    def on_callback(self):\n        return 1\n\n"
        "    def configure(self):\n"
        "        self.on_click()\n"
        "        callbacks = [self.on_callback, handler]\n"
        "        def nested():\n"
        "            return handler\n"
        "        missing = unknown\n"
        "        missing_attr = unknown.attr\n",
    )

    result = analyze_python_workspace(tmp_path)
    configure = _node_id(result, "app.Runner.configure")
    on_click = _node_id(result, "app.Runner.on_click")
    on_callback = _node_id(result, "app.Runner.on_callback")
    handler = _node_id(result, "app.handler")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (configure, on_click, RelationshipKind.CALLS.value) in relationships
    assert (configure, on_click, RelationshipKind.REFERENCES.value) not in relationships
    assert (configure, on_callback, RelationshipKind.REFERENCES.value) in relationships
    assert (configure, on_callback, RelationshipKind.CALLS.value) not in relationships
    assert (configure, handler, RelationshipKind.REFERENCES.value) in relationships
    handler_locations = (
        relationships[(configure, handler, RelationshipKind.REFERENCES.value)].evidence[0].locations
    )
    assert {location.range.start.line for location in handler_locations} == {12, 14}

    unresolved = {
        node.reference_text: node
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    assert set(unresolved) == {"unknown", "unknown.attr"}
    unresolved_nodes = [
        node
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
        and node.reference_text in {"unknown", "unknown.attr"}
    ]
    by_location = {
        (
            node.reference_text,
            node.location.range.start.line,
            node.location.range.start.character,
        ): node
        for node in unresolved_nodes
        if node.location is not None
    }
    assert set(by_location) == {
        ("unknown", 15, 18),
        ("unknown.attr", 16, 23),
    }
    for node in by_location.values():
        assert node.identity.originating_node == configure
        assert node.language == "python"
        assert node.location is not None
        reference = next(
            relationship
            for relationship in result.document.relationships
            if relationship.source == configure
            and relationship.target == node.id
            and relationship.kind == RelationshipKind.REFERENCES.value
        )
        assert reference.evidence[0].provenance == Provenance.STATIC_ANALYSIS
        assert reference.evidence[0].locations == (node.location,)
    assert "self" not in unresolved
    assert "self.on_click" not in unresolved
    assert validate_document(result.document).is_valid


def test_resolved_attribute_chain_suppresses_all_unresolved_bases(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "value = a.b.c\n")
    _write(tmp_path, "app/a/b.py", "class c:\n    pass\n")

    result = analyze_python_workspace(tmp_path)
    app = _node_id(result, "app")
    target = _node_id(result, "app.a.b.c")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert (app, target, RelationshipKind.REFERENCES.value) in relationships
    assert not unresolved.intersection({"a", "a.b"})


def test_load_argument_in_nested_call_func_is_still_a_reference(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def handler():\n    return 1\n\n"
        "def factory(callback):\n    return callback\n\n"
        "factory(handler)()\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    handler = _node_id(result, "app.handler")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (module, handler, RelationshipKind.REFERENCES.value) in relationships
    location = (
        relationships[(module, handler, RelationshipKind.REFERENCES.value)].evidence[0].locations[0]
    )
    assert location.range.start.line == 6
    assert location.range.start.character == 8


def test_callee_suppression_keeps_subexpressions_as_references(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke(table, key):\n"
        "    table[handler]()\n"
        "    (handler if key else fallback)()\n"
        '    f"{value}".join()\n',
    )

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    references = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
        and any(
            relationship.source == invoke
            and relationship.target == node.id
            and relationship.kind == RelationshipKind.REFERENCES.value
            for relationship in result.document.relationships
        )
    }

    assert {"handler", "fallback", "value"} <= references


def test_unresolved_attribute_chain_emits_one_fact_labelled_with_its_full_text(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "def invoke():\n    return obj.a.b.c.d\n")

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    # The root identifier decides whether the chain is reportable; it never
    # becomes the label, which would file every member of ``obj`` under one
    # name that no query can act on.
    assert unresolved == {"obj.a.b.c.d"}
    assert len(_nodes(result, "obj.a.b.c.d")) == 1


def test_non_atomic_member_bases_keep_parentheses_in_unresolved_labels(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke():\n    return (left + right).denominator, (a if k else b).run\n",
    )

    result = analyze_python_workspace(tmp_path)

    assert _unresolved_by_source(result)["app.invoke"] == {
        "(left + right).denominator",
        "(a if k else b).run",
        "left",
        "right",
        "a",
        "k",
        "b",
    }


def test_already_unparsed_member_bases_keep_their_full_text_labels(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def invoke():\n    return f(x).y, self.items[0].name\n")

    result = analyze_python_workspace(tmp_path)

    assert _unresolved_by_source(result)["app.invoke"] == {
        "f",
        "x",
        "f(x).y",
        "self.items",
        "self.items[0].name",
    }


def test_member_loads_and_calls_emit_the_same_full_text_facts(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "class Cfg:\n    DEFAULT = 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import Cfg\n\n"
        "def load_unknown():\n"
        "    return unknown.target\n"
        "def call_unknown():\n"
        "    return unknown.target()\n"
        "def load_member():\n"
        "    return Cfg.DEFAULT\n"
        "def call_member():\n"
        "    return Cfg.DEFAULT()\n"
        "class Declared:\n"
        "    def on_click(self):\n"
        "        return 1\n"
        "    def load(self):\n"
        "        return self.on_click\n"
        "    def call(self):\n"
        "        return self.on_click()\n"
        "class Undeclared:\n"
        "    def load(self):\n"
        "        return self.on_click\n"
        "    def call(self):\n"
        "        return self.on_click()\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = _unresolved_by_source(result)
    edges = _edge_labels(result)

    # A load and a call state the same thing about the same name, so the two
    # forms report the same fact, labelled with the whole expression.
    assert unresolved["app.load_unknown"] == {"unknown.target"}
    assert unresolved["app.call_unknown"] == {"unknown.target"}
    assert unresolved["app.load_member"] == {"Cfg.DEFAULT"}
    assert unresolved["app.call_member"] == {"Cfg.DEFAULT"}
    # The unknown member does not hide the known import it was reached through.
    assert ("app.load_member", "library.Cfg", RelationshipKind.REFERENCES.value) in edges
    assert ("app.call_member", "library.Cfg", RelationshipKind.REFERENCES.value) in edges
    assert unresolved["app.Undeclared.load"] == {"self.on_click"}
    assert unresolved["app.Undeclared.call"] == {"self.on_click"}
    assert "app.Declared.load" not in unresolved
    assert "app.Declared.call" not in unresolved
    assert (
        "app.Declared.load",
        "app.Declared.on_click",
        RelationshipKind.REFERENCES.value,
    ) in edges
    assert ("app.Declared.call", "app.Declared.on_click", RelationshipKind.CALLS.value) in edges


def test_a_resolved_member_expression_records_only_itself(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "def wrap(function):\n    return function\n")
    _write(
        tmp_path,
        "app.py",
        "import library\n\ndef use():\n    return library.wrap()\n",
    )

    result = analyze_python_workspace(tmp_path)
    use = _node_id(result, "app.use")
    facts = [
        (relationship.target, relationship.kind)
        for relationship in result.document.relationships
        if relationship.source == use
    ]

    # The reference to the module that carries an unknown member exists only
    # because the member is unknown. Once the whole expression resolves, adding
    # the module as well would count one use of ``library.wrap`` twice and make
    # every module look used by everyone who calls into it.
    assert facts == [(_node_id(result, "library.wrap"), RelationshipKind.CALLS.value)]


def test_expression_without_a_root_identifier_still_reports_its_full_text(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        'def invoke():\n    return f"{missing}".join\n',
    )

    result = analyze_python_workspace(tmp_path)

    # No guard can fire on an expression that has no root identifier at all, so
    # this is the one path that reaches emission unguarded: it must still
    # produce the expression's own fact, not fall through to silence, and the
    # interior name must survive the descent.
    assert _unresolved_by_source(result)["app.invoke"] == {"f'{missing}'.join", "missing"}


def test_chain_rooted_in_a_bound_parameter_reports_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def render(items):\n    return items[0].name\n")

    result = analyze_python_workspace(tmp_path)

    # The root identifier is ``items``, not the ``items[0]`` that precedes the
    # first dot, so the chain is recognized as a dynamic local.
    assert _unresolved_by_source(result) == {}


def test_module_member_records_the_module_and_the_unknown_member(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "def wrap(function):\n    return function\n")
    _write(
        tmp_path,
        "app.py",
        "import library\n\ndef use():\n    return library.wrap, library.MISSING\n",
    )

    result = analyze_python_workspace(tmp_path)
    edges = _edge_labels(result)

    assert ("app.use", "library.wrap", RelationshipKind.REFERENCES.value) in edges
    assert ("app.use", "library", RelationshipKind.REFERENCES.value) in edges
    assert _unresolved_by_source(result)["app.use"] == {"library.MISSING"}
    # An unresolved node labelled ``library`` would collide with the module
    # that is known and resolved on the same line.
    assert _nodes(result, "library") == [
        node for node in _nodes(result, "library") if node.node_class == NodeClass.SYMBOL
    ]


def test_module_level_subscript_chain_emits_its_root_and_full_text_once(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "value = missing[0].name\n")

    result = analyze_python_workspace(tmp_path)

    assert _unresolved_by_source(result)["app"] == {"missing", "missing[0].name"}
    assert len(_nodes(result, "missing")) == 1
    assert len(_nodes(result, "missing[0].name")) == 1


def test_member_chain_over_a_call_keeps_the_inner_call_and_its_arguments(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "library.py",
        "def build(argument):\n    return argument\n\ndef make():\n    return 1\n",
    )
    _write(
        tmp_path,
        "app.py",
        "import library\n"
        "from library import build, make\n\n"
        "def chained():\n"
        "    return library.make().c.d\n"
        "def argument_chain():\n"
        "    return build(make).c.d\n",
    )

    result = analyze_python_workspace(tmp_path)
    edges = _edge_labels(result)
    unresolved = _unresolved_by_source(result)

    # Descending a chain must not swallow the expressions inside it.
    assert ("app.chained", "library.make", RelationshipKind.CALLS.value) in edges
    assert ("app.argument_chain", "library.build", RelationshipKind.CALLS.value) in edges
    assert ("app.argument_chain", "library.make", RelationshipKind.REFERENCES.value) in edges
    assert unresolved["app.chained"] == {"library.make().c.d"}
    assert unresolved["app.argument_chain"] == {"build(make).c.d"}


def test_call_rooted_chain_records_the_call_only_once(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "library.py",
        "def build(argument):\n    return argument\n\ndef make():\n    return 1\n",
    )
    _write(
        tmp_path,
        "app.py",
        "from library import build, make\n\ndef f():\n    return build(make).c.d\n",
    )

    result = analyze_python_workspace(tmp_path)
    caller = _node_id(result, "app.f")
    build = _node_id(result, "library.build")
    edges = [
        (relationship.kind, len(relationship.evidence[0].locations))
        for relationship in result.document.relationships
        if relationship.source == caller and relationship.target == build
    ]

    # The chain descent records how ``build`` is used: it is called. Resolving
    # the chain's root as well would assert a second, different use of the same
    # identifier that the source never makes.
    assert edges == [(RelationshipKind.CALLS.value, 1)]
    assert ("app.f", "library.make", RelationshipKind.REFERENCES.value) in _edge_labels(result)
    assert _unresolved_by_source(result)["app.f"] == {"build(make).c.d"}


def test_subscript_rooted_chain_records_its_base_load_only_once(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "def table():\n    return {}\n")
    _write(
        tmp_path,
        "app.py",
        "from library import table\n\ndef g():\n    return table[0].x\n",
    )

    result = analyze_python_workspace(tmp_path)
    references = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == _node_id(result, "app.g")
        and relationship.target == _node_id(result, "library.table")
        and relationship.kind == RelationshipKind.REFERENCES.value
    ]

    # ``table`` is an ordinary load inside the subscript, recorded by the
    # descent. ``RelationshipAccumulator`` merges identical source/target/kind
    # triples into one relationship, so a duplicate fact cannot appear as a
    # second edge; it appears as a second evidence location, spanning the whole
    # chain rather than the name. Pinning the single call site is what makes
    # both the duplicate and the loss of the descent's own load visible.
    assert len(references) == 1
    assert len(references[0].evidence[0].locations) == 1
    assert references[0].evidence[0].locations[0].range.start.line == 3
    assert _unresolved_by_source(result)["app.g"] == {"table[0].x"}


def test_attribute_store_and_delete_targets_reference_their_base(tmp_path: Path) -> None:
    _write(tmp_path, "store.py", "registry = {}\ncount = 0\n")
    _write(
        tmp_path,
        "app.py",
        "import store\n\n"
        "def install():\n"
        "    store.registry = {}\n"
        "    store.count += 1\n"
        "    del store.registry\n",
    )

    result = analyze_python_workspace(tmp_path)
    reference = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == _node_id(result, "app.install")
        and relationship.target == _node_id(result, "store")
        and relationship.kind == RelationshipKind.REFERENCES.value
    ]

    # A store or delete target records no member fact, but it still loads the
    # object it assigns into.
    assert len(reference) == 1
    assert {location.range.start.line for location in reference[0].evidence[0].locations} == {
        3,
        4,
        5,
    }
    assert _unresolved_by_source(result) == {}


def test_function_local_import_is_reportable_and_lazy_reimports_resolve(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n\nclass Thing:\n    pass\n")
    _write(
        tmp_path,
        "app.py",
        "from library import Thing\n\n"
        "def local_only():\n"
        "    from library import helper\n\n"
        "    return helper()\n\n"
        "def lazy_reimport():\n"
        "    from library import Thing\n\n"
        "    return Thing()\n",
    )

    result = analyze_python_workspace(tmp_path)

    # An import binds statically. Suppressing it as if it were a local
    # assignment would erase the call instead of leaving it to a later slice.
    assert _unresolved_by_source(result)["app.local_only"] == {"helper"}
    assert ("app.lazy_reimport", "library.Thing", RelationshipKind.CALLS.value) in _edge_labels(
        result
    )


def test_implicit_class_receivers_resolve_through_the_owning_class(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    @classmethod\n"
        "    def helper(cls):\n"
        "        return 1\n"
        "    def __new__(cls, *arguments):\n"
        "        return cls.helper()\n"
        "    def __init_subclass__(cls, **keywords):\n"
        "        return cls.helper()\n"
        "    def __class_getitem__(cls, item):\n"
        "        return cls.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    edges = _edge_labels(result)

    # These three receive the class implicitly, without a decorator to say so.
    for method in ("__new__", "__init_subclass__", "__class_getitem__"):
        assert (f"app.Runner.{method}", "app.Runner.helper", RelationshipKind.CALLS.value) in edges


def test_receiver_shaped_parameter_without_eligibility_reports_its_members(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Meta(type):\n    def __call__(cls, *arguments):\n        return cls.build()\n",
    )

    result = analyze_python_workspace(tmp_path)

    # A metaclass ``__call__`` receives the class it builds, which is not the
    # class that declares the method: unresolvable, but not a dynamic local.
    assert _unresolved_by_source(result)["app.Meta.__call__"] == {"cls.build"}


def test_function_binders_suppress_parameters_and_assignment_targets(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke(positional, /, regular, *, keyword, **kwargs):\n"
        "    assigned = source\n"
        "    for loop_item in iterable:\n"
        "        pass\n"
        "    with manager as resource:\n"
        "        pass\n"
        "    try:\n"
        "        raise ValueError()\n"
        "    except Exception as error:\n"
        "        pass\n"
        "    [item for item in items]\n"
        "    (walrus := factory)\n"
        "    return positional, regular, keyword, kwargs, assigned, loop_item, resource, "
        "error, item, walrus\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert not unresolved.intersection(
        {
            "positional",
            "regular",
            "keyword",
            "kwargs",
            "assigned",
            "loop_item",
            "resource",
            "error",
            "walrus",
        }
    )
    assert {"source", "iterable", "manager", "items", "factory", "item"} <= unresolved


def test_comprehension_targets_are_local_but_walrus_names_bind_the_function(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def invoke(items):\n"
        "    before = helper\n"
        "    helper()\n"
        "    [helper for helper in items]\n"
        "    [helper() for helper in items]\n"
        "    [helper() for item in items if (marker := helper())]\n"
        "    helper()\n"
        "    after = helper\n"
        "    marker()\n",
    )

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    helper = _node_id(result, "library.helper")
    calls = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == invoke
        and relationship.target == helper
        and relationship.kind == RelationshipKind.CALLS.value
    ]

    assert len(calls) == 1
    assert len(calls[0].evidence[0].locations) == 4
    references = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == invoke
        and relationship.target == helper
        and relationship.kind == RelationshipKind.REFERENCES.value
    ]
    assert len(references) == 1
    assert len(references[0].evidence[0].locations) == 2
    assert all(
        node.reference_text != "marker"
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    )


def test_function_local_imports_del_and_match_captures_are_lexical_binders(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "import library\n"
        "from library import helper\n\n"
        "def invoke(value):\n"
        "    import library as library\n"
        "    from library import helper as helper\n"
        "    del helper\n"
        "    match value:\n"
        '        case {"capture": capture, **rest}:\n'
        "            return library.helper(), capture, rest\n"
        "        case [*items]:\n"
        "            return helper(), items\n",
    )

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    helper = _node_id(result, "library.helper")
    # ``del`` and match captures are dynamic binders, so the bare ``helper()``
    # call is suppressed. The local ``import`` is not: it names the same module
    # the module-level alias does, and ``library.helper()`` resolves through it.
    assert (invoke, helper, RelationshipKind.CALLS.value) in {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }
    assert (
        len(
            [
                relationship
                for relationship in result.document.relationships
                if relationship.source == invoke
                and relationship.target == helper
                and relationship.kind == RelationshipKind.CALLS.value
            ][0]
            .evidence[0]
            .locations
        )
        == 1
    )
    assert {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    } == set()


def test_all_match_capture_forms_are_lexical_binders(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke(value):\n"
        "    match value:\n"
        '        case {"mapping": mapping, **rest}:\n'
        "            return mapping, rest\n"
        "        case [sequence, *star]:\n"
        "            return sequence, star\n"
        "        case int(number) as typed:\n"
        "            return number, typed\n"
        "        case [or_value] | (or_value,):\n"
        "            return or_value\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert not unresolved.intersection(
        {"mapping", "rest", "sequence", "star", "number", "typed", "or_value"}
    )


def test_comprehension_generator_order_and_walrus_positions_preserve_scope(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def invoke(values):\n"
        "    ordered = [result for helper in helper() for result in helper()]\n"
        "    scoped = [\n"
        "        (first := helper())\n"
        "        for item in values\n"
        "        if (allowed := predicate(item))\n"
        "        for other in item\n"
        "        if other\n"
        "    ]\n"
        "    return ordered, scoped, first, allowed, item, other\n",
    )

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    helper = _node_id(result, "library.helper")
    calls = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == invoke
        and relationship.target == helper
        and relationship.kind == RelationshipKind.CALLS.value
    ]
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    # The first generator iterable sees the enclosing import, while the second
    # iterable sees the first generator's local ``helper`` target.
    assert len(calls) == 1
    assert unresolved == {"predicate", "item", "other"}


def test_global_names_remain_eligible_and_nonlocal_names_are_bound(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke():\n"
        "    outer = 1\n"
        "    def nested():\n"
        "        global module_name\n"
        "        nonlocal outer\n"
        "        return module_name, outer\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert "module_name" in unresolved
    assert "outer" not in unresolved


def test_nested_global_overrides_an_inherited_local_binder(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    helper = 1\n"
        "    def inner():\n"
        "        global helper\n"
        "        return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "library.helper")
    assert (outer, helper, RelationshipKind.CALLS.value) in {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }


def test_nested_global_overrides_an_inherited_local_for_reference(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    helper = 1\n"
        "    def inner():\n"
        "        global helper\n"
        "        return helper\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "library.helper")
    assert (outer, helper, RelationshipKind.REFERENCES.value) in {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }


def test_nested_global_does_not_override_same_named_comprehension_target(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    helper = 1\n"
        "    def inner():\n"
        "        global helper\n"
        "        return [helper() for helper in unknown]\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "library.helper")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert (outer, helper, RelationshipKind.CALLS.value) not in relationships
    assert unresolved == {"unknown"}


def test_nested_class_body_global_overrides_enclosing_local_with_control(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    helper = 1\n"
        "    class Global:\n"
        "        global helper\n"
        "        helper()\n"
        "    class Local:\n"
        "        helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "library.helper")
    calls = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer
        and relationship.target == helper
        and relationship.kind == RelationshipKind.CALLS.value
    ]

    assert len(calls) == 1
    assert {location.range.start.line for location in calls[0].evidence[0].locations} == {6}


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax requires Python 3.12")
def test_pep695_type_parameters_are_bound(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "type Alias[T] = list[T]\n"
        "class Box[T]:\n"
        "    value: T\n"
        "    def get[U](self, value: U) -> T:\n"
        "        return value\n"
        "def invoke[T](value: T) -> T:\n"
        "    return value\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert "T" not in unresolved
    assert "U" not in unresolved


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax requires Python 3.12")
def test_nested_generic_function_headers_bind_their_type_parameters(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer():\n    def inner[T](value: T) -> T:\n        return value\n    return inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert "T" not in unresolved


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax requires Python 3.12")
def test_type_alias_parameters_do_not_bind_same_name_later_in_module(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "type Alias[T] = list[T]\nprint(T)\n")

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert unresolved == {"T"}


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax requires Python 3.12")
def test_pep695_bounds_remain_references_while_type_parameters_are_bound(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "type Alias[T: AliasBound] = list[T]\n"
        "def invoke[T: FunctionBound](value: T) -> T:\n"
        "    return value\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert unresolved == {"AliasBound", "FunctionBound"}


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax requires Python 3.12")
def test_nested_generic_class_body_binds_its_type_parameters(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer():\n    class Box[T: ClassBound]:\n        value: T\n    return Box\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert unresolved == {"ClassBound"}


def test_local_binding_shadows_import_alias_for_dynamic_attribute_call(tmp_path: Path) -> None:
    _write(tmp_path, "other.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "import other\ndef invoke():\n    other = 1\n    return other.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    other_helper = _node_id(result, "other.helper")
    assert (
        invoke,
        other_helper,
        RelationshipKind.CALLS.value,
    ) not in {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }


def test_cls_method_calls_resolve_through_class_declarations(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    @classmethod\n"
        "    def run(cls):\n"
        "        return cls.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    run = _node_id(result, "app.Runner.run")
    helper = _node_id(result, "app.Runner.helper")
    assert (run, helper, RelationshipKind.CALLS.value) in {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }


def test_arbitrary_and_rebound_self_or_cls_do_not_resolve_as_class_receivers(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def arbitrary(receiver):\n"
        "        return receiver.helper()\n"
        "    def rebound(self):\n"
        "        self = object()\n"
        "        return self.helper()\n"
        "    @classmethod\n"
        "    def invalid(cls):\n"
        "        cls = object()\n"
        "        return cls.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "app.Runner.helper")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }
    assert all(
        (method, helper, RelationshipKind.CALLS.value) not in relationships
        for method in (
            _node_id(result, "app.Runner.arbitrary"),
            _node_id(result, "app.Runner.rebound"),
            _node_id(result, "app.Runner.invalid"),
        )
    )
    # An arbitrary or reassigned receiver is a dynamic local: the call is
    # dropped outright rather than reported as an unresolved member.
    assert _unresolved_by_source(result) == {}


def test_an_import_that_rebinds_the_receiver_disqualifies_it(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def run(self):\n"
        "        from library import self\n\n"
        "        return self.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)

    # An import binder is deliberately not a dynamic local, but it is still a
    # rebinding: whatever ``self`` names here, it is not the instance, so
    # resolving through the owning class would assert a call that cannot occur.
    assert (
        "app.Runner.run",
        "app.Runner.helper",
        RelationshipKind.CALLS.value,
    ) not in _edge_labels(result)
    assert _unresolved_by_source(result) == {}


def test_comprehension_receiver_targets_shadow_self_and_cls_only_inside_comp(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def run(self, values):\n"
        "        inside = [self.helper() for self in values]\n"
        "        return self.helper()\n"
        "    @classmethod\n"
        "    def class_run(cls, values):\n"
        "        inside = [cls.helper() for cls in values]\n"
        "        return cls.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "app.Runner.helper")
    calls = [
        relationship
        for relationship in result.document.relationships
        if relationship.target == helper and relationship.kind == RelationshipKind.CALLS.value
    ]

    # Only the calls after each comprehension can use the class receiver.
    assert len(calls) == 2
    assert {
        location.range.start.line for call in calls for location in call.evidence[0].locations
    } == {5, 9}
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    assert not unresolved.intersection({"self", "self.helper", "cls", "cls.helper"})


def test_staticmethod_self_parameter_is_not_an_instance_receiver(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    @staticmethod\n"
        "    def invoke(self):\n"
        "        return self.helper()\n"
        "    @staticmethod\n"
        "    def capture(self):\n"
        "        def nested():\n"
        "            return self.helper()\n"
        "        return nested()\n"
        "    @staticmethod\n"
        "    def bare(cls):\n"
        "        return cls()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "app.Runner.helper")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }
    unresolved = [
        node for node in result.document.nodes if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    ]

    invoke = _node_id(result, "app.Runner.invoke")
    capture = _node_id(result, "app.Runner.capture")
    assert (invoke, helper, RelationshipKind.CALLS.value) not in relationships
    assert (capture, helper, RelationshipKind.CALLS.value) not in relationships
    # A closure over a staticmethod's ``self`` parameter is the same fact one
    # frame down: an unresolved receiver-shaped member, never a class receiver.
    # The bare ``cls()`` call has no member to report and stays a local call.
    assert {node.reference_text for node in unresolved} == {"self.helper"}
    assert {
        location.range.start.line
        for node in unresolved
        for relationship in result.document.relationships
        if relationship.target == node.id
        for location in relationship.evidence[0].locations
    } == {5, 9}


def test_staticmethod_nested_receiver_like_scopes_are_not_class_receivers(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    @staticmethod\n"
        "    def invoke():\n"
        "        def nested(self):\n"
        "            return self.helper()\n"
        "        callback = lambda cls: cls.helper()\n"
        "        return nested, callback\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "app.Runner.helper")
    invoke = _node_id(result, "app.Runner.invoke")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert (invoke, helper, RelationshipKind.CALLS.value) not in relationships
    assert not unresolved.intersection({"self", "self.helper", "cls", "cls.helper"})


def test_closures_inherit_valid_instance_and_class_receivers(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def outer(self, items):\n"
        "        def nested():\n"
        "            def deeper():\n"
        "                return [self.helper() for item in items]\n"
        "            return deeper(), self.helper\n"
        "        callback = lambda: self.helper()\n"
        "        return nested(), callback()\n"
        "    @classmethod\n"
        "    def class_outer(cls):\n"
        "        def nested():\n"
        "            return cls.helper()\n"
        "        callback = lambda: cls.helper()\n"
        "        return nested(), callback()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "app.Runner.helper")
    outer = _node_id(result, "app.Runner.outer")
    class_outer = _node_id(result, "app.Runner.class_outer")
    lines_by_edge = {
        (relationship.source, relationship.kind): {
            location.range.start.line for location in relationship.evidence[0].locations
        }
        for relationship in result.document.relationships
        if relationship.target == helper
        and relationship.kind in {RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value}
    }

    # Every closure use is attributed to the enclosing emitted method, whose
    # receiver it captures unshadowed.
    assert lines_by_edge == {
        (outer, RelationshipKind.CALLS.value): {6, 8},
        (outer, RelationshipKind.REFERENCES.value): {7},
        (class_outer, RelationshipKind.CALLS.value): {13, 14},
    }
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    assert not unresolved.intersection({"self", "self.helper", "cls", "cls.helper"})


def test_nested_scopes_that_rebind_the_receiver_do_not_inherit_it(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def outer(self):\n"
        "        def nested(self):\n"
        "            return self.helper()\n"
        "        def rebound():\n"
        "            self = object()\n"
        "            return self.helper()\n"
        "        callback = lambda cls: cls.helper()\n"
        "        return nested(self), rebound(), callback(Runner)\n"
        "    @classmethod\n"
        "    def class_outer(cls):\n"
        "        def nested(cls):\n"
        "            return cls.helper()\n"
        "        callback = lambda self: self.helper()\n"
        "        return nested(cls), callback(cls)\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "app.Runner.helper")
    assert not [
        relationship
        for relationship in result.document.relationships
        if relationship.target == helper
        and relationship.kind in {RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value}
    ]
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    assert not unresolved.intersection({"self", "self.helper", "cls", "cls.helper"})


def test_super_call_has_one_unresolved_outer_member_fact(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Runner:\n    def run(self):\n        return super().run()\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    assert {text for text in unresolved if "super" in text} == {"super().run"}


def test_builtin_names_are_not_unresolved(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke(values):\n"
        "    return len(values), str(values), ValueError(), range(1), dict(), list(), "
        "sorted(values), tuple(values), bool(values)\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    assert not unresolved.intersection(
        {"len", "str", "ValueError", "range", "dict", "list", "sorted", "tuple", "bool"}
    )


def test_imported_name_that_shadows_a_builtin_is_not_suppressed(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "from externallib import list\n\ndef use():\n    return list()\n")

    result = analyze_python_workspace(tmp_path)

    # ``list`` is a builtin, but this workspace imported something else under
    # that name and the dependency must survive.
    assert _unresolved_by_source(result)["app.use"] == {"list"}


def test_module_named_after_a_builtin_does_not_exempt_that_builtin(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/list.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from pkg.list import helper\n\ndef use():\n    return list.foo, list()\n",
    )

    result = analyze_python_workspace(tmp_path)

    # Importing *from* ``pkg.list`` binds ``helper``, not ``list``. The member
    # load is an ordinary builtin use and is suppressed, and the bare call
    # resolves through the module shorthand: neither leaves the builtin behind
    # as an unresolved workspace dependency.
    assert "app.use" not in _unresolved_by_source(result)


def test_nested_scope_import_of_a_builtin_name_is_not_suppressed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer():\n"
        "    def inner():\n"
        "        from externallib import list\n\n"
        "        return list()\n"
        "    return inner\n",
    )

    result = analyze_python_workspace(tmp_path)

    # A nested scope binds imports exactly as a top-level one does; losing them
    # there would silently drop the dependency one frame down.
    assert _unresolved_by_source(result)["app.outer"] == {"list"}


def test_an_import_does_not_outlive_the_scope_that_ran_it(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer(rows):\n"
        "    def first():\n"
        "        from externallib import list\n\n"
        "        return list()\n\n"
        "    def second():\n"
        "        return list()\n\n"
        "    return first, second, list(), [list() for row in rows], lambda: list()\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved_sites = {
        (node.reference_text, location.range.start.line + 1)
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
        for relationship in result.document.relationships
        if relationship.target == node.id
        for evidence in relationship.evidence
        for location in evidence.locations
    }

    # A scope's imports are popped with it. Leaving them behind would let one
    # function's ``import`` decide what a sibling, the enclosing body, a
    # comprehension, and a lambda mean by the same name.
    assert unresolved_sites == {("list", 5)}


def test_an_import_reaches_every_scope_nested_inside_the_one_that_ran_it(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer():\n"
        "    def middle(rows):\n"
        "        from externallib import list\n\n"
        "        def inner():\n"
        "            return list()\n\n"
        "        return inner, [list() for row in rows]\n\n"
        "    return middle\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved_sites = {
        (node.reference_text, location.range.start.line + 1)
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
        for relationship in result.document.relationships
        if relationship.target == node.id
        for evidence in relationship.evidence
        for location in evidence.locations
    }

    # The frames between the ``import`` statement and the use do not bind the
    # name, so consulting only the innermost one would hide the dependency and
    # hand ``list`` back to builtin suppression. Both a nested ``def`` and a
    # comprehension are checked because each pushes a frame of its own.
    assert unresolved_sites == {("list", 6), ("list", 8)}


def test_nested_scope_import_of_a_workspace_name_stays_reportable(tmp_path: Path) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "def outer():\n"
        "    def inner():\n"
        "        from library import helper\n\n"
        "        return helper()\n"
        "    return inner\n",
    )

    result = analyze_python_workspace(tmp_path)

    # Import bindings are not dynamic locals at any depth, and resolving them
    # is a later slice's work: the call is reported, not dropped and not
    # resolved through a module-level alias that does not exist.
    assert _unresolved_by_source(result)["app.outer"] == {"helper"}
    assert ("app.outer", "library.helper", RelationshipKind.CALLS.value) not in _edge_labels(result)


def test_class_body_imports_bind_in_the_class_scope_only(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "class C:\n"
        "    from externallib import list\n\n"
        "    values = list()\n\n"
        "    def method(self):\n"
        "        return list()\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = _unresolved_by_source(result)

    # The class body binds ``list`` for itself; inside a method the class scope
    # is invisible again and the builtin is the builtin.
    assert unresolved["app.C"] == {"list"}
    assert "app.C.method" not in unresolved


def test_local_import_rebinding_a_module_alias_refuses_that_alias(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "def go():\n    return 1\n")
    _write(tmp_path, "b.py", "def go():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "import a as lib\n\ndef f():\n    import b as lib\n\n    return lib.go()\n",
    )

    result = analyze_python_workspace(tmp_path)

    # The call is to ``b.go``. Resolving it through the module's ``lib`` would
    # record a dependency on ``a`` that this scope cannot even see.
    assert ("app.f", "a.go", RelationshipKind.CALLS.value) not in _edge_labels(result)
    assert _unresolved_by_source(result)["app.f"] == {"lib.go"}


def test_local_from_import_rebinding_a_module_alias_refuses_that_alias(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "models.py", "class User:\n    pass\n")
    _write(tmp_path, "other.py", "class User:\n    pass\n")
    _write(
        tmp_path,
        "app.py",
        "from models import User\n\n"
        "def f():\n"
        "    from other import User\n\n"
        "    return User()\n\n"
        "def nested():\n"
        "    from other import User\n\n"
        "    def inner():\n"
        "        return User()\n"
        "    return inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = _unresolved_by_source(result)
    edges = _edge_labels(result)

    assert ("app.f", "models.User", RelationshipKind.CALLS.value) not in edges
    assert unresolved["app.f"] == {"User"}
    # The same rebinding seen from a nested scope, through the scope stack.
    assert ("app.nested", "models.User", RelationshipKind.CALLS.value) not in edges
    assert unresolved["app.nested"] == {"User"}


def test_inner_scope_import_outranks_an_enclosing_assignment(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer():\n"
        "    helper = build()\n"
        "    def inner():\n"
        "        from externallib import helper\n\n"
        "        return helper()\n"
        "    return inner, helper\n",
    )

    result = analyze_python_workspace(tmp_path)

    # The nearest binding of ``helper`` inside ``inner`` is its import, so the
    # call is reported; the enclosing assignment only governs ``outer``'s own
    # use of the name, which stays a dynamic local.
    assert _unresolved_by_source(result)["app.outer"] == {"build", "helper"}


def test_method_headers_see_the_class_body_imports_their_bodies_cannot(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class C:\n"
        "    from externallib import list\n\n"
        "    def run(self, value=list()):\n"
        "        return list()\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved_sites = {
        (node.reference_text, location.range.start.line + 1)
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
        for relationship in result.document.relationships
        if relationship.target == node.id
        for evidence in relationship.evidence
        for location in evidence.locations
    }

    # A method's header is evaluated in the class body, its body is not. Both
    # are attributed to the method, so only the line tells the two apart: the
    # default sees the class's ``list``, the body sees the builtin.
    assert unresolved_sites == {("list", 4)}


def test_the_nearest_scope_that_imports_a_name_decides_what_it_means(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "a.py", "def go():\n    return 1\n")
    _write(tmp_path, "b.py", "def go():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "import a as lib\n\n"
        "def outer():\n"
        "    def middle():\n"
        "        import b as lib\n\n"
        "        def inner():\n"
        "            import a as lib\n\n"
        "            return lib.go()\n"
        "        return inner\n"
        "    return middle\n",
    )

    result = analyze_python_workspace(tmp_path)

    # ``middle`` rebinds ``lib`` away from the module alias and ``inner`` binds
    # it back. Letting an outer frame win would refuse a call the source does
    # make; the call is to ``a.go`` and the module alias names ``a``.
    assert ("app.outer", "a.go", RelationshipKind.CALLS.value) in _edge_labels(result)
    assert _unresolved_by_source(result) == {}


def test_a_scopes_own_assignment_outranks_its_own_import(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer():\n"
        "    def inner(flag):\n"
        "        from externallib import list\n\n"
        "        if flag:\n"
        "            list = build()\n"
        "        return list()\n"
        "    return inner\n",
    )

    result = analyze_python_workspace(tmp_path)

    # One scope binds ``list`` twice. The assignment makes it a dynamic local,
    # which nothing static can claim, so the call reports nothing at all.
    assert _unresolved_by_source(result)["app.outer"] == {"build"}


def test_lambda_default_walrus_binds_the_enclosing_function(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke():\n    handler = lambda value=(seed := 1): value\n    return handler, seed\n",
    )

    result = analyze_python_workspace(tmp_path)

    # A lambda's defaults are evaluated in the enclosing scope, so the walrus
    # binds ``seed`` there.
    assert _unresolved_by_source(result) == {}


def test_function_headers_use_enclosing_scope_for_defaults_annotations_and_decorators(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def marker():\n    return 1\n\n"
        "@marker\n"
        "def invoke(marker: marker = marker) -> marker:\n"
        "    return marker\n",
    )

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    marker = _node_id(result, "app.marker")
    references = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }

    assert (invoke, marker, RelationshipKind.REFERENCES.value) in references


def test_nested_function_and_lambda_binders_do_not_leak_as_unresolved_names(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def invoke():\n"
        "    def nested(value):\n"
        "        return value()\n"
        "    return (lambda item: item())\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert "value" not in unresolved
    assert "item" not in unresolved


def test_callee_subscript_suppresses_table_but_preserves_key_reference(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def invoke():\n    table[handler]()\n")

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    references = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
        and any(
            relationship.source == invoke
            and relationship.target == node.id
            and relationship.kind == RelationshipKind.REFERENCES.value
            for relationship in result.document.relationships
        )
    }

    assert "handler" in references
    assert "table" not in references


def test_complex_member_callee_keeps_base_and_key_without_pseudo_reference(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def invoke():\n    obj[key].method()\n")

    result = analyze_python_workspace(tmp_path)
    invoke = _node_id(result, "app.invoke")
    references = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
        and any(
            relationship.source == invoke
            and relationship.target == node.id
            and relationship.kind == RelationshipKind.REFERENCES.value
            for relationship in result.document.relationships
        )
    }

    assert {"obj", "key"} <= references
    assert "obj[key]" not in references


def test_non_call_complex_member_reference_emits_base_without_pseudo_reference(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "def invoke():\n    return obj[key].method\n")

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert unresolved == {"obj", "key", "obj[key].method"}
    assert "obj[key]" not in unresolved


def test_builtin_member_callee_is_suppressed_but_argument_is_retained(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def invoke():\n    str.foo(value)\n")

    result = analyze_python_workspace(tmp_path)
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }

    assert unresolved == {"value"}


def test_decorator_load_references_resolve_for_module_and_direct_method_scopes(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "decorators.py", "def handler(target):\n    return target\n")
    _write(
        tmp_path,
        "app.py",
        "import decorators as pkg\n\n"
        "@pkg.handler\n"
        "def decorated():\n    return 1\n\n"
        "class Runner:\n"
        "    @pkg.handler\n"
        "    def run(self):\n"
        "        return 1\n",
    )

    result = analyze_python_workspace(tmp_path)
    decorated = _node_id(result, "app.decorated")
    run = _node_id(result, "app.Runner.run")
    handler = _node_id(result, "decorators.handler")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (decorated, handler, RelationshipKind.REFERENCES.value) in relationships
    assert (run, handler, RelationshipKind.REFERENCES.value) in relationships
    assert (decorated, handler, RelationshipKind.CALLS.value) not in relationships
    assert (run, handler, RelationshipKind.CALLS.value) not in relationships

    decorated_location = relationships[(decorated, handler, RelationshipKind.REFERENCES.value)]
    assert decorated_location.evidence[0].locations[0].range.start.line == 2
    method_location = relationships[(run, handler, RelationshipKind.REFERENCES.value)]
    assert method_location.evidence[0].locations[0].range.start.line == 7


def test_decorated_definitions_reference_the_enclosing_scope(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "decorators.py",
        "def handler(target):\n    return target\n",
    )
    _write(
        tmp_path,
        "app.py",
        "import decorators as pkg\n\n"
        "@pkg.handler\n"
        "def decorated():\n    return 1\n\n"
        "class Runner:\n"
        "    @pkg.handler\n"
        "    def run(self):\n"
        "        return 1\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    runner = _node_id(result, "app.Runner")
    decorated = _node_id(result, "app.decorated")
    run = _node_id(result, "app.Runner.run")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    module_edge = relationships[(module, decorated, RelationshipKind.REFERENCES.value)]
    method_edge = relationships[(runner, run, RelationshipKind.REFERENCES.value)]
    assert [location.range.start.line for location in module_edge.evidence[0].locations] == [2]
    assert [location.range.start.line for location in method_edge.evidence[0].locations] == [7]


def test_decorated_definition_merges_inward_evidence_and_preserves_outward_edges(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def a(target):\n    return target\n\n"
        "def b(value):\n    return value\n\n"
        "@a\n"
        "@b(1)\n"
        "def decorated():\n    return 1\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    decorated = _node_id(result, "app.decorated")
    a = _node_id(result, "app.a")
    b = _node_id(result, "app.b")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    inward = relationships[(module, decorated, RelationshipKind.REFERENCES.value)]
    assert [location.range.start.line for location in inward.evidence[0].locations] == [6, 7]
    assert (decorated, a, RelationshipKind.REFERENCES.value) in relationships
    assert (decorated, b, RelationshipKind.CALLS.value) in relationships


def test_only_decorated_top_level_symbols_get_inward_edges(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def decorator(target):\n    return target\n\n"
        "@decorator\n"
        "class Decorated:\n"
        "    pass\n\n"
        "def undecorated():\n    pass\n\n"
        "def outer():\n"
        "    @decorator\n"
        "    def nested():\n"
        "        pass\n"
        "    return nested\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    decorated = _node_id(result, "app.Decorated")
    undecorated = _node_id(result, "app.undecorated")
    outer = _node_id(result, "app.outer")
    decorator = _node_id(result, "app.decorator")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (module, decorated, RelationshipKind.REFERENCES.value) in relationships
    assert (module, undecorated, RelationshipKind.REFERENCES.value) not in relationships
    assert not any(node.label == "app.outer.nested" for node in result.document.nodes)
    assert (outer, decorator, RelationshipKind.REFERENCES.value) in relationships
    assert not any(
        node.node_class == NodeClass.UNRESOLVED_REFERENCE and node.reference_text == "nested"
        for node in result.document.nodes
    )


def test_same_named_decorated_definitions_each_get_their_own_inward_edge(
    tmp_path: Path,
) -> None:
    # ``_declarations`` keeps only the last node per name, so without a
    # per-statement target the getter and the overload stubs would have no
    # inbound edge and the undecorated real ``f`` would carry the stubs'
    # decorations as evidence.
    _write(
        tmp_path,
        "app.py",
        "from typing import overload\n\n"
        "class Box:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return 1\n\n"
        "    @value.setter\n"
        "    def value(self, v):\n"
        "        pass\n\n"
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "@overload\n"
        "def f(x: str) -> str: ...\n"
        "def f(x):\n"
        "    return x\n\n"
        "@overload\n"
        "async def g(): ...\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    box = _node_id(result, "app.Box")
    inward = {
        (relationship.source, relationship.target): [
            location.range.start.line for location in relationship.evidence[0].locations
        ]
        for relationship in result.document.relationships
        if relationship.kind == RelationshipKind.REFERENCES.value
    }
    by_line = {
        node.location.range.start.line: node.id
        for node in result.document.nodes
        if node.location is not None and node.label in {"app.Box.value", "app.f", "app.g"}
    }

    assert inward[(box, by_line[4])] == [3]
    assert inward[(box, by_line[8])] == [7]
    assert inward[(module, by_line[12])] == [11]
    assert inward[(module, by_line[14])] == [13]
    assert (module, by_line[15]) not in inward
    assert inward[(module, by_line[19])] == [18]


def test_repeated_property_definitions_keep_body_and_containment_attribution(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def getter_helper():\n"
        "    return 1\n\n"
        "def setter_helper():\n"
        "    return 2\n\n"
        "class Box:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return getter_helper()\n\n"
        "    @value.setter\n"
        "    def value(self, value):\n"
        "        return setter_helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    box = _node_id(result, "app.Box")
    getter_helper = _node_id(result, "app.getter_helper")
    setter_helper = _node_id(result, "app.setter_helper")
    value_nodes = sorted(
        _nodes(result, "app.Box.value"), key=lambda node: node.location.range.start.line
    )
    getter, setter = (node.id for node in value_nodes)
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (box, getter, RelationshipKind.CONTAINS.value) in relationships
    assert (box, setter, RelationshipKind.CONTAINS.value) in relationships
    getter_call = relationships[(getter, getter_helper, RelationshipKind.CALLS.value)]
    setter_call = relationships[(setter, setter_helper, RelationshipKind.CALLS.value)]
    assert (setter, getter_helper, RelationshipKind.CALLS.value) not in relationships
    assert getter_call.evidence[0].locations[0].range.start.line == 9
    assert setter_call.evidence[0].locations[0].range.start.line == 13


def test_overload_stubs_and_real_definition_keep_distinct_attribution(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "from typing import overload\n\n"
        "def helper():\n"
        "    return 1\n\n"
        "def helper_c():\n"
        "    return 2\n\n"
        "@overload\n"
        "def f(value: int) -> int:\n"
        "    return helper_c()\n\n"
        "@overload\n"
        "def f(value: str) -> str: ...\n\n"
        "def f(value):\n"
        "    return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    helper = _node_id(result, "app.helper")
    helper_c = _node_id(result, "app.helper_c")
    f_nodes = sorted(_nodes(result, "app.f"), key=lambda node: node.location.range.start.line)
    stub_one, stub_two, implementation = f_nodes
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    for node in f_nodes:
        assert (module, node.id, RelationshipKind.CONTAINS.value) in relationships
    assert (
        module,
        stub_one.id,
        RelationshipKind.REFERENCES.value,
    ) in relationships
    assert (
        module,
        stub_two.id,
        RelationshipKind.REFERENCES.value,
    ) in relationships
    assert (module, implementation.id, RelationshipKind.REFERENCES.value) not in relationships
    assert (implementation.id, helper, RelationshipKind.CALLS.value) in relationships
    assert (stub_one.id, helper_c, RelationshipKind.CALLS.value) in relationships
    assert (implementation.id, helper_c, RelationshipKind.CALLS.value) not in relationships


def test_duplicate_classes_keep_direct_methods_and_decorator_sources_separate(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def first():\n"
        "    return 1\n\n"
        "def second():\n"
        "    return 2\n\n"
        "def mark(value):\n"
        "    return value\n\n"
        "@mark\n"
        "class Thing:\n"
        "    def run(self):\n"
        "        return first()\n\n"
        "@mark\n"
        "class Thing:\n"
        "    @mark\n"
        "    def run(self):\n"
        "        return second()\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    first = _node_id(result, "app.first")
    second = _node_id(result, "app.second")
    mark = _node_id(result, "app.mark")
    classes = sorted(_nodes(result, "app.Thing"), key=lambda node: node.location.range.start.line)
    methods = sorted(
        _nodes(result, "app.Thing.run"), key=lambda node: node.location.range.start.line
    )
    first_class, second_class = classes
    first_method, second_method = methods
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (module, first_class.id, RelationshipKind.CONTAINS.value) in relationships
    assert (module, second_class.id, RelationshipKind.CONTAINS.value) in relationships
    assert (first_class.id, first_method.id, RelationshipKind.CONTAINS.value) in relationships
    assert (second_class.id, second_method.id, RelationshipKind.CONTAINS.value) in relationships
    assert (first_method.id, first, RelationshipKind.CALLS.value) in relationships
    assert (second_method.id, second, RelationshipKind.CALLS.value) in relationships
    assert (module, first_class.id, RelationshipKind.REFERENCES.value) in relationships
    assert (module, second_class.id, RelationshipKind.REFERENCES.value) in relationships
    assert (second_class.id, second_method.id, RelationshipKind.REFERENCES.value) in relationships
    assert (first_class.id, mark, RelationshipKind.REFERENCES.value) in relationships
    assert (second_class.id, mark, RelationshipKind.REFERENCES.value) in relationships


def test_duplicate_classes_resolve_self_calls_within_each_class_statement(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Thing:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def run(self):\n"
        "        return self.helper()\n\n"
        "class Thing:\n"
        "    def helper(self):\n"
        "        return 2\n"
        "    def run(self):\n"
        "        return self.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helpers = sorted(
        _nodes(result, "app.Thing.helper"), key=lambda node: node.location.range.start.line
    )
    runs = sorted(_nodes(result, "app.Thing.run"), key=lambda node: node.location.range.start.line)
    first_helper, second_helper = helpers
    first_run, second_run = runs
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    first_call = relationships[(first_run.id, first_helper.id, RelationshipKind.CALLS.value)]
    second_call = relationships[(second_run.id, second_helper.id, RelationshipKind.CALLS.value)]
    assert (first_run.id, second_helper.id, RelationshipKind.CALLS.value) not in relationships
    assert (second_run.id, first_helper.id, RelationshipKind.CALLS.value) not in relationships
    assert first_call.evidence[0].locations[0].range.start.line == 4
    assert second_call.evidence[0].locations[0].range.start.line == 10


def test_unique_definition_graph_relationships_remain_unchanged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def alpha():\n    return beta()\n\ndef beta():\n    return 1\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    alpha = _node_id(result, "app.alpha")
    beta = _node_id(result, "app.beta")
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (module, alpha, RelationshipKind.CONTAINS.value) in relationships
    assert (module, beta, RelationshipKind.CONTAINS.value) in relationships
    call = relationships[(alpha, beta, RelationshipKind.CALLS.value)]
    assert call.evidence[0].locations[0].range.start.line == 1


def test_from_import_targets_last_same_named_definition(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "library.py",
        "def helper():\n    return 1\n\ndef helper():\n    return 2\n",
    )
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\ndef caller():\n    return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    app_module = _node_id(result, "app")
    caller = _node_id(result, "app.caller")
    helper_nodes = sorted(
        _nodes(result, "library.helper"), key=lambda node: node.location.range.start.line
    )
    first_helper, final_helper = helper_nodes
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (app_module, final_helper.id, RelationshipKind.IMPORTS.value) in relationships
    assert (app_module, first_helper.id, RelationshipKind.IMPORTS.value) not in relationships
    assert (caller, final_helper.id, RelationshipKind.CALLS.value) in relationships
    assert (caller, first_helper.id, RelationshipKind.CALLS.value) not in relationships


def test_plain_shadowed_definition_body_and_containment_use_statement_identity(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def first():\n"
        "    return 1\n\n"
        "def second():\n"
        "    return 2\n\n"
        "def f():\n"
        "    return first()\n\n"
        "def f():\n"
        "    return second()\n",
    )

    result = analyze_python_workspace(tmp_path)
    module = _node_id(result, "app")
    first = _node_id(result, "app.first")
    second = _node_id(result, "app.second")
    f_nodes = sorted(_nodes(result, "app.f"), key=lambda node: node.location.range.start.line)
    first_f, second_f = f_nodes
    relationships = {
        (relationship.source, relationship.target, relationship.kind): relationship
        for relationship in result.document.relationships
    }

    assert (module, first_f.id, RelationshipKind.CONTAINS.value) in relationships
    assert (module, second_f.id, RelationshipKind.CONTAINS.value) in relationships
    assert (first_f.id, first, RelationshipKind.CALLS.value) in relationships
    assert (second_f.id, second, RelationshipKind.CALLS.value) in relationships
    assert (first_f.id, second, RelationshipKind.CALLS.value) not in relationships
    assert (second_f.id, first, RelationshipKind.CALLS.value) not in relationships


def test_shadowed_definitions_keep_header_and_unresolved_call_attribution(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def first_default():\n"
        "    return 1\n\n"
        "def second_default():\n"
        "    return 2\n\n"
        "class FirstType:\n"
        "    pass\n\n"
        "class SecondType:\n"
        "    pass\n\n"
        "def first_decorator(value):\n"
        "    return value\n\n"
        "def second_decorator(value):\n"
        "    return value\n\n"
        "@first_decorator\n"
        "def f(value: FirstType = first_default()) -> FirstType:\n"
        "    unknown_first\n"
        "    return missing_first()\n\n"
        "@second_decorator\n"
        "def f(value: SecondType = second_default()) -> SecondType:\n"
        "    unknown_second\n"
        "    return missing_second()\n",
    )

    result = analyze_python_workspace(tmp_path)
    first_default = _node_id(result, "app.first_default")
    second_default = _node_id(result, "app.second_default")
    first_type = _node_id(result, "app.FirstType")
    second_type = _node_id(result, "app.SecondType")
    first_decorator = _node_id(result, "app.first_decorator")
    second_decorator = _node_id(result, "app.second_decorator")
    first_f, second_f = sorted(
        _nodes(result, "app.f"), key=lambda node: node.location.range.start.line
    )
    unresolved = {
        node.reference_text: node
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }

    first_missing = unresolved["missing_first"]
    second_missing = unresolved["missing_second"]
    first_unknown = unresolved["unknown_first"]
    second_unknown = unresolved["unknown_second"]
    assert set(unresolved) == {
        "missing_first",
        "missing_second",
        "unknown_first",
        "unknown_second",
    }
    assert first_missing.identity.originating_node == first_f.id
    assert second_missing.identity.originating_node == second_f.id
    assert first_unknown.identity.originating_node == first_f.id
    assert second_unknown.identity.originating_node == second_f.id

    # Defaults produce calls; decorators and annotations produce references.
    assert (first_f.id, first_default, RelationshipKind.CALLS.value) in relationships
    assert (second_f.id, second_default, RelationshipKind.CALLS.value) in relationships
    assert (first_f.id, first_type, RelationshipKind.REFERENCES.value) in relationships
    assert (second_f.id, second_type, RelationshipKind.REFERENCES.value) in relationships
    assert (first_f.id, first_decorator, RelationshipKind.REFERENCES.value) in relationships
    assert (second_f.id, second_decorator, RelationshipKind.REFERENCES.value) in relationships
    assert (first_f.id, first_missing.id, RelationshipKind.REFERENCES.value) in relationships
    assert (second_f.id, second_missing.id, RelationshipKind.REFERENCES.value) in relationships

    assert (first_f.id, second_default, RelationshipKind.CALLS.value) not in relationships
    assert (second_f.id, first_default, RelationshipKind.CALLS.value) not in relationships
    assert (first_f.id, second_type, RelationshipKind.REFERENCES.value) not in relationships
    assert (second_f.id, first_type, RelationshipKind.REFERENCES.value) not in relationships
    assert (first_f.id, second_decorator, RelationshipKind.REFERENCES.value) not in relationships
    assert (second_f.id, first_decorator, RelationshipKind.REFERENCES.value) not in relationships
    assert (first_f.id, second_missing.id, RelationshipKind.REFERENCES.value) not in relationships
    assert (second_f.id, first_missing.id, RelationshipKind.REFERENCES.value) not in relationships
    assert (first_f.id, second_unknown.id, RelationshipKind.REFERENCES.value) not in relationships
    assert (second_f.id, first_unknown.id, RelationshipKind.REFERENCES.value) not in relationships


def test_class_bases_and_keywords_are_references_at_every_nesting_level(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Base:\n    pass\n\n"
        "class Meta(type):\n    pass\n\n"
        "class Sub(Base, metaclass=Meta):\n    pass\n\n"
        "def factory():\n"
        "    class Local(Base):\n        pass\n"
        "    return Local\n",
    )

    result = analyze_python_workspace(tmp_path)
    base = _node_id(result, "app.Base")
    meta = _node_id(result, "app.Meta")
    sub = _node_id(result, "app.Sub")
    factory = _node_id(result, "app.factory")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }

    assert (sub, base, RelationshipKind.REFERENCES.value) in relationships
    assert (sub, meta, RelationshipKind.REFERENCES.value) in relationships
    # A nested class header is evaluated in the enclosing function's scope.
    assert (factory, base, RelationshipKind.REFERENCES.value) in relationships


def test_class_body_statements_are_attributed_to_the_class_not_dropped(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "from dataclasses import dataclass, field\n\n"
        "def make_config():\n    return {}\n\n"
        "def helper():\n    return 1\n\n"
        "def inner():\n    return 2\n\n"
        "@dataclass\n"
        "class Cfg:\n"
        "    defaults = make_config()\n"
        "    data: dict = field(default_factory=make_config)\n"
        "    handler = staticmethod(helper)\n\n"
        "    def method(self):\n        return inner()\n",
    )

    result = analyze_python_workspace(tmp_path)
    cfg = _node_id(result, "app.Cfg")
    method = _node_id(result, "app.Cfg.method")
    make_config = _node_id(result, "app.make_config")
    helper = _node_id(result, "app.helper")
    inner = _node_id(result, "app.inner")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }

    assert (cfg, make_config, RelationshipKind.CALLS.value) in relationships
    assert (cfg, make_config, RelationshipKind.REFERENCES.value) in relationships
    assert (cfg, helper, RelationshipKind.REFERENCES.value) in relationships
    assert (cfg, helper, RelationshipKind.CALLS.value) not in relationships
    # Methods keep their own scope: a call in a method body is not hoisted to
    # the class merely because the class body is now visited.
    assert (method, inner, RelationshipKind.CALLS.value) in relationships
    assert (cfg, inner, RelationshipKind.CALLS.value) not in relationships


def test_nested_class_methods_are_attributed_to_the_enclosing_owner(tmp_path: Path) -> None:
    _write(tmp_path, "lib.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "import lib\n\n"
        "def outer(left, right):\n"
        "    class Inner:\n"
        "        def run(self):\n"
        "            return lib.helper(), self.other(), missing()\n"
        "        def other(self):\n"
        "            return 1\n"
        "    return (left + right).denominator, Inner\n\n"
        "class Outer:\n"
        "    class Nested:\n"
        "        def run(self):\n"
        "            return lib.helper()\n"
        "    def direct(self):\n"
        "        return lib.helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    outer_class = _node_id(result, "app.Outer")
    helper = _node_id(result, "lib.helper")
    edges = _edge_labels(result)

    assert ("app.outer", "lib.helper", RelationshipKind.CALLS.value) in edges
    assert ("app.Outer", "lib.helper", RelationshipKind.CALLS.value) in edges
    assert {
        location.range.start.line
        for relationship in result.document.relationships
        if relationship.source == outer
        and relationship.target == helper
        and relationship.kind == RelationshipKind.CALLS.value
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {5}
    assert {
        location.range.start.line
        for relationship in result.document.relationships
        if relationship.source == outer_class
        and relationship.target == helper
        and relationship.kind == RelationshipKind.CALLS.value
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {13}
    assert _unresolved_by_source(result)["app.outer"] == {
        "(left + right).denominator",
        "missing",
        "self.other",
    }


def test_nested_class_method_headers_use_the_enclosing_scope(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def decorator(target):\n    return target\n\n"
        "def default():\n    return 1\n\n"
        "def outer():\n"
        "    class Inner:\n"
        "        @decorator\n"
        "        @unknown_decorator\n"
        "        def run(value=default, annotation: decorator = None):\n"
        "            return value\n"
        "    return Inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    relationships = _edge_labels(result)
    outer = _node_id(result, "app.outer")
    decorator = _node_id(result, "app.decorator")
    default = _node_id(result, "app.default")

    assert ("app.outer", "app.decorator", RelationshipKind.REFERENCES.value) in relationships
    assert ("app.outer", "app.default", RelationshipKind.REFERENCES.value) in relationships
    assert _unresolved_by_source(result)["app.outer"] == {"unknown_decorator"}
    assert ("app.outer", "app.decorator", RelationshipKind.CALLS.value) not in relationships
    assert ("app.outer", "app.default", RelationshipKind.CALLS.value) not in relationships
    assert all(
        relationship.source != outer
        or relationship.target not in {decorator, default}
        or relationship.kind == RelationshipKind.REFERENCES.value
        for relationship in result.document.relationships
    )


def test_nested_class_method_headers_suppress_outer_function_locals(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def outer():\n"
        "    decorator = lambda target: target\n"
        "    class Inner:\n"
        "        @decorator\n"
        "        @unknown_decorator\n"
        "        def run(self):\n"
        "            return 1\n"
        "    return Inner\n",
    )

    result = analyze_python_workspace(tmp_path)

    # The enclosing function's local decorator is not a workspace dependency;
    # the unbound decorator must still be reported under that function owner.
    assert _unresolved_by_source(result)["app.outer"] == {"unknown_decorator"}


def test_nested_class_method_header_sees_preceding_assignment_but_body_does_not(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    class Inner:\n"
        "        helper = staticmethod(lambda: None)\n"
        "        @helper\n"
        "        def run(self):\n"
        "            return helper()\n"
        "    return Inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "library.helper")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == _node_id(result, "app.outer") and relationship.target == helper
    ]

    # The class-local assignment shadows the module decorator for the method
    # header, but the method body cannot see that class binding and resolves
    # its call through the enclosing module import.
    assert [
        (relationship.kind, location.range.start.line)
        for relationship in relationships
        for location in relationship.evidence[0].locations
    ] == [(RelationshipKind.CALLS.value, 7)]
    assert _unresolved_by_source(result) == {}


def test_nested_class_method_headers_use_class_bindings_in_source_order(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    class Inner:\n"
        "        @helper\n"
        "        def before(self):\n"
        "            return 1\n"
        "        helper = staticmethod(lambda: None)\n"
        "        @helper\n"
        "        def after(self):\n"
        "            return 1\n"
        "    return Inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "library.helper")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == helper
    ]

    # The first header executes before the class-local assignment exists and
    # therefore sees the module import. The later header sees the class-local
    # binding and must not retain that outer dependency.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 5),
    }
    assert _unresolved_by_source(result) == {}


def test_nested_class_method_headers_use_class_imports_in_source_order(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    class Inner:\n"
        "        @helper\n"
        "        def before(self):\n"
        "            return 1\n"
        "        from other import helper\n"
        "        @helper\n"
        "        def after(self):\n"
        "            return 1\n"
        "    return Inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "library.helper")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == helper
    ]

    # The first header executes before the class-local import and sees the
    # module import. The later header sees the new local import and must not
    # retain that outer dependency.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 5),
    }
    assert _unresolved_by_source(result)["app.outer"] == {"helper"}


def test_nested_class_import_replaces_preceding_dynamic_header_binding(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    class Inner:\n"
        "        helper = staticmethod(lambda target: target)\n"
        "        @helper\n"
        "        def before(self):\n"
        "            return 1\n"
        "        from other import helper\n"
        "        @helper\n"
        "        def after(self):\n"
        "            return helper()\n"
        "    return Inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    module_helper = _node_id(result, "library.helper")
    resolved_helper_uses = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == module_helper
    ]

    # The dynamic class assignment suppresses the earlier decorator. The
    # subsequent class import replaces that assignment for the later header,
    # so it is reported as an unresolved local import instead of being
    # suppressed or attributed to the module import. The method body remains
    # isolated from both class bindings and resolves through the module.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in resolved_helper_uses
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {(RelationshipKind.CALLS.value, 12)}
    assert _unresolved_by_source(result) == {"app.outer": {"helper"}}
    unresolved_ids = {
        node.id
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE and node.reference_text == "helper"
    }
    assert {
        location.range.start.line + 1
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target in unresolved_ids
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {10}


def test_nested_class_method_header_sees_preceding_import_but_body_does_not(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    class Inner:\n"
        "        from other import helper\n"
        "        @helper\n"
        "        def run(self):\n"
        "            return helper()\n"
        "    return Inner\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "library.helper")
    outer = _node_id(result, "app.outer")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == helper
    ]

    # The class-local import prevents the decorator from being attributed to
    # the outer import. The method body remains isolated from the class import
    # and therefore still resolves the enclosing module binding.
    assert [
        (relationship.kind, location.range.start.line)
        for relationship in relationships
        for location in relationship.evidence[0].locations
    ] == [(RelationshipKind.CALLS.value, 7)]
    assert _unresolved_by_source(result)["app.outer"] == {"helper"}


def test_nested_methods_skip_every_enclosing_class_namespace(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "def outer():\n"
        "    class Outer:\n"
        "        helper = staticmethod(lambda: None)\n"
        "        class Assigned:\n"
        "            @helper\n"
        "            def run(self):\n"
        "                return helper()\n"
        "        class Imported:\n"
        "            from other import helper\n"
        "            @helper\n"
        "            def run(self):\n"
        "                return helper()\n"
        "    return Outer\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "library.helper")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == helper
    ]

    # Class namespaces are not lexical enclosures: the first nested decorator
    # skips Outer.helper and sees the module import. Both method bodies also
    # see the module import; the second nested class's own import suppresses
    # only its decorator and leaves that header unresolved.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 7),
        (RelationshipKind.CALLS.value, 9),
        (RelationshipKind.CALLS.value, 14),
    }
    assert _unresolved_by_source(result) == {"app.outer": {"helper"}}


def test_direct_nested_methods_skip_the_enclosing_class_namespace(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Outer:\n"
        "    helper = staticmethod(lambda: None)\n"
        "    class Assigned:\n"
        "        @helper\n"
        "        def run(self):\n"
        "            return helper()\n"
        "    class Imported:\n"
        "        from other import helper\n"
        "        @helper\n"
        "        def run(self):\n"
        "            return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.Outer")
    helper = _node_id(result, "library.helper")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == helper
    ]

    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 6),
        (RelationshipKind.CALLS.value, 8),
        (RelationshipKind.CALLS.value, 13),
    }
    assert _unresolved_by_source(result) == {"app.Outer": {"helper"}}


def test_direct_nested_class_headers_skip_the_enclosing_class_namespace(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Outer:\n"
        "    helper = staticmethod(lambda: None)\n"
        "    class Inner(helper):\n"
        "        @helper\n"
        "        def run(self):\n"
        "            return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.Outer")
    helper = _node_id(result, "library.helper")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == helper
    ]

    # The nested class header and its method header skip Outer.helper and see
    # the module import. Its method body is class-scope-isolated and sees that
    # same module import as well.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 5),
        (RelationshipKind.REFERENCES.value, 6),
        (RelationshipKind.CALLS.value, 8),
    }
    assert _unresolved_by_source(result) == {}


def test_direct_class_method_headers_use_assignment_source_order_and_body_scope(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Runner:\n"
        "    @helper\n"
        "    def before(self):\n"
        "        return 1\n"
        "    helper = staticmethod(lambda: None)\n"
        "    @helper\n"
        "    def after(self):\n"
        "        return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "library.helper")
    method_sources = {
        _node_id(result, "app.Runner.before"),
        _node_id(result, "app.Runner.after"),
    }
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source in method_sources and relationship.target == helper
    ]

    # The first header runs before the class-local assignment and sees the
    # module import. The later header sees that assignment, while its body
    # still resolves ``helper`` through the enclosing module scope.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 4),
        (RelationshipKind.CALLS.value, 10),
    }
    assert _unresolved_by_source(result) == {}


def test_direct_async_class_method_headers_use_assignment_source_order_and_body_scope(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Runner:\n"
        "    @helper\n"
        "    async def before(self):\n"
        "        return 1\n"
        "    helper = staticmethod(lambda: None)\n"
        "    @helper\n"
        "    async def after(self):\n"
        "        return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "library.helper")
    method_sources = {
        _node_id(result, "app.Runner.before"),
        _node_id(result, "app.Runner.after"),
    }
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source in method_sources and relationship.target == helper
    ]

    # Async methods use the same class-header scope: the first decorator sees
    # the module import, the later one sees the class-local assignment, and
    # the later body still resolves through the enclosing module scope.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 4),
        (RelationshipKind.CALLS.value, 10),
    }
    assert _unresolved_by_source(result) == {}


def test_direct_class_method_headers_use_import_source_order_and_body_scope(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Runner:\n"
        "    @helper\n"
        "    def before(self):\n"
        "        return 1\n"
        "    from other import helper\n"
        "    @helper\n"
        "    def after(self):\n"
        "        return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    helper = _node_id(result, "library.helper")
    before = _node_id(result, "app.Runner.before")
    after = _node_id(result, "app.Runner.after")
    relationships = [
        relationship
        for relationship in result.document.relationships
        if relationship.source in {before, after} and relationship.target == helper
    ]

    # The first header sees the enclosing import. The class-local import
    # shadows it for the later header, but not for the method body.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in relationships
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {
        (RelationshipKind.REFERENCES.value, 4),
        (RelationshipKind.CALLS.value, 10),
    }
    assert _unresolved_by_source(result) == {"app.Runner.after": {"helper"}}


def test_direct_class_import_replaces_preceding_dynamic_header_binding(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Runner:\n"
        "    helper = staticmethod(lambda target: target)\n"
        "    @helper\n"
        "    def before(self):\n"
        "        return 1\n"
        "    from other import helper\n"
        "    @helper\n"
        "    def after(self):\n"
        "        return helper()\n",
    )

    result = analyze_python_workspace(tmp_path)
    before = _node_id(result, "app.Runner.before")
    after = _node_id(result, "app.Runner.after")
    module_helper = _node_id(result, "library.helper")
    resolved_helper_uses = [
        relationship
        for relationship in result.document.relationships
        if relationship.source in {before, after} and relationship.target == module_helper
    ]

    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in resolved_helper_uses
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {(RelationshipKind.CALLS.value, 11)}
    assert _unresolved_by_source(result) == {"app.Runner.after": {"helper"}}
    unresolved_ids = {
        node.id
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE and node.reference_text == "helper"
    }
    assert {
        location.range.start.line + 1
        for relationship in result.document.relationships
        if relationship.source == after and relationship.target in unresolved_ids
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {9}


def test_direct_nested_class_import_replaces_compound_dynamic_binding(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "library.py", "def helper():\n    return 1\n")
    _write(tmp_path, "other.py", "def helper():\n    return 2\n")
    _write(
        tmp_path,
        "app.py",
        "from library import helper\n\n"
        "class Outer:\n"
        "    class Inner:\n"
        "        if True:\n"
        "            helper = staticmethod(lambda target: target)\n"
        "        @helper\n"
        "        def before(self):\n"
        "            return self.missing()\n"
        "        from other import helper\n"
        "        @helper\n"
        "        def after(self):\n"
        "            return helper(), self.missing()\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.Outer")
    module_helper = _node_id(result, "library.helper")
    resolved_helper_uses = [
        relationship
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target == module_helper
    ]

    # The nested class's compound assignment suppresses its first header. Its
    # own later import replaces that dynamic possibility for the second header,
    # while both method bodies retain module scope and receiver-shaped members.
    assert {
        (relationship.kind, location.range.start.line + 1)
        for relationship in resolved_helper_uses
        for evidence in relationship.evidence
        for location in evidence.locations
    } == {(RelationshipKind.CALLS.value, 13)}
    unresolved = _unresolved_by_source(result)
    assert unresolved["app.Outer"] == {"helper", "self.missing"}
    unresolved_ids = {
        node.id
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    unresolved_locations = {
        (node.reference_text, location.range.start.line + 1)
        for relationship in result.document.relationships
        if relationship.source == outer and relationship.target in unresolved_ids
        for node in result.document.nodes
        if node.id == relationship.target
        for evidence in relationship.evidence
        for location in evidence.locations
    }
    assert unresolved_locations == {
        ("self.missing", 9),
        ("helper", 11),
        ("self.missing", 13),
    }


def test_nested_class_method_receiver_does_not_use_outer_class_declarations(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "class Outer:\n"
        "    def other(self):\n"
        "        return 1\n"
        "    class Inner:\n"
        "        def run(self):\n"
        "            return self.other()\n",
    )

    result = analyze_python_workspace(tmp_path)

    assert _unresolved_by_source(result)["app.Outer"] == {"self.other"}
    assert ("app.Outer", "app.Outer.other", RelationshipKind.CALLS.value) not in _edge_labels(
        result
    )


def test_nested_class_method_three_level_scope_stack_and_global_nonlocal(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "lib.py", "def helper():\n    return 1\n")
    source = (
        "from lib import helper\n\n"
        "def outer():\n"
        "    helper = object()\n"
        "    local = object()\n"
        "    class First:\n"
        "        def middle(self):\n"
        "            class Second:\n"
        "                def leaf(self):\n"
        "                    global helper\n"
        "                    nonlocal local\n"
        "                    return lib.helper(), helper(), local()\n"
        "            return Second\n"
        "    return First\n"
    )
    _write(tmp_path, "app.py", source)

    visitor = _ScopeCallVisitor("app")
    visitor.visit(ast.parse(source))
    assert visitor._scope_bound_names == []
    assert visitor._scope_global_names == []
    assert visitor._scope_shadow_names == []
    assert visitor._scope_import_targets == []
    assert visitor._scope_receiver_overrides == []
    assert visitor._scope_nested_class_method == []
    assert visitor._scope_is_class == []

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    helper = _node_id(result, "lib.helper")
    relationships = _edge_labels(result)
    assert ("app.outer", "lib.helper", RelationshipKind.CALLS.value) in relationships
    assert _unresolved_by_source(result).get("app.outer", set()) == set()
    assert any(
        relationship.source == outer
        and relationship.target == helper
        and relationship.kind == RelationshipKind.CALLS.value
        for relationship in result.document.relationships
    )


def test_function_signature_defaults_and_annotations_are_attributed_to_the_function(
    tmp_path: Path,
) -> None:
    # Defaults and annotations are evaluated outside the body, so a visitor
    # given only `statement.body` misses them; nested definitions already saw
    # them through generic_visit. Both nesting levels must agree.
    _write(
        tmp_path,
        "app.py",
        "def default_cb():\n    return 0\n\n"
        "def kw_default():\n    return 1\n\n"
        "class Handler:\n    pass\n\n"
        "class Result:\n    pass\n\n"
        "def top(cb=default_cb, *, hook=kw_default) -> Result:\n"
        "    return cb()\n\n"
        "class Runner:\n"
        "    def run(self, handler: Handler = default_cb):\n"
        "        return handler\n",
    )

    result = analyze_python_workspace(tmp_path)
    top = _node_id(result, "app.top")
    run = _node_id(result, "app.Runner.run")
    default_cb = _node_id(result, "app.default_cb")
    kw_default = _node_id(result, "app.kw_default")
    handler = _node_id(result, "app.Handler")
    result_class = _node_id(result, "app.Result")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }

    assert (top, default_cb, RelationshipKind.REFERENCES.value) in relationships
    assert (top, kw_default, RelationshipKind.REFERENCES.value) in relationships
    assert (top, result_class, RelationshipKind.REFERENCES.value) in relationships
    assert (run, handler, RelationshipKind.REFERENCES.value) in relationships
    assert (run, default_cb, RelationshipKind.REFERENCES.value) in relationships
    # A default is a reference to the callable, never a call of it.
    assert (top, default_cb, RelationshipKind.CALLS.value) not in relationships


def test_signature_references_match_between_nested_and_top_level_definitions(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "def default_cb():\n    return 0\n\n"
        "class Handler:\n    pass\n\n"
        "def outer():\n"
        "    def inner(cb=default_cb, handler: Handler = None):\n"
        "        return cb\n\n"
        "    return inner\n\n"
        "def peer(cb=default_cb, handler: Handler = None):\n"
        "    return cb\n",
    )

    result = analyze_python_workspace(tmp_path)
    outer = _node_id(result, "app.outer")
    peer = _node_id(result, "app.peer")
    default_cb = _node_id(result, "app.default_cb")
    handler = _node_id(result, "app.Handler")
    relationships = {
        (relationship.source, relationship.target, relationship.kind)
        for relationship in result.document.relationships
    }

    for target in (default_cb, handler):
        assert (outer, target, RelationshipKind.REFERENCES.value) in relationships
        assert (peer, target, RelationshipKind.REFERENCES.value) in relationships


def test_duplicate_imports_are_deduplicated_to_one_node_per_triple(tmp_path: Path) -> None:
    """AC-08 (a): repeated names in one import statement yield one node each."""
    _write(
        tmp_path,
        "app.py",
        "import ghost_mod, ghost_mod\nfrom ghost import a, a\n",
    )

    result = analyze_python_workspace(tmp_path)
    unresolved = [
        node for node in result.document.nodes if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    ]

    # Two distinct triples (different reference_text and statement location),
    # each repeated once — dedup keeps exactly two nodes.
    assert len(unresolved) == 2
    assert {node.reference_text for node in unresolved} == {"ghost_mod", "ghost.a"}

    # Each node's REFERENCES relationship has exactly one evidence location,
    # because _relationship collapses equal locations via dict.fromkeys.
    for node in unresolved:
        refs = [
            rel
            for rel in result.document.relationships
            if rel.target == node.id and rel.kind == RelationshipKind.REFERENCES.value
        ]
        assert len(refs) == 1
        assert len(refs[0].evidence[0].locations) == 1

    # The document is valid — no duplicate node IDs.
    validation = validate_document(result.document)
    assert validation.is_valid
    assert not any(issue.code == IssueCode.NODE_ID_DUPLICATE for issue in validation.issues)


@pytest.mark.slow
def test_unresolved_dedup_completes_25000_sites_within_three_seconds(tmp_path: Path) -> None:
    """AC-08 (b): 25,000 distinct unresolved sites complete in <= 3 seconds.

    This is an absolute wall-clock gate on a single size point, not a scaling
    comparison. It discriminates the set-based dedup (this branch) from the
    O(n^2) scan it replaced: observed 0.56s on this branch vs 5.38s on main,
    for 25,000 sites, measured 2026-08-23.
    """
    site_count = 25_000
    # Generate a module with site_count distinct unresolved import statements.
    # Each `import unique_N` is a distinct (origin, reference_text, location)
    # triple, so every call to _unresolved exercises the dedup path.
    lines = [f"import unique_{i}" for i in range(site_count)]
    source = "\n".join(lines) + "\n"
    _write(tmp_path, "generated.py", source)

    workspace = Workspace(tmp_path)
    files = (tmp_path / "generated.py",)

    start = time.perf_counter()
    result = analyze_python_files(workspace, files)
    elapsed = time.perf_counter() - start

    unresolved = [
        node for node in result.document.nodes if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    ]
    assert len(unresolved) == site_count
    assert elapsed <= 3.0, f"analyze took {elapsed:.2f}s, expected <= 3.0s"


def test_python_module_location_uses_shared_index_without_moving_trailing_end(
    tmp_path: Path,
) -> None:
    source = "def helper():\n    return 1\n"
    _write(tmp_path, "app.py", source)
    result = analyze_python_workspace(tmp_path)
    module = next(node for node in result.document.nodes if node.label == "app")
    assert module.location is not None
    assert module.location.range.end == LineIndex(source).end_position()
    assert module.location.range.end.line == 1
    assert module.location.range.end.character == len(b"    return 1")


def test_python_form_feed_source_stays_valid_under_shared_line_rules(tmp_path: Path) -> None:
    source = "value = 1\f\n"
    _write(tmp_path, "app.py", source)
    result = analyze_python_workspace(tmp_path)
    assert result.diagnostics == ()
    assert validate_document(result.document, source_text_by_path={"app.py": source}).is_valid
