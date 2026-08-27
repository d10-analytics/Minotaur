from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

import esprima
import pytest

from minotaur.graph_model.location import encoded_length, split_lines
from minotaur.graph_model.provenance import CoordinateEncoding, NodeClass, RelationshipKind
from minotaur.graph_model.serialization import serialize
from minotaur.graph_model.validation import validate_document
from minotaur.language_interpreter.contract import DiagnosticCode
from minotaur.language_interpreter.javascript import analyze_javascript_files
from minotaur.language_interpreter.python import analyze_python_files
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


def test_esprima_dependency_is_installed_and_esm_parses(tmp_path):
    assert esprima.__name__ == "esprima"
    assert importlib.metadata.version("esprima2") == "6.0.0"
    result = _analyze(
        tmp_path,
        {"app.js": "export { value } from './lib.js';\nimport { value } from './lib.js';\n"},
    )
    assert result.diagnostics == ()


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
    symbols = {
        (node.label, node.symbol_kind)
        for node in result.document.nodes
        if node.symbol_kind is not None
    }
    assert symbols == {
        ("app", "module"),
        ("app.top", "function"),
        ("app.Thing", "class"),
        ("app.Thing.constructor", "method"),
        ("app.Thing.run", "method"),
        ("app.arrow", "function"),
        ("app.namedDefault", "function"),
    }
    app = _node(result, "app")
    thing = _node(result, "app.Thing")
    method = _node(result, "app.Thing.run")
    contains = {(edge.source, edge.target, edge.kind) for edge in result.document.relationships}
    file_node = _node(result, "app.js")
    assert {
        (file_node.id, app.id, RelationshipKind.CONTAINS.value),
        (app.id, _node(result, "app.top").id, RelationshipKind.CONTAINS.value),
        (app.id, thing.id, RelationshipKind.CONTAINS.value),
        (app.id, _node(result, "app.arrow").id, RelationshipKind.CONTAINS.value),
        (app.id, _node(result, "app.namedDefault").id, RelationshipKind.CONTAINS.value),
        (thing.id, _node(result, "app.Thing.constructor").id, RelationshipKind.CONTAINS.value),
        (thing.id, method.id, RelationshipKind.CONTAINS.value),
    } == contains
    assert not any("<anonymous>" in node.label for node in result.document.nodes)


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


def test_excluded_object_methods_this_and_class_fields_emit_no_facts(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "const object = { method() { objectTarget(); } };\n"
                "class Thing { field = fieldTarget; static staticField = staticTarget; "
                "run() { return this.field; } }\n"
            )
        },
    )
    excluded = {"method", "field", "staticField", "objectTarget", "fieldTarget", "staticTarget"}
    assert not any(any(name in node.label for name in excluded) for node in result.document.nodes)
    assert not any(
        any(name in endpoint for name in excluded)
        for edge in result.document.relationships
        for endpoint in (edge.source, edge.target)
    )
    assert not any(
        node.reference_text in excluded
        for node in result.document.nodes
        if node.reference_text is not None
    )


def test_utf8_ranges_validate_and_serialization_is_order_independent(tmp_path):
    source = 'const emoji = "😀"; function outer() { missing(); }\n'
    first = _analyze(tmp_path, {"app.js": source, "lib.js": "export const helper = () => {};"})
    app_path = tmp_path / "app.js"
    lib_path = tmp_path / "lib.js"
    second = analyze_javascript_files(Workspace(tmp_path), (lib_path, app_path))
    assert serialize(first.document) == serialize(second.document)
    function = _node(first, "app.outer")
    unresolved = _node(first, "missing")
    function_prefix = source.index("function")
    missing_prefix = source.index("missing")
    assert function.location.range.start.line == 0
    assert function.location.range.start.character == len(source[:function_prefix].encode("utf-8"))
    assert unresolved.location.range.start.line == 0
    assert unresolved.location.range.start.character == len(source[:missing_prefix].encode("utf-8"))
    assert function.location.range.start.character != function_prefix
    assert (
        function.location.range.start.character
        != len(source[:function_prefix].encode("utf-16-le")) // 2
    )
    assert unresolved.location.range.end.character == len(
        source[: missing_prefix + 7].encode("utf-8")
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
    assert texts == {
        "./lib.js#default",
        "package#*",
        "package#default",
        "./lib#extensionless",
        "./setup.js#side-effect",
        "./lib.js#missing",
        "./lib.js#ghost",
        "./lib.js#helper",
        "./lib.js#dynamic",
    }
    module = _node(result, "app")
    unresolved = {
        node.id: node
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    for node in unresolved.values():
        references = [
            edge
            for edge in result.document.relationships
            if edge.target == node.id and edge.kind == RelationshipKind.REFERENCES.value
        ]
        assert references
        assert all(edge.source == module.id for edge in references)
        assert all(node.location in edge.evidence[0].locations for edge in references)
    assert not any(
        edge.target in unresolved and edge.kind == RelationshipKind.IMPORTS.value
        for edge in result.document.relationships
    )
    assert not _edges(result, RelationshipKind.CALLS.value)


def test_module_and_lexical_shadowing_cover_parameters_patterns_and_catches(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function target() {}\n"
                "function outer(target) { target(); { const target = value; target(); } "
                "target(); }\n"
                "function blockScope() { target(); { const target = value; target(); } "
                "target(); }\n"
                "function destructured({ target }) { target(); }\n"
                "function local() { const { target } = value; target(); }\n"
                "function caught() { try { value(); } catch (target) { target(); } target(); }\n"
            )
        },
    )
    target_nodes = [node for node in result.document.nodes if node.label == "app.target"]
    assert len(target_nodes) == 1
    target_id = target_nodes[0].id
    calls = [
        edge for edge in _edges(result, RelationshipKind.CALLS.value) if edge.target == target_id
    ]
    assert len(calls) == 2  # blockScope and caught each have an unshadowed use
    assert sum(len(edge.evidence[0].locations) for edge in calls) == 3
    assert not any(node.reference_text == "target" for node in result.document.nodes)


def test_lexical_module_bindings_and_unsupported_import_locals_are_suppressed(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "const total = 1; function f() { report(total); }\n"
                "import value from './lib.js'; value(); value;\n"
                "function target() {} function caller() { target(); }\n"
            ),
            "lib.js": "export function helper() {}\n",
        },
    )
    texts = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert texts == {"report", "./lib.js#default"}
    caller = _node(result, "app.caller")
    target = _node(result, "app.target")
    assert any(
        edge.source == caller.id
        and edge.target == target.id
        and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )


def test_member_bases_are_references_and_iife_bodies_are_walked_once(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "lib.js": "export function api() {}\nexport function target() {}\n",
            "app.js": (
                "import { api, target } from './lib.js';\n"
                "api.run(); const h = api.run; ghost.run();\n"
                "target(); (function () { hidden(); })();\n"
            ),
        },
    )
    api = _node(result, "lib.api")
    app = _node(result, "app")
    api_refs = [
        edge
        for edge in _edges(result, RelationshipKind.REFERENCES.value)
        if edge.source == app.id and edge.target == api.id
    ]
    assert len(api_refs) == 1
    assert len(api_refs[0].evidence[0].locations) == 2
    assert not any(node.reference_text == "run" for node in result.document.nodes)
    ghost = [node for node in result.document.nodes if node.reference_text == "ghost"]
    assert len(ghost) == 1
    assert (
        sum(
            edge.target == ghost[0].id and edge.kind == RelationshipKind.REFERENCES.value
            for edge in result.document.relationships
        )
        == 1
    )
    hidden = _node(result, "hidden")
    assert any(
        edge.source == app.id and edge.target == hidden.id for edge in result.document.relationships
    )
    target = _node(result, "lib.target")
    target_edges = [
        edge
        for edge in result.document.relationships
        if edge.target == target.id and edge.source == app.id
    ]
    assert [(edge.kind) for edge in target_edges] == [RelationshipKind.CALLS.value]


def test_relative_parent_imports_resolve_and_root_escape_is_unresolved(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "src/app.js": (
                "import { helper } from '../lib/util.js';\n"
                "import { helper as outside } from '../../outside.js';\n"
                "helper(); outside;\n"
            ),
            "lib/util.js": "export function helper() {}\n",
        },
    )
    app = _node(result, "src/app")
    util = _node(result, "lib/util")
    helper = _node(result, "lib/util.helper")
    imports = _edges(result, RelationshipKind.IMPORTS.value)
    assert [(edge.source, edge.target) for edge in imports] == [(app.id, util.id)]
    assert any(
        edge.source == app.id
        and edge.target == helper.id
        and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )
    outside = _node(result, "../../outside.js#helper")
    assert not any(
        edge.target == outside.id and edge.kind == RelationshipKind.IMPORTS.value
        for edge in imports
    )
    assert any(
        edge.source == app.id and edge.target == outside.id
        for edge in result.document.relationships
    )


@pytest.mark.parametrize("source", ["export function ok() {}\r", "export function ok() {}\r\n"])
def test_cr_and_crlf_documents_are_valid_under_source_text_validation(tmp_path, source):
    result = _analyze(tmp_path, {"app.js": source})
    assert result.diagnostics == ()
    assert validate_document(result.document, source_text_by_path={"app.js": source}).is_valid


def test_cr_only_parse_error_uses_shared_line_index(tmp_path):
    source = "function ok() {}\rfunction {"
    result = _analyze(tmp_path, {"broken.js": source})
    diagnostic = next(d for d in result.diagnostics if d.code is DiagnosticCode.PARSE_ERROR)
    assert diagnostic.location is not None
    assert diagnostic.location.range.start.line == 1


@pytest.mark.slow
def test_line_index_keeps_large_javascript_conversion_linear(tmp_path):
    """A 350 KB file converts in <=3 seconds (observed 1.54s on this runner)."""
    import time

    source = "".join(f"function f{i}() {{ missing(); }}\n" for i in range(12_000))
    assert len(source) > 350_000
    path = tmp_path / "large.js"
    path.write_text(source, encoding="utf-8")
    start = time.perf_counter()
    result = analyze_javascript_files(Workspace(tmp_path), (path,))
    elapsed = time.perf_counter() - start
    assert result.diagnostics == ()
    assert elapsed <= 3.0, f"analyze took {elapsed:.2f}s, expected <= 3.0s"


def test_raw_javascript_digest_matches_python_for_identical_bytes(tmp_path):
    source = b"\xef\xbb\xbfexport function helper() {}\r\n"
    js_path = tmp_path / "app.js"
    py_path = tmp_path / "app.py"
    js_path.write_bytes(source)
    py_path.write_bytes(b"def helper():\r\n    return None\r\n")
    js_result = analyze_javascript_files(Workspace(tmp_path), (js_path,))
    py_result = analyze_python_files(Workspace(tmp_path), (py_path,))
    js_file = _node(js_result, "app.js")
    py_file = next(node for node in py_result.document.nodes if node.label == "app.py")
    assert (
        js_file.extensions["minotaur-javascript"]["content_sha256"]
        == hashlib.sha256(source).hexdigest()
    )
    assert (
        py_file.extensions["minotaur-python"]["content_sha256"]
        == hashlib.sha256(py_path.read_bytes()).hexdigest()
    )


def test_public_line_helpers_have_one_shared_break_and_encoding_rule():
    assert split_lines("a\rb\r\nc\nd\f") == ["a", "b", "c", "d\f"]
    assert encoded_length("é😀", CoordinateEncoding.UTF_8) == 6
    assert encoded_length("é😀", CoordinateEncoding.UTF_16) == 3
    assert encoded_length("é😀", CoordinateEncoding.UTF_32) == 2


@pytest.mark.parametrize(
    ("source", "end_line", "end_character"),
    [
        ("const value = 'one😀'\n", 0, 23),
        ("const value = 'one😀'", 0, 23),
        ("const value = 'one😀'\r\n", 0, 23),
    ],
)
def test_javascript_module_location_ends_at_last_content_line(
    tmp_path, source, end_line, end_character
):
    result = _analyze(tmp_path, {"app.js": source})
    module = _node(result, "app")
    assert module.location.range.end.line == end_line
    assert module.location.range.end.character == end_character
