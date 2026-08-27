"""Behavioral coverage for the bounded native Python interpreter.

Language-specific interpreter tests live beneath ``tests/language_interpreter``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_file
from minotaur.graph_model.provenance import NodeClass, Provenance, RelationshipKind
from minotaur.graph_model.validation import IssueCode, validate_document
from minotaur.language_interpreter.contract import AnalysisResult, DiagnosticCode
from minotaur.language_interpreter.python import analyze_python_files, analyze_python_workspace
from minotaur.language_interpreter.python.interpreter import _calls
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

    assert set(unresolved) == {"unavailable", "value.callback"}
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
        ("unknown", 16, 23),
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


def test_resolved_attribute_chain_suppresses_all_unresolved_bases() -> None:
    statements = ast.parse("value = a.b.c\n").body
    relationships = defaultdict(list)
    nodes = []

    _calls(
        statements,
        {"app.a.b.c": "resolved-target"},
        {},
        "app",
        "app.py",
        "origin",
        relationships,
        nodes,
        set(),
    )

    assert set(relationships) == {("origin", "resolved-target", RelationshipKind.REFERENCES.value)}
    assert not [node for node in nodes if node.node_class == NodeClass.UNRESOLVED_REFERENCE]


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
    nested = next(
        node
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE and node.reference_text == "nested"
    )
    assert (outer, nested.id, RelationshipKind.REFERENCES.value) in relationships


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
        "value",
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
