"""Behavioral coverage for the bounded native Python interpreter.

Language-specific interpreter tests live beneath ``tests/language_interpreter``.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_file
from minotaur.graph_model.provenance import NodeClass, RelationshipKind
from minotaur.graph_model.validation import IssueCode, validate_document
from minotaur.language_interpreter.contract import AnalysisResult, DiagnosticCode
from minotaur.language_interpreter.python import analyze_python_files, analyze_python_workspace
from minotaur.language_interpreter.workspace import Workspace


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
        node.reference_text
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE
    }
    assert "unknown" not in unresolved
    assert "unknown.attr" not in unresolved


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
