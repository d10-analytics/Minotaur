from __future__ import annotations

import hashlib
from pathlib import Path

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


def test_declaration_kinds_containment_and_anonymous_default_exclusion(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function top() {}\n"
                "class Thing { constructor() {} run() {} }\n"
                "const arrow = () => {};\n"
                "export default function namedDefault() {}\n"
                "export default () => {};\n"
            )
        },
    )
    labels = {node.label for node in result.document.nodes}
    assert {
        "app.top",
        "app.Thing",
        "app.Thing.constructor",
        "app.Thing.run",
        "app.arrow",
        "app.namedDefault",
    } <= labels
    assert not any(label.endswith("<anonymous>") for label in labels)
    app = _node(result, "app")
    thing = _node(result, "app.Thing")
    method = _node(result, "app.Thing.run")
    contains = {(edge.source, edge.target, edge.kind) for edge in result.document.relationships}
    assert (app.id, thing.id, RelationshipKind.CONTAINS.value) in contains
    assert (thing.id, method.id, RelationshipKind.CONTAINS.value) in contains
    assert (app.id, method.id, RelationshipKind.CONTAINS.value) not in contains


def test_calls_callback_references_and_unbound_uses_have_precise_kinds(tmp_path):
    source_line = "function use() { helper(); consume(helper); missing(); unknown; }"
    result = _analyze(
        tmp_path,
        {"app.js": (f"function helper() {{}}\n{source_line}\n")},
    )
    helper = _node(result, "app.helper")
    use = _node(result, "app.use")
    calls = [
        edge for edge in result.document.relationships if edge.kind == RelationshipKind.CALLS.value
    ]
    references = [
        edge
        for edge in result.document.relationships
        if edge.kind == RelationshipKind.REFERENCES.value
    ]
    assert any(edge.source == use.id and edge.target == helper.id for edge in calls)
    call = next(edge for edge in calls if edge.target == helper.id)
    location = call.evidence[0].locations[0]
    assert location.path == "app.js"
    assert location.range.start.character == len(
        source_line[: source_line.index("helper")].encode()
    )
    assert any(edge.source == use.id and edge.target == helper.id for edge in references)
    unresolved = {
        node.reference_text: node for node in result.document.nodes if node.reference_text
    }
    assert {"missing", "unknown", "consume"} <= unresolved.keys()
    assert not any(
        edge.target == unresolved["missing"].id and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )


def test_named_and_default_export_resolution_and_full_validation(tmp_path):
    source = "export function named() {}\nexport default function fallback() {}\n"
    result = _analyze(
        tmp_path,
        {
            "lib.js": source,
            "app.js": (
                "import { named } from './lib.js';\n"
                "import fallback from './lib.js';\n"
                "named(); fallback();\n"
            ),
        },
    )
    named = _node(result, "lib.named")
    fallback = _node(result, "lib.fallback")
    assert named.extensions["minotaur-javascript"]["export_kind"] == "named"
    assert fallback.extensions["minotaur-javascript"]["export_kind"] == "default"
    app = _node(result, "app")
    assert any(
        edge.source == app.id
        and edge.target == named.id
        and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )
    assert any(node.reference_text == "./lib.js#default" for node in result.document.nodes)
    source_by_path = {"lib.js": source, "app.js": (tmp_path / "app.js").read_text()}
    assert validate_document(result.document, source_text_by_path=source_by_path).is_valid


def test_parse_and_read_failures_have_no_nodes_or_relationships_from_failed_path(
    tmp_path, monkeypatch
):
    result = _analyze(tmp_path, {"broken.js": "return;", "good.js": "export function good() {};"})
    broken_ids = {node.id for node in result.document.nodes if node.label.startswith("broken")}
    assert not broken_ids
    assert all(
        edge.source not in broken_ids and edge.target not in broken_ids
        for edge in result.document.relationships
    )
    bad = tmp_path / "unreadable.js"
    bad.write_text("export function lost() {};", encoding="utf-8")
    good = tmp_path / "sibling.js"
    good.write_text("export function kept() {};", encoding="utf-8")
    original = type(bad).read_bytes

    def read_bytes(path):
        if path == bad:
            raise OSError("synthetic read failure")
        return original(path)

    monkeypatch.setattr(type(bad), "read_bytes", read_bytes)
    read_result = analyze_javascript_files(Workspace(tmp_path), (bad, good))
    failed_ids = {
        node.id for node in read_result.document.nodes if node.label.startswith("unreadable")
    }
    assert not failed_ids
    assert all(
        edge.source not in failed_ids and edge.target not in failed_ids
        for edge in read_result.document.relationships
    )


def test_interpreter_records_first_slice_exclusion_rationale():
    source = (
        Path(__file__).parents[3] / "src/minotaur/language_interpreter/javascript/interpreter.py"
    )
    text = source.read_text(encoding="utf-8")
    assert "Object-literal methods" in text
    assert "``this``" in text
    assert "class fields" in text


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


def test_tolerant_returned_error_is_all_or_nothing(tmp_path):
    result = _analyze(
        tmp_path,
        {"broken.js": "return;", "good.js": "export function kept() {};"},
    )
    assert any(
        d.code is DiagnosticCode.PARSE_ERROR and d.path == "broken.js" for d in result.diagnostics
    )
    assert not any(node.label.startswith("broken") for node in result.document.nodes)
    assert _node(result, "good.kept")


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


def test_nested_class_methods_keep_enclosing_owner_and_fields_are_excluded(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function outer() { class Inner { method() { nestedMissing(); } "
                "field = fieldMissing; static staticField = staticMissing; } }"
            )
        },
    )
    outer = _node(result, "app.outer")
    unresolved = [node for node in result.document.nodes if node.reference_text == "nestedMissing"]
    assert unresolved
    assert any(
        edge.source == outer.id
        and edge.target == unresolved[0].id
        and edge.kind == RelationshipKind.REFERENCES.value
        for edge in result.document.relationships
    )
    assert not any(
        node.reference_text in {"fieldMissing", "staticMissing"} for node in result.document.nodes
    )
    assert not any(node.label == "app.outer.Inner.method" for node in result.document.nodes)


def test_dynamic_import_is_module_owned_and_nonstatic_argument_is_not_a_reference(tmp_path):
    result = _analyze(tmp_path, {"app.js": "function outer() { import(specifier); }"})
    module = _node(result, "app")
    outer = _node(result, "app.outer")
    dynamic = _node(result, "import()")
    assert dynamic.reference_text == "import()"
    assert any(
        edge.source == module.id
        and edge.target == dynamic.id
        and edge.kind == RelationshipKind.REFERENCES.value
        for edge in result.document.relationships
    )
    assert not any(
        edge.source == outer.id and edge.target == dynamic.id
        for edge in result.document.relationships
    )
    assert not any(node.reference_text == "specifier" for node in result.document.nodes)


def test_unsupported_imports_are_explicit_unresolved_facts(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "import value from './lib.js';\n"
                "import * as ns from 'package';\n"
                "import bare from 'package';\n"
                "import { extensionless } from './lib';\n"
                "import './setup.js';\n"
                "import { missing } from './lib.js';\n"
                "import { ghost } from './lib.js';\n"
                "import('./lib.js');\n"
                "export { helper } from './lib.js';\n"
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
        "package#default",
        "./lib#extensionless",
        "./setup.js#side-effect",
        "./lib.js#missing",
        "./lib.js#ghost",
        "./lib.js#helper",
        "./lib.js#dynamic",
    } <= texts
    assert not _edges(result, RelationshipKind.CALLS.value)
