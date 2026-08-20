"""Behavioral coverage for the bounded native Python interpreter.

Language-specific interpreter tests live beneath ``tests/language_interpreter``.
"""

from __future__ import annotations

from pathlib import Path

from minotaur.graph_model.provenance import NodeClass, RelationshipKind
from minotaur.graph_model.validation import validate_document
from minotaur.language_interpreter.contract import AnalysisResult, DiagnosticCode
from minotaur.language_interpreter.python import analyze_python_workspace


def _write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _node_id(result: AnalysisResult, label: str) -> str:
    document = result.document
    return next(node.id for node in document.nodes if node.label == label)


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
