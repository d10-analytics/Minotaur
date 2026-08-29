from __future__ import annotations

import hashlib
import importlib.metadata

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


def test_parameter_defaults_and_class_superclass_expressions_keep_emitted_owners(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function helper() {}\n"
                "function use(value = helper()) {}\n"
                "function factory(value) {}\n"
                "class Base {}\n"
                "class Child extends factory(Base) { run(value = helper()) {} }\n"
            )
        },
    )
    helper = _node(result, "app.helper")
    use = _node(result, "app.use")
    run = _node(result, "app.Child.run")
    child = _node(result, "app.Child")
    base = _node(result, "app.Base")
    factory = _node(result, "app.factory")
    helper_calls = [
        edge for edge in _edges(result, RelationshipKind.CALLS.value) if edge.target == helper.id
    ]
    assert {(edge.source, edge.target) for edge in helper_calls} == {
        (use.id, helper.id),
        (run.id, helper.id),
    }
    assert any(
        edge.source == child.id
        and edge.target == factory.id
        and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )
    assert any(
        edge.source == child.id
        and edge.target == base.id
        and edge.kind == RelationshipKind.REFERENCES.value
        for edge in result.document.relationships
    )


def test_callable_variable_declarators_own_initializer_facts_independently(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function shared() {}\n"
                "const arrow = () => { shared(); arrowMissing(); }, "
                "named = function namedLocal() { shared; namedMissing(); }, "
                "value = sibling();\n"
            )
        },
    )
    module = _node(result, "app")
    shared = _node(result, "app.shared")
    arrow = _node(result, "app.arrow")
    named = _node(result, "app.named")
    shared_calls = [
        edge for edge in _edges(result, RelationshipKind.CALLS.value) if edge.target == shared.id
    ]
    shared_references = [
        edge
        for edge in _edges(result, RelationshipKind.REFERENCES.value)
        if edge.target == shared.id
    ]
    assert {(edge.source, edge.target) for edge in shared_calls} == {(arrow.id, shared.id)}
    assert {(edge.source, edge.target) for edge in shared_references} == {(named.id, shared.id)}
    unresolved = {
        node.reference_text: node
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    for text, owner in {
        "arrowMissing": arrow.id,
        "namedMissing": named.id,
        "sibling": module.id,
    }.items():
        edges = [
            edge
            for edge in _edges(result, RelationshipKind.REFERENCES.value)
            if edge.target == unresolved[text].id
        ]
        assert len(edges) == 1
        assert edges[0].source == owner


def test_named_function_expression_name_shadows_module_binding_only_in_its_body(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function helper() {}\n"
                "const callback = function helper() { helper(); outside(); };\n"
                "helper();\n"
            )
        },
    )
    helper = _node(result, "app.helper")
    callback = _node(result, "app.callback")
    assert any(
        edge.source == _node(result, "app").id
        and edge.target == helper.id
        and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )
    assert not any(
        edge.source == callback.id
        and edge.target == helper.id
        and edge.kind == RelationshipKind.CALLS.value
        for edge in result.document.relationships
    )
    assert not any(
        edge.source == callback.id
        and edge.target == helper.id
        and edge.kind == RelationshipKind.REFERENCES.value
        for edge in result.document.relationships
    )
    assert not any(node.reference_text == "helper" for node in result.document.nodes)
    assert any(node.reference_text == "outside" for node in result.document.nodes)


def test_object_property_function_bodies_are_deferred_but_iife_values_are_walked(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "const object = {"
                "deferred: function() { hidden(); }, "
                "arrow: () => hiddenArrow(), "
                "[key()]: function() { hiddenComputed(); }, "
                "[methodKey()]() { hiddenMethod(); }, "
                "eager: (function() { eager(); })()"
                "};\n"
            )
        },
    )
    texts = {
        node.reference_text for node in result.document.nodes if node.reference_text is not None
    }
    assert "eager" in texts
    assert {"key", "methodKey", "eager"} <= texts
    assert {"hidden", "hiddenArrow", "hiddenComputed", "hiddenMethod"}.isdisjoint(texts)


def test_computed_callable_keys_defer_bodies_but_iife_and_calls_execute(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "const object = {"
                "[function() { hiddenFunctionKey(); }]: value(), "
                "[() => hiddenArrowKey()]: value(), "
                "[(function() { executedKey(); })()]: value(), "
                "[ordinaryKey()]: value()"
                "};\n"
            )
        },
    )
    texts = {
        node.reference_text for node in result.document.nodes if node.reference_text is not None
    }
    assert {"executedKey", "ordinaryKey", "value"} <= texts
    assert {"hiddenFunctionKey", "hiddenArrowKey"}.isdisjoint(texts)


def test_destructured_parameter_defaults_are_walked_without_binding_uses(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function helper() {}\n"
                "function f({ x = helper() } = {}) {}\n"
                "function nested([first, { second = helper() }] = []) {}\n"
            )
        },
    )
    helper = _node(result, "app.helper")
    owners = {_node(result, "app.f").id, _node(result, "app.nested").id}
    calls = [
        edge for edge in _edges(result, RelationshipKind.CALLS.value) if edge.target == helper.id
    ]
    assert {edge.source for edge in calls} == owners
    assert not any(
        node.reference_text in {"x", "first", "second"} for node in result.document.nodes
    )


def test_all_parameter_bindings_shadow_defaults_and_computed_pattern_keys(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function later() {}\n"
                "function keyModule() {}\n"
                "function recursive() {}\n"
                "function f(first = later(), later = externalF()) {}\n"
                "function nested({ [keyModule]: value = externalNested } = {}, keyModule = {}) {}\n"
                "const named = function recursive(value = recursive()) {};\n"
                "later(); keyModule(); recursive();\n"
            )
        },
    )
    module = _node(result, "app")
    f = _node(result, "app.f")
    nested = _node(result, "app.nested")
    named = _node(result, "app.named")
    later = _node(result, "app.later")
    key_module = _node(result, "app.keyModule")
    recursive = _node(result, "app.recursive")
    assert not any(
        edge.source == f.id
        and edge.target == later.id
        and edge.kind in {RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value}
        for edge in result.document.relationships
    )
    assert not any(
        edge.source == nested.id
        and edge.target == key_module.id
        and edge.kind == RelationshipKind.REFERENCES.value
        for edge in result.document.relationships
    )
    assert not any(
        edge.source == named.id
        and edge.target == recursive.id
        and edge.kind in {RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value}
        for edge in result.document.relationships
    )
    unresolved = {
        node.reference_text: node
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert {"externalF", "externalNested"} <= unresolved.keys()
    assert any(
        edge.source == f.id
        and edge.target == unresolved["externalF"].id
        and edge.kind == RelationshipKind.REFERENCES.value
        for edge in result.document.relationships
    )
    assert any(
        edge.source == nested.id
        and edge.target == unresolved["externalNested"].id
        and edge.kind == RelationshipKind.REFERENCES.value
        for edge in result.document.relationships
    )
    for target in (later, key_module, recursive):
        assert any(
            edge.source == module.id
            and edge.target == target.id
            and edge.kind == RelationshipKind.CALLS.value
            for edge in result.document.relationships
        )


def test_nested_array_rest_assignment_and_late_computed_bindings_shadow_defaults(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function key() {}\n"
                "function objectValue() {}\n"
                "function arrayValue() {}\n"
                "function restValue() {}\n"
                "function assignedValue() {}\n"
                "const callback = ({ [key]: objectValue = externalObject, "
                "nested: [arrayValue = externalArray, ...restValue] } = {}, "
                "assignedValue = assignedValue(), key = {}) => {};\n"
                "key(); objectValue(); arrayValue(); restValue(); assignedValue();\n"
            )
        },
    )
    module = _node(result, "app")
    callback = _node(result, "app.callback")
    module_targets = {
        _node(result, f"app.{name}").id
        for name in ("key", "objectValue", "arrayValue", "restValue", "assignedValue")
    }
    assert not any(
        edge.source == callback.id
        and edge.target in module_targets
        and edge.kind in {RelationshipKind.CALLS.value, RelationshipKind.REFERENCES.value}
        for edge in result.document.relationships
    )
    unresolved = {
        node.reference_text: node
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert set(unresolved) == {"externalObject", "externalArray"}
    assert all(
        any(
            edge.source == callback.id
            and edge.target == unresolved[text].id
            and edge.kind == RelationshipKind.REFERENCES.value
            for edge in result.document.relationships
        )
        for text in ("externalObject", "externalArray")
    )
    for target_id in module_targets:
        assert any(
            edge.source == module.id
            and edge.target == target_id
            and edge.kind == RelationshipKind.CALLS.value
            for edge in result.document.relationships
        )


def test_local_export_lists_emit_module_owned_unresolved_facts_including_aliases(tmp_path):
    source = "function helper() {}\nexport { helper, helper as publicName };\n"
    result = _analyze(tmp_path, {"app.js": source})
    module = _node(result, "app")
    unresolved = {
        node.reference_text: node
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert {"export#helper", "export#publicName"} <= unresolved.keys()
    for text in ("export#helper", "export#publicName"):
        node = unresolved[text]
        matching = [
            edge
            for edge in _edges(result, RelationshipKind.REFERENCES.value)
            if edge.source == module.id and edge.target == node.id
        ]
        assert len(matching) == 1
        location = matching[0].evidence[0].locations[0]
        assert location == node.location
    export_line = source.splitlines()[1]
    assert (
        export_line[
            unresolved["export#helper"].location.range.start.character : unresolved[
                "export#helper"
            ].location.range.end.character
        ]
        == "helper"
    )
    assert (
        export_line[
            unresolved["export#publicName"].location.range.start.character : unresolved[
                "export#publicName"
            ].location.range.end.character
        ]
        == "publicName"
    )


def test_uppercase_javascript_suffixes_have_canonical_labels_and_resolve_imports(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "src/app.JS": "import { helper } from '../lib.JS';\nhelper();\n",
            "lib.JS": "export function helper() {}\n",
        },
    )
    app = _node(result, "src/app")
    lib = _node(result, "lib")
    helper = _node(result, "lib.helper")
    assert (
        _node(result, "src/app.JS").extensions["minotaur-javascript"]["content_sha256"]
        == hashlib.sha256(b"import { helper } from '../lib.JS';\nhelper();\n").hexdigest()
    )
    assert _node(result, "src/app.JS").label == "src/app.JS"
    assert _node(result, "lib.JS").label == "lib.JS"
    assert any(
        edge.source == app.id
        and edge.target == lib.id
        and edge.kind == RelationshipKind.IMPORTS.value
        for edge in result.document.relationships
    )
    assert any(
        edge.source == app.id
        and edge.target == helper.id
        and edge.kind == RelationshipKind.CALLS.value
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


def test_loop_scope_boundaries_preserve_outer_resolution_and_var_function_scope(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function target() {}\n"
                "function letLoop() { target(); for (let target of values) { "
                "target(); } target(); }\n"
                "function varBlock() { target(); { var target = 1; } target(); }\n"
            )
        },
    )
    target = _node(result, "app.target")
    let_loop = _node(result, "app.letLoop")
    var_block = _node(result, "app.varBlock")
    target_calls = [
        edge for edge in _edges(result, RelationshipKind.CALLS.value) if edge.target == target.id
    ]
    let_loop_calls = [edge for edge in target_calls if edge.source == let_loop.id]
    var_block_calls = [edge for edge in target_calls if edge.source == var_block.id]
    # A for-header let binding is limited to the loop; the outer calls still
    # resolve to the module declaration.
    assert len(let_loop_calls) == 1
    assert len(let_loop_calls[0].evidence[0].locations) == 2
    # A var binding in a nested block belongs to the enclosing function scope.
    assert not var_block_calls
    assert any(node.reference_text == "values" for node in result.document.nodes)


def test_var_in_nested_block_shadows_module_binding_for_enclosing_function(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function target() {}\n"
                "function varBlock() { target(); { var target = 1; } target(); }\n"
            )
        },
    )
    target = _node(result, "app.target")
    var_block = _node(result, "app.varBlock")
    assert not [
        edge
        for edge in _edges(result, RelationshipKind.CALLS.value)
        if edge.source == var_block.id and edge.target == target.id
    ]


def test_for_initializer_and_switch_lexical_scopes_do_not_leak(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function target() {}\n"
                "function loopInitializer() { for (let target = target; ok; next()) { "
                "target(); } target(); }\n"
                "function switchScope() { target(); switch (selector) { case 1: "
                "let target = 1; target(); break; } target(); }\n"
            )
        },
    )
    target = _node(result, "app.target")
    loop_initializer = _node(result, "app.loopInitializer")
    switch_scope = _node(result, "app.switchScope")
    target_calls = [
        edge for edge in _edges(result, RelationshipKind.CALLS.value) if edge.target == target.id
    ]
    loop_calls = [edge for edge in target_calls if edge.source == loop_initializer.id]
    switch_calls = [edge for edge in target_calls if edge.source == switch_scope.id]
    loop_target_references = [
        edge
        for edge in _edges(result, RelationshipKind.REFERENCES.value)
        if edge.source == loop_initializer.id and edge.target == target.id
    ]
    # The loop binding is in scope for its initializer (TDZ), body, test, and
    # update; only the call after the loop resolves to the module declaration.
    assert len(loop_calls) == 1
    assert len(loop_calls[0].evidence[0].locations) == 1
    assert not loop_target_references
    # A switch lexical declaration covers the switch, not its enclosing
    # function; calls before and after it still resolve.
    assert len(switch_calls) == 1
    assert len(switch_calls[0].evidence[0].locations) == 2
    assert {node.reference_text for node in result.document.nodes if node.reference_text} >= {
        "ok",
        "next",
        "selector",
    }


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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("function f() { unknown.target(); }", {"unknown", "unknown.target"}),
        ("function f() { const h = unknown.other; }", {"unknown", "unknown.other"}),
    ],
)
def test_member_calls_and_loads_emit_exact_base_and_full_facts(tmp_path, source, expected):
    result = _analyze(tmp_path, {"app.js": source})
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert unresolved == expected


def test_nested_member_calls_and_loads_keep_the_complete_chain_label(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "app.js": (
                "function f() { unknown.api.run(); const value = unknown.api.other; }"
            )
        },
    )
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert unresolved == {"unknown", "unknown.api.run", "unknown.api.other"}


def test_resolved_member_callee_keeps_import_reference_and_never_calls(tmp_path):
    result = _analyze(
        tmp_path,
        {
            "lib.js": "export function api() {}\n",
            "app.js": "import { api } from './lib.js';\napi.run();\n",
        },
    )
    app = _node(result, "app")
    api = _node(result, "lib.api")
    api_edges = [
        edge
        for edge in result.document.relationships
        if edge.source == app.id and edge.target == api.id
    ]
    assert [edge.kind for edge in api_edges] == [RelationshipKind.REFERENCES.value]
    assert {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    } == {"api.run"}
    assert not _edges(result, RelationshipKind.CALLS.value)


def test_shadowed_member_root_suppresses_the_whole_member_fact(tmp_path):
    result = _analyze(tmp_path, {"app.js": "function f(cfg) { return cfg.value; }"})
    assert {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    } == set()


def test_computed_member_chain_emits_full_text_and_ordinary_base_key_facts(tmp_path):
    result = _analyze(tmp_path, {"app.js": "function f() { return table[key].run(); }"})
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert unresolved == {"table", "key", "table[key].run"}
    assert not _edges(result, RelationshipKind.CALLS.value)


def test_member_fact_exclusions_keep_this_new_and_iife_dispatch_silent(tmp_path):
    result = _analyze(
        tmp_path,
        {"app.js": "this.field; new Foo().bar; (function () { hidden(); })().value;"},
    )
    unresolved = {
        node.reference_text
        for node in result.document.nodes
        if node.node_class is NodeClass.UNRESOLVED_REFERENCE
    }
    assert unresolved == {"hidden"}


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
    bom_source = b'\xef\xbb\xbfvalue = "\xf0\x9f\x98\x80";\r\n'
    source = b'value = "\xf0\x9f\x98\x80";\r\n'
    js_path = tmp_path / "app.js"
    py_path = tmp_path / "app.py"
    bom_path = tmp_path / "bom.js"
    bom_path.write_bytes(bom_source)
    js_path.write_bytes(source)
    py_path.write_bytes(source)
    js_result = analyze_javascript_files(Workspace(tmp_path), (js_path, bom_path))
    py_result = analyze_python_files(Workspace(tmp_path), (py_path,))
    js_file = _node(js_result, "app.js")
    bom_file = _node(js_result, "bom.js")
    py_file = next(node for node in py_result.document.nodes if node.label == "app.py")
    assert (
        js_file.extensions["minotaur-javascript"]["content_sha256"]
        == hashlib.sha256(source).hexdigest()
    )
    assert (
        bom_file.extensions["minotaur-javascript"]["content_sha256"]
        == hashlib.sha256(bom_source).hexdigest()
    )
    assert (
        py_file.extensions["minotaur-python"]["content_sha256"]
        == js_file.extensions["minotaur-javascript"]["content_sha256"]
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
