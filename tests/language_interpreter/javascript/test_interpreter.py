from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("esprima")

from minotaur.graph_model.provenance import CoordinateEncoding, NodeClass, RelationshipKind
from minotaur.graph_model.serialization import serialize
from minotaur.graph_model.validation import validate_document
from minotaur.language_interpreter.contract import DiagnosticCode
from minotaur.language_interpreter.javascript import analyze_javascript_files
from minotaur.language_interpreter.workspace import Workspace


def _analyze(tmp_path, files: dict[str, str]):
    paths = []
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        paths.append(path)
    return analyze_javascript_files(Workspace(tmp_path), tuple(paths))


def _node(result, label):
    return next(node for node in result.document.nodes if node.label == label)


def _edges(result, kind):
    return [edge for edge in result.document.relationships if edge.kind == kind]


def test_esm_declarations_imports_calls_and_metadata(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "lib.js": "export function helper() {}\n",
            "app.js": "import { helper } from './lib.js';\nhelper();\n",
        },
    )
    assert not result.diagnostics
    assert result.document.coordinate_encoding is CoordinateEncoding.UTF_8
    assert result.document.generated_by.name == "minotaur-javascript"
    file_node = _node(result, "lib.js")
    assert (
        file_node.extensions["minotaur-javascript"]["content_sha256"]
        == hashlib.sha256(b"export function helper() {}\n").hexdigest()
    )
    helper = _node(result, "lib.helper")
    assert helper.symbol_kind == "function"
    assert helper.extensions["minotaur-javascript"]["export_kind"] == "named"
    app_module = _node(result, "app")
    assert any(
        edge.source == app_module.id
        and edge.target == _node(result, "lib").id
        and edge.kind == RelationshipKind.IMPORTS.value
        for edge in result.document.relationships
    )
    assert any(
        edge.source == app_module.id
        and edge.target == helper.id
        and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )


def test_utf8_ranges_validate_and_serialization_is_order_independent(tmp_path):
    source = 'const emoji = "😀";\nfunction outer() { missing(); }\n'
    first = _analyze(tmp_path, {"app.js": source, "lib.js": "export const helper = () => {};"})
    app_path = tmp_path / "app.js"
    lib_path = tmp_path / "lib.js"
    second = analyze_javascript_files(Workspace(tmp_path), (lib_path, app_path))
    assert serialize(first.document) == serialize(second.document)
    function = _node(first, "app.outer")
    unresolved = _node(first, "missing")
    assert function.location.range.start.line == 1
    assert function.location.range.start.character == 0
    assert unresolved.location.range.start.line == 1
    line = source.splitlines()[1]
    assert unresolved.location.range.start.character == len(
        line[: line.index("missing")].encode("utf-8")
    )
    assert validate_document(first.document, source_text_by_path={"app.js": source}).is_valid


def test_malformed_file_is_all_or_nothing(tmp_path):
    result = _analyze(
        tmp_path, {"broken.js": "function {", "good.js": "export const ok = () => {};"}
    )
    assert any(
        d.code is DiagnosticCode.PARSE_ERROR and d.path == "broken.js" for d in result.diagnostics
    )
    assert not any(node.label.startswith("broken") for node in result.document.nodes)
    assert _node(result, "good.ok")


def test_injected_read_error_discards_only_the_failed_file(tmp_path, monkeypatch):
    bad = tmp_path / "bad.js"
    good = tmp_path / "good.js"
    bad.write_text("export const lost = () => {};", encoding="utf-8")
    good.write_text("export const kept = () => {};", encoding="utf-8")
    original_read_bytes = type(bad).read_bytes

    def read_bytes(path):
        if path == bad:
            raise OSError("synthetic read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(type(bad), "read_bytes", read_bytes)
    result = analyze_javascript_files(Workspace(tmp_path), (bad, good))
    assert any(
        d.code is DiagnosticCode.SOURCE_READ_ERROR and d.path == "bad.js"
        for d in result.diagnostics
    )
    assert not any(node.label.startswith("bad") for node in result.document.nodes)
    assert _node(result, "good.kept")


def test_later_binding_wins_and_lexical_shadow_suppresses_use(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function target() {}\n"
                "function target() {}\n"
                "target();\n"
                "function outer(target) { target(); }\n"
            )
        },
    )
    targets = [node for node in result.document.nodes if node.label == "app.target"]
    calls = [
        edge for edge in result.document.relationships if edge.kind == RelationshipKind.CALLS.value
    ]
    assert len(targets) == 2
    assert len(calls) == 1
    assert calls[0].target == targets[-1].id
    assert not any(node.reference_text == "target" for node in result.document.nodes)


def test_unsupported_imports_are_explicit_unresolved_facts(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "import value from './lib.js';\n"
                "import * as ns from 'package';\n"
                "import './setup.js';\n"
                "import { missing } from './lib.js';\n"
                "import('./lib.js');\n"
            ),
            "lib.js": "export const value = 1;\n",
        },
    )
    texts = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert {
        "./lib.js#default",
        "package#*",
        "./setup.js#side-effect",
        "./lib.js#missing",
        "./lib.js#dynamic",
    } <= texts
    assert not _edges(result, RelationshipKind.CALLS.value)
